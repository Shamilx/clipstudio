import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { api, formatDuration, type Job } from '../api'
import { ErrorNote } from '../components/ErrorNote'
import { useJobStream } from '../useJobStream'
import {
  Alert,
  Button,
  Card,
  ConfirmButton,
  LoadingBlock,
  PageHeader,
  ProgressBar,
  Spinner,
  StatusBadge,
  cn,
  parseUtc,
  useToast,
} from '../ui'
import { CheckIcon, ChevronDownIcon, ChevronRightIcon, RefreshIcon, XIcon } from '../ui/icons'

/** Stage order and labels, mirroring clipstudio.pipeline.Stage. */
const STAGES = [
  { key: 'prepare', label: 'Prepare', note: 'Extracting audio and thumbnails' },
  { key: 'transcribe', label: 'Transcribe', note: 'Word-level timing' },
  { key: 'highlights', label: 'Highlights', note: 'Choosing the moments worth cutting' },
  { key: 'reframe', label: 'Reframe', note: 'Tracking the speaker into vertical' },
  { key: 'captions', label: 'Captions', note: 'Building subtitle timing' },
  { key: 'export', label: 'Export', note: 'Rendering clips' },
  { key: 'qa', label: 'QA', note: 'Checking the finished frames' },
] as const

function ageSeconds(iso: string | null | undefined, now: number): number | null {
  const t = parseUtc(iso)
  return t === null ? null : Math.max(0, Math.round((now - t) / 1000))
}

function ago(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.floor(seconds / 60)} min ${seconds % 60}s ago`
}

function ActivityPanel({ job }: { job: Job }) {
  const [now, setNow] = useState(() => Date.now())
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  const live = job.status === 'running'
  const age = ageSeconds(job.heartbeat_at, now)
  const tone =
    !live || age === null
      ? 'text-faint'
      : age > 180
        ? 'text-bad'
        : age > 60
          ? 'text-warn'
          : 'text-faint'
  const log = [...(job.activity_log ?? [])].reverse()
  if (!job.activity && log.length === 0) return null

  return (
    <div className="mt-4 border-t border-line pt-3">
      {job.activity && <p className="text-[13px]">{job.activity}</p>}
      {live && age !== null && (
        <p className={cn('mt-0.5 text-xs tabular-nums', tone)}>
          Updated {ago(age)}
          {age > 180 ? '. No update for 3 min, it may be stuck.' : ''}
        </p>
      )}
      {log.length > 0 && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setOpen(!open)}
            aria-expanded={open}
            className="flex items-center gap-1 text-xs text-muted hover:text-fg"
          >
            {open ? <ChevronDownIcon size={12} /> : <ChevronRightIcon size={12} />}
            Activity log ({log.length})
          </button>
          {open && (
            <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto rounded-control bg-bg p-2 font-mono text-[11px] text-muted">
              {log.map((line, i) => (
                <li key={`${line.t}-${i}`}>
                  <span className="text-faint">
                    {new Date(parseUtc(line.t) ?? Date.now()).toLocaleTimeString()}
                  </span>{' '}
                  {line.stage && <span className="text-faint">[{line.stage}]</span>} {line.msg}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

export function JobProgress() {
  const { jobId } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const { job, progress } = useJobStream(jobId)
  const [cancelling, setCancelling] = useState(false)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    if (job?.status === 'done') {
      // A short hold so the completed state is legible rather than a flash.
      const timer = setTimeout(() => navigate(`/jobs/${job.id}/clips`), 900)
      return () => clearTimeout(timer)
    }
  }, [job?.status, job?.id, navigate])

  if (!job) return <LoadingBlock label="Loading job" />

  const activeIndex = STAGES.findIndex((s) => s.key === job.current_stage)
  const percent = Math.round((progress?.overall ?? job.progress) * 100)
  const stopped = job.status === 'failed' || job.status === 'cancelled'

  const cancel = async () => {
    setCancelling(true)
    try {
      await api.cancelJob(job.id)
      toast.success('Job cancelled')
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setCancelling(false)
    }
  }

  const retry = async () => {
    setRetrying(true)
    try {
      await api.retryJob(job.id)
      window.location.reload()
    } catch (err) {
      toast.error(err as Error)
      setRetrying(false)
    }
  }

  const subtitle = [
    job.source ? formatDuration(job.source.duration_s) : null,
    job.profile ? `${job.profile} profile` : null,
    job.provider,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className="max-w-3xl">
      <PageHeader
        title={job.source?.title || 'Untitled'}
        subtitle={subtitle}
        actions={
          <>
            <StatusBadge status={job.status} />
            {(job.status === 'running' || job.status === 'queued') && (
              <ConfirmButton onConfirm={cancel} loading={cancelling} confirmLabel="Confirm cancel" variant="secondary">
                Cancel
              </ConfirmButton>
            )}
            {stopped && (
              <Button variant="primary" onClick={retry} loading={retrying}>
                <RefreshIcon />
                Retry
              </Button>
            )}
            {job.status === 'done' && (
              <Button variant="primary" onClick={() => navigate(`/jobs/${job.id}/clips`)}>
                Review clips
              </Button>
            )}
          </>
        }
      />

      <div className="flex flex-col gap-4">
        {job.error && (
          <div>
            <ErrorNote error={new Error(job.error)} />
            <p className="mt-1.5 text-xs text-faint">
              Retrying resumes from the stage that failed. Finished stages are not redone.
            </p>
          </div>
        )}

        {job.status === 'done' && (
          <Alert tone="success" title="Job finished">
            Opening the clip review.
          </Alert>
        )}

        <Card>
          <div className="flex items-center gap-3">
            <ProgressBar value={percent / 100} tone={stopped ? 'danger' : job.status === 'done' ? 'success' : 'accent'} label="Overall progress" />
            <span className="w-10 shrink-0 text-right text-[13px] font-medium tabular-nums">{percent}%</span>
          </div>
          <ActivityPanel job={job} />
        </Card>

        <Card title="Stages" bodyClassName="p-0">
          <ol>
            {STAGES.map((stage, index) => {
              // A failed job stopped at current_stage, so that stage reads as failed.
              const failedHere = stopped && index === activeIndex
              const state =
                job.status === 'done' || (activeIndex >= 0 && index < activeIndex)
                  ? 'done'
                  : failedHere
                    ? 'stopped'
                    : index === activeIndex && job.status === 'running'
                      ? 'active'
                      : 'pending'
              const fraction = state === 'active' ? progress?.stageProgress : undefined

              return (
                <li key={stage.key} className="flex items-start gap-3 border-b border-line px-4 py-3 last:border-0">
                  <span
                    className={cn(
                      'mt-0.5 grid size-5 shrink-0 place-items-center rounded-full border',
                      state === 'done' && 'border-ok/50 bg-ok/10 text-ok',
                      state === 'stopped' && 'border-bad/50 bg-bad/10 text-bad',
                      state === 'active' && 'border-accent/50 text-accent',
                      state === 'pending' && 'border-line-strong text-faint',
                    )}
                  >
                    {state === 'done' ? (
                      <CheckIcon size={12} />
                    ) : state === 'stopped' ? (
                      <XIcon size={12} />
                    ) : state === 'active' ? (
                      <Spinner size={12} />
                    ) : (
                      <span className="text-[10px] tabular-nums">{index + 1}</span>
                    )}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4">
                      <span className={cn('text-[13px] font-medium', state === 'pending' && 'text-faint')}>
                        {stage.label}
                      </span>
                      <span className={cn('text-xs', state === 'stopped' ? 'text-bad' : 'text-faint')}>
                        {state === 'stopped'
                          ? job.status === 'cancelled'
                            ? 'Cancelled here'
                            : 'Failed here'
                          : state === 'active'
                            ? (progress?.message ?? stage.note)
                            : stage.note}
                      </span>
                    </div>
                    {state === 'active' && (
                      <ProgressBar value={fraction ?? null} size="sm" className="mt-2" label={`${stage.label} progress`} />
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        </Card>
      </div>
    </div>
  )
}
