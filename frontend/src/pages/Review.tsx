import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  api,
  formatBytes,
  formatDuration,
  ApiError,
  type CaptionStyle,
  type ClipTemplate,
  type Clip,
  type CropPath,
  REJECT_REASONS,
  type Job,
  type RejectReason,
  type Word,
} from '../api'
import { CaptionEditor } from '../components/CaptionEditor'
import { ClipPlayer } from '../components/ClipPlayer'
import { ErrorNote } from '../components/ErrorNote'
import { HookPanel } from '../components/HookPanel'
import { ResultsPanel } from '../components/ResultsPanel'
import { TrimBar } from '../components/TrimBar'
import { QaBadge } from '../components/QaBadge'
import { QualityBadge } from '../components/QualityBadge'
import { QualityPanel } from '../components/QualityPanel'
import { qualityApi, type QualityReport } from '../qualityApi'
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  Kbd,
  LoadingBlock,
  PageHeader,
  Select,
  StatusBadge,
  Tabs,
  Textarea,
  buttonClass,
  cn,
  useOutsideClose,
  useToast,
} from '../ui'
import { CheckIcon, ChevronLeftIcon, CopyIcon, DownloadIcon, XIcon } from '../ui/icons'

const RATIOS = ['9:16', '1:1', '16:9'] as const

type ClipFilter = 'all' | 'kept' | 'pending' | 'rejected'
type PanelTab = 'details' | 'captions' | 'style'

const FILTERS: Record<ClipFilter, (c: Clip) => boolean> = {
  all: () => true,
  kept: (c) => c.status === 'kept' || c.status === 'exported',
  pending: (c) => c.status === 'candidate',
  rejected: (c) => c.status === 'discarded',
}

function aspectOf(ratio: string): [number, number] {
  const [w, h] = ratio.split(':').map(Number)
  return w > 0 && h > 0 ? [w, h] : [9, 16]
}

function isTyping(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}

export function Review() {
  const { jobId } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const [job, setJob] = useState<Job | null>(null)
  const [clips, setClips] = useState<Clip[]>([])
  const [styles, setStyles] = useState<CaptionStyle[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [words, setWords] = useState<Word[]>([])
  const [cropPath, setCropPath] = useState<CropPath | null>(null)
  const [wordsDirty, setWordsDirty] = useState(false)
  const [savingWords, setSavingWords] = useState(false)
  const [exporting, setExporting] = useState<Set<string>>(new Set())
  const [loadError, setLoadError] = useState<Error | null>(null)
  const [templates, setTemplates] = useState<ClipTemplate[]>([])
  const [templateBusy, setTemplateBusy] = useState(false)
  const [templateNote, setTemplateNote] = useState<string | null>(null)
  const [rev, setRev] = useState(0)
  const [quality, setQuality] = useState<Record<string, QualityReport>>({})
  const [askReasonFor, setAskReasonFor] = useState<string | null>(null)
  const [moreCount, setMoreCount] = useState(5)
  const [moreBusy, setMoreBusy] = useState(false)
  const [filter, setFilter] = useState<ClipFilter>('all')
  const [panelTab, setPanelTab] = useState<PanelTab>('details')
  const [view, setView] = useState<'final' | 'preview'>('preview')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    if (!jobId) return
    qualityApi
      .forJob(jobId)
      .then((list) => setQuality(Object.fromEntries(list.map((r) => [r.clip_id ?? '', r]))))
      .catch(() => undefined)
  }, [jobId])

  useEffect(() => {
    api.listTemplates().then(setTemplates).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!jobId) return
    void Promise.all([api.getJob(jobId), api.listClips(jobId), api.captionStyles()])
      .then(([loadedJob, loadedClips, loadedStyles]) => {
        setJob(loadedJob)
        setClips(loadedClips)
        setStyles(loadedStyles)
        setSelectedId((current) => current ?? loadedClips[0]?.id ?? null)
        setLoaded(true)
      })
      .catch((err) => setLoadError(err as Error))
  }, [jobId])

  const selected = useMemo(
    () => clips.find((clip) => clip.id === selectedId) ?? null,
    [clips, selectedId],
  )

  useEffect(() => {
    if (!selected) return
    setWordsDirty(false)
    api
      .getClipWords(selected.id)
      .then(setWords)
      .catch(() => setWords([]))
    // 404 is expected for audio-only sources and jobs that never reframed; the
    // player falls back to a centre crop, matching what the renderer does.
    api
      .getCropPath(selected.id)
      .then(setCropPath)
      .catch(() => setCropPath(null))
  }, [selected?.id])

  // Show the final render by default once a clip has one.
  const exportCount = selected?.exports.length ?? 0
  useEffect(() => {
    setView(exportCount > 0 ? 'final' : 'preview')
  }, [selected?.id, exportCount > 0])

  const patchClip = useCallback((updated: Clip) => {
    setClips((current) => current.map((clip) => (clip.id === updated.id ? updated : clip)))
  }, [])

  const setStatus = async (clip: Clip, status: Clip['status']) => {
    try {
      patchClip(await api.patchClip(clip.id, { status }))
      setAskReasonFor(status === 'discarded' ? clip.id : null)
    } catch (err) {
      toast.error(err as Error)
    }
  }

  const setRejectReason = async (clip: Clip, reason: RejectReason) => {
    try {
      patchClip(await api.patchClip(clip.id, { reject_reason: reason }))
      setAskReasonFor(null)
    } catch (err) {
      toast.error(err as Error)
    }
  }

  const generateMore = async () => {
    if (!jobId) return
    setMoreBusy(true)
    try {
      await api.generateMore(jobId, moreCount)
      navigate(`/jobs/${jobId}`)
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setMoreBusy(false)
    }
  }

  const commitTrim = async (start: number, end: number) => {
    if (!selected) return
    try {
      patchClip(await api.patchClip(selected.id, { start_s: start, end_s: end }))
      setWords(await api.getClipWords(selected.id))
      // The final render is now stale: show the trimmed live preview instead.
      setView('preview')
      if (selected.exports.length > 0) {
        toast.success('Trimmed. Export again to update the final video.')
      }
    } catch (err) {
      toast.error(err as Error)
    }
  }

  const saveWords = async () => {
    if (!selected) return
    setSavingWords(true)
    try {
      patchClip(await api.patchCaptions(selected.id, { words }))
      setWordsDirty(false)
      toast.success('Captions saved')
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setSavingWords(false)
    }
  }

  const setStyle = async (clip: Clip, styleKey: string) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { caption_style: styleKey }))
    } catch (err) {
      toast.error(err as Error)
    }
  }

  const rerender = async (clip: Clip, name: string) => {
    setTemplateBusy(true)
    setTemplateNote(null)
    try {
      await api.rerenderClip(clip.id, name)
      patchClip(await api.getClip(clip.id))
      setRev((n) => n + 1)
      toast.success(`Re-rendered with the "${name}" template`)
    } catch (err) {
      const e = err as ApiError
      const message =
        e.status === 404 || e.status === 405
          ? 'Re-rendering is not available on this server yet.'
          : e.message
      setTemplateNote(message)
      toast.error(message)
    } finally {
      setTemplateBusy(false)
    }
  }

  const setRatio = async (clip: Clip, ratio: string) => {
    try {
      patchClip(await api.patchCaptions(clip.id, { ratio }))
    } catch (err) {
      toast.error(err as Error)
    }
  }

  const exportClip = async (clip: Clip) => {
    setExporting((current) => new Set(current).add(clip.id))
    try {
      await api.exportClip(clip.id, clip.ratio, clip.caption_style)
      patchClip(await api.getClip(clip.id))
      toast.success('Clip exported')
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setExporting((current) => {
        const next = new Set(current)
        next.delete(clip.id)
        return next
      })
    }
  }

  const exportKept = async () => {
    const targets = clips.filter((clip) => clip.status === 'kept')
    for (const clip of targets) await exportClip(clip)
  }

  const visibleClips = useMemo(() => clips.filter(FILTERS[filter]), [clips, filter])

  // Keyboard shortcuts. The handler is read through a ref so the listener is
  // attached once but always sees the current state.
  const keyHandler = useRef<(e: KeyboardEvent) => void>(() => undefined)
  keyHandler.current = (e: KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return
    const index = visibleClips.findIndex((c) => c.id === selectedId)
    const go = (next: number) => {
      const target = visibleClips[Math.min(visibleClips.length - 1, Math.max(0, next))]
      if (target) setSelectedId(target.id)
    }
    switch (e.key) {
      case 'j':
      case 'ArrowDown':
        e.preventDefault()
        go(index + 1)
        break
      case 'k':
      case 'ArrowUp':
        e.preventDefault()
        go(index < 0 ? 0 : index - 1)
        break
      case 'a':
      case 'A':
        if (selected) void setStatus(selected, selected.status === 'kept' ? 'candidate' : 'kept')
        break
      case 'x':
      case 'X':
        if (selected) void setStatus(selected, selected.status === 'discarded' ? 'candidate' : 'discarded')
        break
    }
  }
  useEffect(() => {
    const handler = (e: KeyboardEvent) => keyHandler.current(e)
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const keptCount = clips.filter((clip) => clip.status === 'kept' || clip.status === 'exported').length
  const approvedToExport = clips.filter((clip) => clip.status === 'kept').length
  const candidateCount = clips.filter((clip) => clip.status === 'candidate').length
  const discardedCount = clips.filter((clip) => clip.status === 'discarded').length
  const activeStyle = styles.find((style) => style.key === selected?.caption_style)

  if (loadError && !loaded) {
    return (
      <div className="max-w-3xl">
        <PageHeader title="Review" />
        <ErrorNote error={loadError} />
      </div>
    )
  }

  if (!job) return <LoadingBlock label="Loading clips" />

  const latestExport = selected && selected.exports.length > 0 ? selected.exports[selected.exports.length - 1] : null
  const rules = job.campaign
  const showFinal = latestExport !== null && view === 'final'
  const [previewW, previewH] = aspectOf(showFinal && latestExport ? latestExport.ratio : (selected?.ratio ?? '9:16'))
  const finalMaxWidth = `max(200px, calc((100vh - 300px) * ${previewW} / ${previewH}))`

  return (
    <div>
      <PageHeader
        title={job.source?.title || 'Untitled'}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <Link to="/jobs" className="inline-flex items-center gap-0.5 hover:text-fg">
              <ChevronLeftIcon size={12} />
              All jobs
            </Link>
            <span className="text-faint">·</span>
            <span className="tabular-nums">
              {clips.length} clips, {keptCount} approved
            </span>
            {job.profile && (
              <span data-testid="content-profile" className="flex items-center gap-1">
                <span className="text-faint">·</span> Content profile: <span className="text-fg">{job.profile}</span>
              </span>
            )}
          </span>
        }
        actions={
          <Button onClick={exportKept} disabled={approvedToExport === 0 || exporting.size > 0} loading={exporting.size > 0}>
            Re-render approved
          </Button>
        }
      />

      {loadError && (
        <div className="mb-4">
          <ErrorNote error={loadError} onDismiss={() => setLoadError(null)} />
        </div>
      )}

      {clips.length === 0 ? (
        <Card>
          <EmptyState
            title="No clips came back"
            description="The model found nothing self-contained enough to stand alone. If you expected clips, widen the length range so shorter moments qualify, or lower the quality threshold in Settings."
            action={
              <Link to="/settings" className={buttonClass('secondary')}>
                Open settings
              </Link>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-[280px_minmax(0,1fr)] xl:grid-cols-[280px_minmax(0,1fr)_360px]">
          {/* Clip list */}
          <section className="flex flex-col overflow-hidden rounded-card border border-line bg-surface md:col-start-1 md:row-span-2 md:row-start-1 md:max-h-[calc(100vh-170px)] xl:row-span-1">
            <div className="border-b border-line p-3">
              <p className="mb-2 text-xs tabular-nums text-muted">
                {keptCount} kept · {candidateCount} candidate · {discardedCount} discarded
              </p>
              <Tabs
                ariaLabel="Filter clips"
                value={filter}
                onChange={setFilter}
                className="w-full [&>button]:flex-1 [&>button]:px-1.5"
                items={[
                  { value: 'all', label: 'All' },
                  { value: 'kept', label: 'Kept' },
                  { value: 'pending', label: 'Pending' },
                  { value: 'rejected', label: 'Rejected' },
                ]}
              />
            </div>
            <ul className="min-h-0 flex-1 overflow-y-auto">
              {visibleClips.length === 0 && (
                <li className="px-4 py-6 text-center text-[13px] text-muted">No clips in this filter.</li>
              )}
              {visibleClips.map((clip) => (
                <li key={clip.id}>
                  <ClipRow
                    clip={clip}
                    selected={clip.id === selectedId}
                    onSelect={() => setSelectedId(clip.id)}
                    quality={quality[clip.id]}
                  />
                </li>
              ))}
            </ul>
            <div className="flex items-center gap-2 border-t border-line p-3">
              <Input
                type="number"
                min={1}
                max={30}
                value={moreCount}
                onChange={(e) => setMoreCount(Math.max(1, Math.min(30, Number(e.target.value) || 1)))}
                className="!w-16"
                aria-label="How many more clips"
              />
              <Button
                className="flex-1"
                onClick={generateMore}
                loading={moreBusy}
                disabled={job.status !== 'done'}
                title="Find new moments in the same video, avoiding the clips you already have"
              >
                Generate more
              </Button>
            </div>
          </section>

          {/* Player */}
          <section className="min-w-0 md:col-start-2 md:row-start-1">
            {selected && jobId && (
              <div className="flex flex-col gap-4">
                <div className="rounded-card border border-line bg-surface p-4">
                  {latestExport && (
                    <div className="mb-3 flex justify-center">
                      <Tabs
                        ariaLabel="Player view"
                        value={view}
                        onChange={setView}
                        items={[
                          { value: 'final', label: 'Final render' },
                          { value: 'preview', label: 'Live preview' },
                        ]}
                      />
                    </div>
                  )}
                  {showFinal && latestExport ? (
                    <div className="mx-auto w-full" style={{ maxWidth: finalMaxWidth }}>
                      <video
                        key={`${latestExport.id}-${rev}`}
                        src={`${api.exportVideoUrl(latestExport.id)}?v=${rev}`}
                        controls
                        playsInline
                        className="w-full rounded-control bg-black object-contain"
                        style={{ aspectRatio: `${previewW} / ${previewH}` }}
                      />
                    </div>
                  ) : (
                    <ClipPlayer
                      src={api.mediaUrl(jobId)}
                      startS={selected.start_s}
                      endS={selected.end_s}
                      words={job?.burn_captions === false ? [] : words}
                      style={activeStyle}
                      ratio={selected.ratio}
                      cropPath={cropPath}
                    />
                  )}
                </div>

                <div className="rounded-card border border-line bg-surface p-4">
                  <TrimBar
                    words={words}
                    startS={selected.start_s}
                    endS={selected.end_s}
                    originalStart={selected.start_s}
                    originalEnd={selected.end_s}
                    onCommit={commitTrim}
                  />
                </div>

                <ActionBar
                  clip={selected}
                  exporting={exporting.has(selected.id)}
                  askReason={askReasonFor === selected.id}
                  onCloseReason={() => setAskReasonFor(null)}
                  onApprove={() => setStatus(selected, selected.status === 'kept' ? 'candidate' : 'kept')}
                  onReject={() => setStatus(selected, selected.status === 'discarded' ? 'candidate' : 'discarded')}
                  onReason={(reason) => setRejectReason(selected, reason)}
                  onExport={() => exportClip(selected)}
                  downloadUrl={latestExport?.download_url}
                />

                <p className="hidden items-center gap-3 text-xs text-faint md:flex">
                  <span>
                    <Kbd>J</Kbd> <Kbd>K</Kbd> next / previous
                  </span>
                  <span>
                    <Kbd>A</Kbd> approve
                  </span>
                  <span>
                    <Kbd>X</Kbd> reject
                  </span>
                </p>
              </div>
            )}
          </section>

          {/* Right panel */}
          <section className="min-w-0 md:col-start-2 md:row-start-2 xl:col-start-3 xl:row-start-1">
            {selected && (
              <Card
                bodyClassName="p-4"
                title={
                  <Tabs
                    ariaLabel="Clip panel"
                    value={panelTab}
                    onChange={setPanelTab}
                    items={[
                      { value: 'details', label: 'Details' },
                      { value: 'captions', label: 'Captions' },
                      { value: 'style', label: 'Style' },
                    ]}
                  />
                }
              >
                {panelTab === 'details' && (
                  <div className="flex flex-col gap-5">
                    <HookPanel clip={selected} onSaved={patchClip} />
                    <ShortsMeta clip={selected} onSaved={patchClip} />

                    <div>
                      <p className="text-xs font-medium text-muted">Why this clip</p>
                      <p className="mt-1 text-[13px] leading-relaxed">
                        {selected.reason || 'No rationale returned for this clip.'}
                      </p>
                      <div className="mt-3 flex gap-4 text-xs tabular-nums text-muted">
                        <span>
                          Score <span className="text-fg">{selected.score}</span>
                        </span>
                        <span>
                          AI <span className="text-fg">{selected.llm_score}</span>
                        </span>
                        <span>
                          Heuristic <span className="text-fg">{selected.heuristic_score}</span>
                        </span>
                      </div>
                    </div>

                    <ResultsPanel clip={selected} />

                    {selected.exports.length > 0 ? (
                      <QualityPanel
                        clipId={selected.id}
                        report={quality[selected.id]}
                        onReport={(r) => setQuality((q) => ({ ...q, [selected.id]: r }))}
                      />
                    ) : (
                      <p className="text-xs text-faint">Export this clip to run quality checks.</p>
                    )}

                    {(job.profile || rules) && (
                      <div data-testid="campaign-rules" className="border-t border-line pt-4">
                        <p className="mb-2 text-xs font-medium text-muted">Campaign rules</p>
                        {rules ? (
                          <>
                            {rules.warnings.length > 0 && (
                              <Alert tone="warning" className="mb-2">
                                <ul>
                                  {rules.warnings.map((w) => (
                                    <li key={w}>{w}</li>
                                  ))}
                                </ul>
                              </Alert>
                            )}
                            <dl className="space-y-1 text-xs">
                              {rules.required_hashtags.length > 0 && (
                                <RuleRow label="Hashtags" value={rules.required_hashtags.join(' ')} />
                              )}
                              {rules.required_caption_text.length > 0 && (
                                <RuleRow label="Caption text" value={rules.required_caption_text.join(' / ')} />
                              )}
                              {rules.caption_style && <RuleRow label="Tone" value={rules.caption_style} />}
                              <RuleRow label="Footage" value={rules.footage_type} />
                              {rules.forbidden.length > 0 && (
                                <RuleRow label="Forbidden" value={rules.forbidden.join(', ')} />
                              )}
                            </dl>
                          </>
                        ) : (
                          <p className="text-xs text-faint">No campaign brief for this job.</p>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {panelTab === 'captions' && (
                  <CaptionEditor
                    words={words}
                    onChange={(next) => {
                      setWords(next)
                      setWordsDirty(true)
                    }}
                    onSave={saveWords}
                    saving={savingWords}
                    dirty={wordsDirty}
                  />
                )}

                {panelTab === 'style' && (
                  <div className="flex flex-col gap-5">
                    <div>
                      <p className="mb-2 text-xs font-medium text-muted">Caption style</p>
                      <div className="grid grid-cols-2 gap-2">
                        {styles.map((style) => {
                          const on = style.key === selected.caption_style
                          return (
                            <button
                              key={style.key}
                              type="button"
                              aria-pressed={on}
                              onClick={() => setStyle(selected, style.key)}
                              className={cn(
                                'rounded-control border px-2.5 py-2 text-left transition-colors',
                                on ? 'border-accent bg-accent/10' : 'border-line hover:border-line-strong',
                              )}
                            >
                              <span className="block text-[13px] font-medium">{style.label}</span>
                              <span className="mt-0.5 block text-xs leading-snug text-faint">{style.description}</span>
                            </button>
                          )
                        })}
                      </div>
                    </div>

                    <div>
                      <p className="mb-2 text-xs font-medium text-muted">Aspect ratio</p>
                      <Tabs
                        ariaLabel="Aspect ratio"
                        value={(RATIOS as readonly string[]).includes(selected.ratio) ? selected.ratio : '9:16'}
                        onChange={(r) => void setRatio(selected, r)}
                        items={RATIOS.map((r) => ({ value: r, label: r }))}
                      />
                    </div>

                    <Field label="Template" htmlFor="rerender-template" hint="Re-renders this clip with the chosen template.">
                      <Select
                        id="rerender-template"
                        disabled={templateBusy}
                        value=""
                        onChange={(e) => e.target.value && void rerender(selected, e.target.value)}
                      >
                        <option value="">Switch template and re-render...</option>
                        {templates.map((t) => (
                          <option key={t.name} value={t.name}>
                            {t.label} - {t.description}
                          </option>
                        ))}
                      </Select>
                    </Field>
                    {templateBusy && <p className="text-xs text-muted">Rendering...</p>}
                    {templateNote && <p className="text-xs text-bad">{templateNote}</p>}

                    <div>
                      <p className="mb-2 text-xs font-medium text-muted">Exports</p>
                      {selected.exports.length === 0 ? (
                        <p className="text-xs text-faint">Nothing exported yet.</p>
                      ) : (
                        <ul className="divide-y divide-line rounded-control border border-line">
                          {selected.exports.map((record) => (
                            <li key={record.id} className="flex items-center justify-between gap-3 px-2.5 py-1.5 text-xs">
                              <span className="truncate">
                                {record.ratio} · {record.style}
                              </span>
                              <span className="flex shrink-0 items-center gap-3">
                                <span className="tabular-nums text-faint">{formatBytes(record.size_bytes)}</span>
                                <a
                                  href={record.download_url}
                                  download
                                  aria-label={`Download ${record.ratio} ${record.style}`}
                                  title="Download"
                                  className="text-accent hover:text-fg"
                                >
                                  <DownloadIcon size={14} />
                                </a>
                              </span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>
                )}
              </Card>
            )}
          </section>
        </div>
      )}
    </div>
  )
}

function RuleRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="inline text-faint">{label}: </dt>
      <dd className="inline text-muted">{value}</dd>
    </div>
  )
}

function ActionBar({
  clip,
  exporting,
  askReason,
  onCloseReason,
  onApprove,
  onReject,
  onReason,
  onExport,
  downloadUrl,
}: {
  clip: Clip
  exporting: boolean
  askReason: boolean
  onCloseReason: () => void
  onApprove: () => void
  onReject: () => void
  onReason: (reason: RejectReason) => void
  onExport: () => void
  downloadUrl?: string
}) {
  const popRef = useOutsideClose(onCloseReason, askReason && clip.status === 'discarded')
  const kept = clip.status === 'kept' || clip.status === 'exported'
  const rejected = clip.status === 'discarded'

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-card border border-line bg-surface p-3">
      <div className="relative" ref={popRef}>
        <Button variant={rejected ? 'danger' : 'secondary'} onClick={onReject}>
          <XIcon size={14} />
          {rejected ? 'Rejected (undo)' : 'Reject'}
        </Button>
        {askReason && rejected && (
          <div
            role="dialog"
            aria-label="Reject reason"
            className="absolute bottom-full left-0 z-20 mb-2 w-56 rounded-control border border-line-strong bg-surface-2 p-1.5 shadow-menu"
          >
            <p className="px-2 py-1 text-xs text-muted">Why? (optional)</p>
            {REJECT_REASONS.map((r) => (
              <button
                key={r.value}
                type="button"
                onClick={() => onReason(r.value)}
                className="block w-full rounded px-2 py-1.5 text-left text-[13px] hover:bg-line"
              >
                {r.label}
              </button>
            ))}
          </div>
        )}
      </div>
      <Button variant={kept ? 'secondary' : 'primary'} onClick={onApprove}>
        <CheckIcon size={14} />
        {kept ? 'Approved (undo)' : 'Approve'}
      </Button>
      <div className="ml-auto flex items-center gap-2">
        {downloadUrl && (
          <a href={downloadUrl} download className={buttonClass('secondary')}>
            <DownloadIcon size={14} />
            Download
          </a>
        )}
        <Button onClick={onExport} loading={exporting}>
          {exporting ? 'Rendering...' : clip.exports.length > 0 ? 'Export again' : 'Export'}
        </Button>
      </div>
    </div>
  )
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const toast = useToast()
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // Clipboard API needs a secure context; fall back to execCommand.
      const area = document.createElement('textarea')
      area.value = text
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      area.remove()
    }
    toast.success('Copied')
  }
  return (
    <Button size="sm" variant="ghost" onClick={copy} disabled={!text} aria-label={label} title={label}>
      <CopyIcon size={14} />
      Copy
    </Button>
  )
}

function parseTags(tags: string): string[] {
  return tags
    .split(/[\s,]+/)
    .filter(Boolean)
    .map((t) => (t.startsWith('#') ? t : `#${t}`))
}

/** YouTube Shorts title, description and hashtags, each with a copy button. */
function ShortsMeta({ clip, onSaved }: { clip: Clip; onSaved: (clip: Clip) => void }) {
  const toast = useToast()
  const [title, setTitle] = useState(clip.title)
  const [description, setDescription] = useState(clip.description)
  const [tags, setTags] = useState(clip.hashtags.join(' '))
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setTitle(clip.title)
    setDescription(clip.description)
    setTags(clip.hashtags.join(' '))
  }, [clip.id, clip.title, clip.description, clip.hashtags])

  const dirty =
    title !== clip.title ||
    description !== clip.description ||
    parseTags(tags).join(' ') !== clip.hashtags.join(' ')

  const save = async () => {
    if (!dirty) return
    setSaving(true)
    try {
      onSaved(await api.patchClip(clip.id, { title, description, hashtags: parseTags(tags) }))
      toast.success('Saved')
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label htmlFor="meta-title" className="text-xs font-medium text-muted">
            Title
          </label>
          <CopyButton text={title} label="Copy title" />
        </div>
        <Input id="meta-title" value={title} onChange={(e) => setTitle(e.target.value)} onBlur={save} />
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label htmlFor="meta-description" className="text-xs font-medium text-muted">
            Description
          </label>
          <CopyButton text={description} label="Copy description" />
        </div>
        <Textarea
          id="meta-description"
          rows={4}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          onBlur={save}
        />
      </div>
      <div>
        <div className="mb-1 flex items-center justify-between">
          <label htmlFor="meta-tags" className="text-xs font-medium text-muted">
            Hashtags
          </label>
          <CopyButton text={tags} label="Copy hashtags" />
        </div>
        <Input id="meta-tags" value={tags} onChange={(e) => setTags(e.target.value)} onBlur={save} />
      </div>
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={save} loading={saving} disabled={!dirty}>
          Save
        </Button>
        <CopyButton text={[title, description, tags].filter(Boolean).join('\n\n')} label="Copy all" />
        {dirty && <span className="text-xs text-faint">Unsaved changes</span>}
      </div>
    </div>
  )
}

function ClipRow({
  clip,
  selected,
  onSelect,
  quality,
}: {
  clip: Clip
  selected: boolean
  onSelect: () => void
  quality?: QualityReport
}) {
  return (
    <div
      className={cn(
        'grid cursor-pointer grid-cols-[1.75rem_minmax(0,1fr)] gap-x-2 border-b border-line px-3 py-2.5 transition-colors last:border-0',
        selected ? 'bg-surface-2 shadow-[inset_2px_0_0_var(--color-accent)]' : 'hover:bg-surface-2/60',
        clip.status === 'discarded' && !selected && 'opacity-60',
      )}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      aria-current={selected ? 'true' : undefined}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onSelect()
      }}
    >
      <span className="pt-0.5 text-xs tabular-nums text-faint">#{clip.rank}</span>
      <div className="min-w-0">
        <p className="line-clamp-2 text-[13px] font-medium leading-snug" title={clip.title || 'Untitled clip'}>
          {clip.title || 'Untitled clip'}
        </p>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <span className="text-xs tabular-nums text-muted">{formatDuration(clip.duration_s)}</span>
          <Badge tone={clip.score >= 85 ? 'success' : clip.score >= 70 ? 'info' : 'neutral'} title="Clip score">
            {clip.score}
          </Badge>
          <StatusBadge status={clip.status} />
          <QualityBadge report={quality} />
          <QaBadge qa={quality?.qa} />
          {clip.user_trimmed && <span className="text-xs text-faint">trimmed</span>}
        </div>
        {clip.status === 'discarded' && clip.reject_reason && (
          <p className="mt-1 text-xs text-faint">
            {REJECT_REASONS.find((r) => r.value === clip.reject_reason)?.label}
          </p>
        )}
      </div>
    </div>
  )
}
