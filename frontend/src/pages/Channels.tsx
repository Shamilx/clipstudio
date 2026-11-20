import { useCallback, useEffect, useState } from 'react'

import { api, type Channel, type ClipTemplate } from '../api'
import { ErrorNote } from '../components/ErrorNote'
import {
  Button,
  Card,
  ConfirmButton,
  EmptyState,
  Field,
  Input,
  LoadingBlock,
  PageHeader,
  Select,
  Switch,
  useToast,
} from '../ui'

function ago(ts: number | null) {
  if (!ts) return 'never'
  const m = Math.round((Date.now() / 1000 - ts) / 60)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  return `${Math.round(m / 60)} h ago`
}

const EMPTY = {
  url: '',
  template: 'auto',
  interval_min: 60,
  min_duration_s: '' as number | '',
  max_duration_s: '' as number | '',
  include_keywords: '',
  exclude_keywords: '',
}

function IntervalInput({ value, onCommit }: { value: number; onCommit: (v: number) => void }) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])
  return (
    <Input
      type="number"
      min={5}
      aria-label="Check interval in minutes"
      className="!h-7 w-20"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        const n = Number(draft)
        if (n > 0 && n !== value) onCommit(n)
        else setDraft(String(value))
      }}
      onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
    />
  )
}

export function Channels() {
  const toast = useToast()
  const [list, setList] = useState<Channel[] | null>(null)
  const [templates, setTemplates] = useState<ClipTemplate[]>([])
  const [form, setForm] = useState(EMPTY)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<Error | null>(null)

  const refresh = useCallback(() => {
    api
      .listChannels()
      .then((l) => {
        setList(l)
        setError(null)
      })
      .catch((e) => setError(e as Error))
  }, [])

  useEffect(() => {
    refresh()
    api.listTemplates().then(setTemplates).catch(() => undefined)
  }, [refresh])

  const set = <K extends keyof typeof EMPTY>(k: K, v: (typeof EMPTY)[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const add = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy('add')
    try {
      await api.addChannel({
        ...form,
        min_duration_s: form.min_duration_s === '' ? null : Number(form.min_duration_s),
        max_duration_s: form.max_duration_s === '' ? null : Number(form.max_duration_s),
      })
      setForm(EMPTY)
      toast.success('Channel added. Only videos uploaded from now on are queued.')
      refresh()
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const check = async (c: Channel) => {
    setBusy(c.id)
    try {
      const r = await api.checkChannel(c.id)
      toast.success(
        r.enqueued.length
          ? `Queued ${r.enqueued.length} new video(s) from ${c.title}`
          : `No new videos from ${c.title}`,
      )
      refresh()
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const remove = async (c: Channel) => {
    setBusy(`rm-${c.id}`)
    try {
      await api.removeChannel(c.id)
      toast.success('Channel removed')
      refresh()
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const patch = async (c: Channel, p: Parameters<typeof api.patchChannel>[1]) => {
    try {
      await api.patchChannel(c.id, p)
      refresh()
    } catch (err) {
      toast.error(err as Error)
    }
  }

  return (
    <div>
      <PageHeader
        title="Channels"
        subtitle="Watch a YouTube channel. New uploads (no Shorts or live streams) go to the queue automatically. Only watch channels whose content you have the rights to use."
      />

      <div className="flex flex-col gap-4">
        <Card title="Add channel">
          <form onSubmit={add} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Channel URL or @handle" htmlFor="ch-url" className="sm:col-span-2">
              <Input
                id="ch-url"
                placeholder="https://www.youtube.com/@TED"
                value={form.url}
                onChange={(e) => set('url', e.target.value)}
                spellCheck={false}
              />
            </Field>
            <Field label="Template" htmlFor="ch-template">
              <Select id="ch-template" value={form.template} onChange={(e) => set('template', e.target.value)}>
                <option value="auto">Auto</option>
                {templates.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.label}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Check every (minutes)" htmlFor="ch-interval">
              <Input
                id="ch-interval"
                type="number"
                min={5}
                value={form.interval_min}
                onChange={(e) => set('interval_min', Number(e.target.value) || 60)}
              />
            </Field>
            <Field label="Min length (s)" htmlFor="ch-min">
              <Input
                id="ch-min"
                type="number"
                value={form.min_duration_s}
                onChange={(e) => set('min_duration_s', e.target.value ? Number(e.target.value) : '')}
              />
            </Field>
            <Field label="Max length (s)" htmlFor="ch-max">
              <Input
                id="ch-max"
                type="number"
                value={form.max_duration_s}
                onChange={(e) => set('max_duration_s', e.target.value ? Number(e.target.value) : '')}
              />
            </Field>
            <Field label="Title must contain (any, comma separated)" htmlFor="ch-inc">
              <Input id="ch-inc" value={form.include_keywords} onChange={(e) => set('include_keywords', e.target.value)} />
            </Field>
            <Field label="Skip titles containing" htmlFor="ch-exc">
              <Input id="ch-exc" value={form.exclude_keywords} onChange={(e) => set('exclude_keywords', e.target.value)} />
            </Field>
            <div className="sm:col-span-2 lg:col-span-4">
              <Button type="submit" variant="primary" loading={busy === 'add'} disabled={!form.url.trim() || busy !== null}>
                Watch channel
              </Button>
            </div>
          </form>
        </Card>

        {error && <ErrorNote error={error} onDismiss={() => setError(null)} />}

        <Card bodyClassName="p-0">
          {list === null && !error && <LoadingBlock label="Loading channels" />}
          {list && list.length === 0 && (
            <EmptyState title="No channels yet" description="Add a channel above to queue its new uploads automatically." />
          )}
          {list && list.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] text-left text-[13px]">
                <thead>
                  <tr className="border-b border-line text-xs text-faint">
                    <th className="px-4 py-2 font-medium">Channel</th>
                    <th className="px-3 py-2 font-medium">Enabled</th>
                    <th className="px-3 py-2 font-medium">Interval (min)</th>
                    <th className="px-3 py-2 font-medium">Last checked</th>
                    <th className="px-3 py-2 text-right font-medium">Queued</th>
                    <th className="px-4 py-2 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((c) => {
                    const rules = [
                      c.template,
                      c.min_duration_s ? `min ${c.min_duration_s}s` : '',
                      c.max_duration_s ? `max ${c.max_duration_s}s` : '',
                      c.include_keywords ? `must contain: ${c.include_keywords}` : '',
                      c.exclude_keywords ? `skip: ${c.exclude_keywords}` : '',
                    ]
                      .filter(Boolean)
                      .join(' · ')
                    return (
                      <tr key={c.id} className="border-b border-line align-top last:border-0">
                        <td className="max-w-[320px] px-4 py-2.5">
                          <div className="truncate font-medium" title={c.title}>
                            {c.title}
                          </div>
                          <div className="truncate text-xs text-faint" title={c.url}>
                            {c.url}
                          </div>
                          <div className="truncate text-xs text-faint" title={rules}>
                            {rules}
                          </div>
                          {c.last_error && <p className="mt-1 text-xs text-bad">{c.last_error}</p>}
                        </td>
                        <td className="px-3 py-2.5">
                          <Switch
                            label={`${c.enabled ? 'Pause' : 'Enable'} ${c.title}`}
                            checked={c.enabled}
                            onChange={(v) => void patch(c, { enabled: v })}
                          />
                        </td>
                        <td className="px-3 py-2">
                          <IntervalInput value={c.interval_min} onCommit={(v) => void patch(c, { interval_min: v })} />
                        </td>
                        <td className="whitespace-nowrap px-3 py-2.5 text-muted">{ago(c.last_checked)}</td>
                        <td className="px-3 py-2.5 text-right tabular-nums">{c.enqueued_count}</td>
                        <td className="px-4 py-2">
                          <div className="flex items-center justify-end gap-1">
                            <Button size="sm" loading={busy === c.id} disabled={busy !== null} onClick={() => void check(c)}>
                              Check now
                            </Button>
                            <ConfirmButton
                              size="sm"
                              variant="ghost"
                              loading={busy === `rm-${c.id}`}
                              confirmLabel="Confirm"
                              onConfirm={() => void remove(c)}
                            >
                              Remove
                            </ConfirmButton>
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
