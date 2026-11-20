import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, type ClipTemplate, type QueueState } from '../api'
import { ErrorNote } from '../components/ErrorNote'
import {
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  IconButton,
  LoadingBlock,
  PageHeader,
  ProgressBar,
  Select,
  StatusBadge,
  Textarea,
  buttonClass,
  useToast,
} from '../ui'
import {
  ArrowDownIcon,
  ArrowUpIcon,
  ChevronRightIcon,
  PauseIcon,
  PlayIcon,
  RefreshIcon,
  TrashIcon,
} from '../ui/icons'

const FINISHED = ['done', 'failed', 'cancelled']

export function Queue() {
  const toast = useToast()
  const [state, setState] = useState<QueueState | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [templates, setTemplates] = useState<ClipTemplate[]>([])
  const [text, setText] = useState('')
  const [template, setTemplate] = useState('auto')
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api
      .getQueue()
      .then((s) => {
        setState(s)
        setError(null)
      })
      .catch((e) => setError(e as Error))
  }, [])

  useEffect(() => {
    refresh()
    api.listTemplates().then(setTemplates).catch(() => undefined)
    const t = window.setInterval(refresh, 3000)
    return () => window.clearInterval(t)
  }, [refresh])

  const act = async (key: string, run: () => Promise<QueueState>) => {
    setBusy(key)
    try {
      setState(await run())
    } catch (e) {
      toast.error(e as Error)
    } finally {
      setBusy(null)
    }
  }

  const add = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    setAdding(true)
    try {
      const result = await api.enqueue(text, template)
      setState(result)
      setText('')
      toast.success(`Added ${result.added ?? 0}, skipped ${result.skipped ?? 0}`)
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setAdding(false)
    }
  }

  const items = state?.items ?? []
  const finished = items.filter((i) => FINISHED.includes(i.status))

  return (
    <div>
      <PageHeader
        title="Queue"
        subtitle="Videos run one at a time. The next one downloads while the current one renders."
        actions={
          <>
            {finished.length > 0 && (
              <Button loading={busy === 'clear'} onClick={() => void act('clear', () => api.queueAction('clear-finished'))}>
                Clear finished
              </Button>
            )}
            <Button
              variant="primary"
              loading={busy === 'toggle'}
              disabled={!state}
              onClick={() => void act('toggle', () => api.queueAction(state?.paused ? 'resume' : 'pause'))}
            >
              {state?.paused ? <PlayIcon /> : <PauseIcon />}
              {state?.paused ? 'Resume' : 'Pause'}
            </Button>
          </>
        }
      />

      <div className="flex flex-col gap-4">
        {state?.paused && (
          <Alert tone="warning" title="Queue is paused">
            Nothing new will start. A video that is already rendering keeps going.
          </Alert>
        )}
        {error && <ErrorNote error={error} onDismiss={() => setError(null)} />}

        <Card title="Add to queue">
          <form onSubmit={add} className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_220px]">
            <Field label="Video URLs (one per line)" htmlFor="queue-urls" className="sm:row-span-2">
              <Textarea
                id="queue-urls"
                rows={4}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder={'https://youtube.com/watch?v=...\nhttps://youtube.com/watch?v=...'}
                spellCheck={false}
              />
            </Field>
            <Field label="Template" htmlFor="queue-template">
              <Select id="queue-template" value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="auto">Auto</option>
                {templates.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.label}
                  </option>
                ))}
              </Select>
            </Field>
            <div className="self-end">
              <Button type="submit" variant="primary" loading={adding} disabled={!text.trim()} className="w-full">
                Add to queue
              </Button>
            </div>
          </form>
        </Card>

        <Card bodyClassName="p-0">
          {!state && !error && <LoadingBlock label="Loading queue" />}
          {state && items.length === 0 && (
            <EmptyState title="The queue is empty" description="Add video links above to process them in order." />
          )}
          {items.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-[13px]">
                <thead>
                  <tr className="border-b border-line text-xs text-faint">
                    <th className="w-10 px-4 py-2 font-medium">#</th>
                    <th className="px-3 py-2 font-medium">Video</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="w-48 px-3 py-2 font-medium">Progress</th>
                    <th className="px-4 py-2 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, n) => {
                    const open = !FINISHED.includes(item.status)
                    const label = item.title || item.url
                    return (
                      <tr key={item.id} className="border-b border-line align-top last:border-0">
                        <td className="px-4 py-2.5 tabular-nums text-faint">{n + 1}</td>
                        <td className="max-w-[340px] px-3 py-2.5">
                          <div className="truncate font-medium" title={label}>
                            {label}
                          </div>
                          <div className="truncate text-xs text-faint">
                            {item.template}
                            {item.origin !== 'manual' && ` · ${item.origin.split(':')[0]}`}
                          </div>
                          {item.error && <p className="mt-1 text-xs text-bad">{item.error}</p>}
                        </td>
                        <td className="px-3 py-2.5">
                          <StatusBadge status={item.status} />
                        </td>
                        <td className="px-3 py-2.5">
                          {item.status === 'running' ? (
                            <div className="flex flex-col gap-1">
                              <ProgressBar value={item.progress} size="sm" />
                              <span className="text-xs tabular-nums text-muted">
                                {Math.round(item.progress * 100)}%{item.stage ? ` · ${item.stage}` : ''}
                              </span>
                            </div>
                          ) : item.status === 'downloading' ? (
                            <ProgressBar value={null} size="sm" />
                          ) : (
                            <span className="text-xs text-faint">-</span>
                          )}
                        </td>
                        <td className="px-4 py-2">
                          <div className="flex items-center justify-end gap-0.5">
                            {item.job_id && (
                              <Link
                                to={item.status === 'done' ? `/jobs/${item.job_id}/clips` : `/jobs/${item.job_id}`}
                                aria-label="Open job"
                                title="Open job"
                                className={buttonClass('ghost', 'sm', 'w-7 !px-0')}
                              >
                                <ChevronRightIcon />
                              </Link>
                            )}
                            {open && (
                              <>
                                <IconButton
                                  label="Move up"
                                  disabled={busy !== null}
                                  onClick={() => void act(`${item.id}-up`, () => api.queueMove(item.id, 'up'))}
                                >
                                  <ArrowUpIcon />
                                </IconButton>
                                <IconButton
                                  label="Move down"
                                  disabled={busy !== null}
                                  onClick={() => void act(`${item.id}-down`, () => api.queueMove(item.id, 'down'))}
                                >
                                  <ArrowDownIcon />
                                </IconButton>
                              </>
                            )}
                            {(item.status === 'failed' || item.status === 'cancelled') && (
                              <IconButton
                                label="Retry"
                                disabled={busy !== null}
                                onClick={() => void act(`${item.id}-retry`, () => api.queueRetry(item.id))}
                              >
                                <RefreshIcon />
                              </IconButton>
                            )}
                            <IconButton
                              label={item.status === 'running' ? 'Cancel and remove' : 'Remove'}
                              disabled={busy !== null}
                              onClick={() => void act(`${item.id}-rm`, () => api.queueRemove(item.id))}
                              className="hover:!text-bad"
                            >
                              <TrashIcon />
                            </IconButton>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </div>
  )
}
