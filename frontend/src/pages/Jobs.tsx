import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api, formatDuration, type Job } from '../api'
import { ErrorNote } from '../components/ErrorNote'
import {
  Card,
  EmptyState,
  LoadingBlock,
  PageHeader,
  ProgressBar,
  StatusBadge,
  Tabs,
  buttonClass,
  relativeTime,
} from '../ui'
import { PlusIcon } from '../ui/icons'

type Filter = 'all' | 'running' | 'done' | 'failed'

const MATCH: Record<Filter, (j: Job) => boolean> = {
  all: () => true,
  running: (j) => j.status === 'running' || j.status === 'queued',
  done: (j) => j.status === 'done',
  failed: (j) => j.status === 'failed' || j.status === 'cancelled',
}

export function Jobs() {
  const navigate = useNavigate()
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [filter, setFilter] = useState<Filter>('all')

  useEffect(() => {
    const load = () =>
      api
        .listJobs(100)
        .then((list) => {
          setJobs(list)
          setError(null)
        })
        .catch((e) => setError(e as Error))
    void load()
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [])

  const visible = useMemo(() => (jobs ?? []).filter(MATCH[filter]), [jobs, filter])
  const count = (f: Filter) => (jobs ?? []).filter(MATCH[f]).length

  const newJobButton = (
    <Link to="/" className={buttonClass('primary')}>
      <PlusIcon />
      New clip job
    </Link>
  )

  return (
    <div>
      <PageHeader title="Jobs" actions={newJobButton} />

      {error && !jobs && <ErrorNote error={error} />}

      {!error && jobs === null && <LoadingBlock label="Loading jobs" />}

      {jobs && (
        <>
          <Tabs
            className="mb-4"
            ariaLabel="Filter jobs"
            value={filter}
            onChange={setFilter}
            items={[
              { value: 'all', label: 'All', count: count('all') },
              { value: 'running', label: 'Running', count: count('running') },
              { value: 'done', label: 'Done', count: count('done') },
              { value: 'failed', label: 'Failed', count: count('failed') },
            ]}
          />

          <Card bodyClassName="p-0">
            {visible.length === 0 ? (
              <EmptyState
                title={jobs.length === 0 ? 'No jobs yet' : 'No jobs match this filter'}
                description={jobs.length === 0 ? 'Start by adding a video.' : undefined}
                action={jobs.length === 0 ? newJobButton : undefined}
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-left text-[13px]">
                  <thead>
                    <tr className="border-b border-line text-xs text-faint">
                      <th className="px-4 py-2 font-medium">Title</th>
                      <th className="px-3 py-2 font-medium">Status</th>
                      <th className="w-48 px-3 py-2 font-medium">Stage</th>
                      <th className="px-3 py-2 font-medium">Clips</th>
                      <th className="px-3 py-2 font-medium">Created</th>
                      <th className="px-4 py-2 text-right font-medium">Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((job) => {
                      const target = job.status === 'done' ? `/jobs/${job.id}/clips` : `/jobs/${job.id}`
                      const title = job.source?.title || 'Untitled'
                      return (
                        <tr
                          key={job.id}
                          onClick={() => navigate(target)}
                          className="cursor-pointer border-b border-line last:border-0 hover:bg-surface-2"
                        >
                          <td className="max-w-[360px] px-4 py-2.5">
                            <Link
                              to={target}
                              onClick={(e) => e.stopPropagation()}
                              className="block truncate font-medium"
                              title={title}
                            >
                              {title}
                            </Link>
                          </td>
                          <td className="px-3 py-2.5">
                            <StatusBadge status={job.status} />
                          </td>
                          <td className="px-3 py-2.5">
                            {job.status === 'running' ? (
                              <div className="flex flex-col gap-1">
                                <ProgressBar value={job.progress} size="sm" />
                                <span className="truncate text-xs text-muted">
                                  {job.current_stage} {Math.round(job.progress * 100)}%
                                </span>
                              </div>
                            ) : (
                              <span className="text-xs text-faint">{job.current_stage || '-'}</span>
                            )}
                          </td>
                          <td className="px-3 py-2.5">
                            {job.status === 'done' ? (
                              <Link
                                to={`/jobs/${job.id}/clips`}
                                onClick={(e) => e.stopPropagation()}
                                className="text-accent hover:underline"
                              >
                                Review
                              </Link>
                            ) : (
                              <span className="text-faint">-</span>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-3 py-2.5 text-muted">
                            {relativeTime(job.created_at)}
                          </td>
                          <td className="px-4 py-2.5 text-right tabular-nums text-muted">
                            {job.source ? formatDuration(job.source.duration_s) : '-'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  )
}
