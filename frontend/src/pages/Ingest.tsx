import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import {
  ApiError,
  EDIT_LABELS,
  api,
  formatDuration,
  type CaptionStyle,
  type ClipTemplate,
  type EditToggles,
  type Job,
  type JobSettingsOverrides,
  type ProviderStatus,
} from '../api'
import { ErrorNote } from '../components/ErrorNote'
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  PageHeader,
  ProgressBar,
  Select,
  StatusBadge,
  SwitchRow,
  Tabs,
  Textarea,
  cn,
  relativeTime,
} from '../ui'
import { ChevronDownIcon, ChevronRightIcon, UploadIcon } from '../ui/icons'

type SourceTab = 'link' | 'upload'

export function Ingest() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<SourceTab>('link')
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState<'url' | 'file' | null>(null)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [fetchStatus, setFetchStatus] = useState<{ fraction: number | null; message: string }>({
    fraction: null,
    message: '',
  })
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [providers, setProviders] = useState<ProviderStatus[]>([])
  const [styles, setStyles] = useState<CaptionStyle[]>([])
  const [templates, setTemplates] = useState<ClipTemplate[]>([])
  const [template, setTemplate] = useState('auto')
  const [defaults, setDefaults] = useState<Partial<EditToggles>>({})
  const [overrides, setOverrides] = useState<JobSettingsOverrides>({})
  const [optionsOpen, setOptionsOpen] = useState(false)
  const [brief, setBrief] = useState('')
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const load = () =>
      api
        .listJobs(5)
        .then(setJobs)
        .catch(() => setJobs((current) => current ?? []))
    void load()
    const poll = window.setInterval(load, 3000)
    api.providerStatus().then(setProviders).catch(() => undefined)
    api.listTemplates().then(setTemplates).catch(() => undefined)
    api.captionStyles().then(setStyles).catch(() => undefined)
    api.getSettings().then((s) => setDefaults(s.edit)).catch(() => undefined)
    return () => window.clearInterval(poll)
  }, [])

  const start = useCallback(
    async (kind: 'url' | 'file', run: () => Promise<{ id: string }>) => {
      setBusy(kind)
      setError(null)
      try {
        const source = await run()
        const job = await api.createJob(source.id, overrides, template, brief)
        navigate(`/jobs/${job.id}`)
      } catch (err) {
        setError(err as Error)
      } finally {
        setBusy(null)
      }
    },
    [navigate, overrides, template, brief],
  )

  const submitUrl = (event: React.FormEvent) => {
    event.preventDefault()
    if (!url.trim() || busy) return
    const progressId = Math.random().toString(36).slice(2, 12)
    setFetchStatus({ fraction: null, message: 'Contacting YouTube' })
    const timer = window.setInterval(() => {
      api.ingestProgress(progressId).then(setFetchStatus).catch(() => undefined)
    }, 700)
    void start('url', () => api.ingestYouTube(url.trim(), undefined, progressId)).finally(() =>
      window.clearInterval(timer),
    )
  }

  const submitFile = (file: File) => {
    if (busy) return
    void start('file', () => api.uploadSource(file))
  }

  // Dropping a file anywhere on the page starts an upload.
  const submitFileRef = useRef(submitFile)
  submitFileRef.current = submitFile
  useEffect(() => {
    const hasFiles = (e: DragEvent) => Boolean(e.dataTransfer?.types.includes('Files'))
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      setDragging(true)
    }
    const leave = (e: DragEvent) => {
      if (e.relatedTarget === null) setDragging(false)
    }
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return
      e.preventDefault()
      setDragging(false)
      const file = e.dataTransfer?.files[0]
      if (file) {
        setTab('upload')
        submitFileRef.current(file)
      }
    }
    window.addEventListener('dragover', over)
    window.addEventListener('dragleave', leave)
    window.addEventListener('drop', drop)
    return () => {
      window.removeEventListener('dragover', over)
      window.removeEventListener('dragleave', leave)
      window.removeEventListener('drop', drop)
    }
  }, [])

  const usableProvider = providers.find((p) => p.available)
  const selectedTemplate = templates.find((t) => t.name === template)
  const set = <K extends keyof JobSettingsOverrides>(key: K, value: JobSettingsOverrides[K]) =>
    setOverrides((current) => ({ ...current, [key]: value }))

  return (
    <div>
      <PageHeader title="New clip job" subtitle="Add a source video, then pick how clips should be chosen." />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="flex min-w-0 flex-col gap-4">
          {error && <ErrorNote error={error} onDismiss={() => setError(null)} />}

          {providers.length > 0 && !usableProvider && (
            <Alert tone="warning" title="No AI provider is reachable">
              Clip selection will fail. Check the API keys in{' '}
              <Link to="/settings" className="text-fg underline underline-offset-2">
                Settings
              </Link>
              .
            </Alert>
          )}

          <Card
            title="Source"
            actions={
              <Tabs
                ariaLabel="Source type"
                value={tab}
                onChange={setTab}
                items={[
                  { value: 'link', label: 'YouTube link' },
                  { value: 'upload', label: 'Upload file' },
                ]}
              />
            }
          >
            {tab === 'link' ? (
              <form onSubmit={submitUrl} className="flex items-end gap-2">
                <Field label="Video URL" htmlFor="url" className="flex-1">
                  <Input
                    id="url"
                    placeholder="https://youtube.com/watch?v=..."
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                    disabled={busy !== null}
                  />
                </Field>
                <Button type="submit" variant="primary" loading={busy === 'url'} disabled={!url.trim() || busy !== null}>
                  Start
                </Button>
              </form>
            ) : (
              <div
                role="button"
                tabIndex={0}
                aria-label="Choose a video file"
                onClick={() => !busy && fileInput.current?.click()}
                onKeyDown={(e) => {
                  if ((e.key === 'Enter' || e.key === ' ') && !busy) {
                    e.preventDefault()
                    fileInput.current?.click()
                  }
                }}
                className={cn(
                  'flex min-h-40 cursor-pointer flex-col items-center justify-center gap-1.5 rounded-control border border-dashed px-6 text-center transition-colors',
                  dragging ? 'border-accent bg-accent/10' : 'border-line-strong hover:border-faint',
                  busy && 'cursor-progress opacity-70',
                )}
              >
                <UploadIcon size={20} className="text-muted" />
                <span className="text-[13px] font-medium">
                  {busy === 'file' ? 'Uploading...' : 'Drop a video file or click to browse'}
                </span>
                <span className="text-xs text-faint">mp4, mov, mkv, webm, mp3, wav, m4a</span>
              </div>
            )}
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              accept="video/*,audio/*"
              aria-label="Video file"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) submitFile(file)
                e.target.value = ''
              }}
            />

            {busy && (
              <div className="mt-4" role="status" aria-live="polite">
                <ProgressBar value={busy === 'url' ? fetchStatus.fraction : null} />
                <p className="mt-1.5 text-xs tabular-nums text-muted">
                  {busy === 'url' ? fetchStatus.message || 'Fetching video' : 'Uploading file'}
                </p>
              </div>
            )}
            <p className="mt-3 text-xs text-faint">Only process videos you have rights to.</p>
          </Card>

          <Card title="Campaign">
            <div className="flex flex-col gap-4">
              <Field
                label="Template"
                htmlFor="template"
                hint={
                  template === 'auto'
                    ? 'Picks a template from the video content.'
                    : selectedTemplate?.description
                }
              >
                <Select id="template" value={template} onChange={(e) => setTemplate(e.target.value)}>
                  <option value="auto">Auto (recommended)</option>
                  {templates.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field
                label="Brief (optional)"
                htmlFor="campaign-brief"
                hint={<span className="tabular-nums">{brief.length} / 6000</span>}
              >
                <Textarea
                  id="campaign-brief"
                  value={brief}
                  onChange={(e) => setBrief(e.target.value.slice(0, 6000))}
                  rows={5}
                  maxLength={6000}
                  placeholder="Paste the campaign brief: what to pick, what to avoid, tone, required hashtags or mentions."
                />
              </Field>
            </div>
          </Card>

          <section className="rounded-card border border-line bg-surface">
            <button
              type="button"
              onClick={() => setOptionsOpen((v) => !v)}
              aria-expanded={optionsOpen}
              className="flex h-11 w-full items-center justify-between px-4 text-sm font-semibold"
            >
              Options
              {optionsOpen ? <ChevronDownIcon /> : <ChevronRightIcon />}
            </button>
            {optionsOpen && (
              <div className="border-t border-line p-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Provider" htmlFor="opt-provider">
                    <Select
                      id="opt-provider"
                      value={overrides.provider ?? ''}
                      onChange={(e) => set('provider', e.target.value || undefined)}
                    >
                      <option value="">Use default</option>
                      {providers.map((p) => (
                        <option key={p.name} value={p.name} disabled={!p.available}>
                          {p.available ? p.name : `${p.name} (unavailable)`}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Language" htmlFor="opt-language">
                    <Select
                      id="opt-language"
                      value={overrides.language ?? ''}
                      onChange={(e) => set('language', e.target.value)}
                    >
                      <option value="">Detect automatically</option>
                      <option value="en">English</option>
                      <option value="az">Azerbaijani</option>
                      <option value="tr">Turkish</option>
                      <option value="ru">Russian</option>
                    </Select>
                  </Field>
                  <Field label="Min length (s)" htmlFor="opt-min">
                    <Input
                      id="opt-min"
                      type="number"
                      min={5}
                      max={300}
                      placeholder="20"
                      value={overrides.min_duration_s ?? ''}
                      onChange={(e) => set('min_duration_s', e.target.value ? Number(e.target.value) : undefined)}
                    />
                  </Field>
                  <Field label="Max length (s)" htmlFor="opt-max">
                    <Input
                      id="opt-max"
                      type="number"
                      min={5}
                      max={300}
                      placeholder="90"
                      value={overrides.max_duration_s ?? ''}
                      onChange={(e) => set('max_duration_s', e.target.value ? Number(e.target.value) : undefined)}
                    />
                  </Field>
                  <Field label="Max clips" htmlFor="opt-clips">
                    <Input
                      id="opt-clips"
                      type="number"
                      min={1}
                      max={50}
                      placeholder="10"
                      value={overrides.max_clips ?? ''}
                      onChange={(e) => set('max_clips', e.target.value ? Number(e.target.value) : undefined)}
                    />
                  </Field>
                  <Field label="Aspect ratio" htmlFor="opt-ratio">
                    <Select
                      id="opt-ratio"
                      value={overrides.ratio ?? ''}
                      onChange={(e) => set('ratio', e.target.value || undefined)}
                    >
                      <option value="">Default</option>
                      <option value="9:16">9:16</option>
                      <option value="1:1">1:1</option>
                      <option value="16:9">16:9</option>
                    </Select>
                  </Field>
                  <Field label="Caption style" htmlFor="opt-style" className="sm:col-span-2">
                    <Select
                      id="opt-style"
                      value={overrides.caption_style ?? ''}
                      onChange={(e) => set('caption_style', e.target.value || undefined)}
                    >
                      <option value="">Default</option>
                      {styles.map((s) => (
                        <option key={s.key} value={s.key}>
                          {s.label} - {s.description}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </div>
                <div className="mt-4 border-t border-line pt-2">
                  <p className="pt-2 text-xs font-medium text-muted">Auto-edit</p>
                  <div className="divide-y divide-line">
                    {(Object.keys(EDIT_LABELS) as (keyof EditToggles)[]).map((key) => (
                      <SwitchRow
                        key={key}
                        label={EDIT_LABELS[key][0]}
                        hint={EDIT_LABELS[key][1]}
                        checked={overrides[key] ?? Boolean(defaults[key])}
                        onChange={(v) => set(key, v)}
                      />
                    ))}
                  </div>
                </div>
              </div>
            )}
          </section>
        </div>

        <aside className="min-w-0">
          <Card title="Recent jobs" actions={<Link to="/jobs" className="text-xs text-muted hover:text-fg">View all</Link>} bodyClassName="p-0">
            {jobs === null ? (
              <p className="px-4 py-6 text-[13px] text-muted">Loading...</p>
            ) : jobs.length === 0 ? (
              <EmptyState title="No jobs yet" description="Jobs you start appear here." />
            ) : (
              <ul className="divide-y divide-line">
                {jobs.map((job) => (
                  <li key={job.id}>
                    <Link
                      to={job.status === 'done' ? `/jobs/${job.id}/clips` : `/jobs/${job.id}`}
                      className="block px-4 py-2.5 hover:bg-surface-2"
                    >
                      <div className="truncate text-[13px] font-medium" title={job.source?.title || 'Untitled'}>
                        {job.source?.title || 'Untitled'}
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted">
                        <StatusBadge status={job.status} />
                        {job.status === 'running' && (
                          <span className="tabular-nums">{Math.round(job.progress * 100)}%</span>
                        )}
                        <span className="tabular-nums">
                          {job.source ? formatDuration(job.source.duration_s) : ''}
                        </span>
                        <span className="ml-auto text-faint">{relativeTime(job.created_at)}</span>
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </aside>
      </div>
    </div>
  )
}
