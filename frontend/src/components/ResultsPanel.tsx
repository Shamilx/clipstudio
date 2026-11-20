import { useCallback, useEffect, useState } from 'react'

import { api, type Clip, type ClipResult, type ResultPlatform } from '../api'
import { Button, Field, Input, Select, relativeTime, useToast } from '../ui'
import { XIcon } from '../ui/icons'

const PLATFORMS: { value: ResultPlatform; label: string }[] = [
  { value: 'youtube', label: 'YouTube Shorts' },
  { value: 'tiktok', label: 'TikTok' },
  { value: 'instagram', label: 'Instagram Reels' },
]

function num(value: string): number | null {
  const n = parseFloat(value)
  return Number.isFinite(n) ? n : null
}

/** Record how a posted clip performed; the best and worst hooks teach the hook writers. */
export function ResultsPanel({ clip }: { clip: Clip }) {
  const toast = useToast()
  const [results, setResults] = useState<ClipResult[]>([])
  const [platform, setPlatform] = useState<ResultPlatform>('youtube')
  const [hookChoice, setHookChoice] = useState<'active' | 'alt'>('active')
  const [views, setViews] = useState('')
  const [viewed, setViewed] = useState('')
  const [avgView, setAvgView] = useState('')
  const [days, setDays] = useState('1')
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setResults(await api.listResults(clip.id))
    } catch {
      setResults([])
    }
  }, [clip.id])

  useEffect(() => {
    void load()
  }, [load])

  const alt = clip.hook_alt ?? ''
  const viewedPct = num(viewed)
  const avgPct = num(avgView)
  const invalid =
    num(views) === null ||
    (viewed !== '' && (viewedPct === null || viewedPct < 0 || viewedPct > 100)) ||
    (avgView !== '' && (avgPct === null || avgPct < 0 || avgPct > 100))

  const add = async () => {
    setSaving(true)
    try {
      await api.addResult(clip.id, {
        platform,
        views: Math.max(0, Math.round(num(views) ?? 0)),
        viewed_pct: viewedPct,
        avg_view_pct: avgPct,
        days_live: Math.max(1, Math.round(num(days) ?? 1)),
        hook: hookChoice === 'alt' && alt ? alt : clip.hook,
      })
      setViews('')
      setViewed('')
      setAvgView('')
      toast.success('Result saved')
      await load()
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (id: string) => {
    try {
      await api.deleteResult(clip.id, id)
      await load()
    } catch (err) {
      toast.error(err as Error)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="text-xs font-medium text-muted">Results</p>
        <p className="mt-0.5 text-xs text-faint">
          Log how this clip did after posting. With 3 or more results, the best and worst hooks guide
          future hooks.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Field label="Platform" htmlFor="res-platform">
          <Select
            id="res-platform"
            value={platform}
            onChange={(e) => setPlatform(e.target.value as ResultPlatform)}
          >
            {PLATFORMS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Views" htmlFor="res-views">
          <Input
            id="res-views"
            inputMode="numeric"
            value={views}
            onChange={(e) => setViews(e.target.value)}
            placeholder="1200"
          />
        </Field>
        <Field label="Viewed vs swiped (%)" htmlFor="res-viewed" hint="YouTube Shorts metric">
          <Input
            id="res-viewed"
            inputMode="decimal"
            value={viewed}
            onChange={(e) => setViewed(e.target.value)}
            placeholder="optional"
          />
        </Field>
        <Field label="Avg view duration (%)" htmlFor="res-avg">
          <Input
            id="res-avg"
            inputMode="decimal"
            value={avgView}
            onChange={(e) => setAvgView(e.target.value)}
            placeholder="optional"
          />
        </Field>
        <Field label="Days live" htmlFor="res-days">
          <Input id="res-days" inputMode="numeric" value={days} onChange={(e) => setDays(e.target.value)} />
        </Field>
        {alt && (
          <Field label="Hook used" htmlFor="res-hook">
            <Select
              id="res-hook"
              value={hookChoice}
              onChange={(e) => setHookChoice(e.target.value as 'active' | 'alt')}
            >
              <option value="active">Active hook</option>
              <option value="alt">Alternative hook</option>
            </Select>
          </Field>
        )}
      </div>
      <div>
        <Button variant="primary" onClick={add} loading={saving} disabled={invalid}>
          Add result
        </Button>
      </div>

      {results.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {results.map((r) => (
            <li
              key={r.id}
              className="flex items-center gap-2 rounded-control border border-line px-3 py-2 text-[13px]"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate">
                  <span className="font-medium capitalize">{r.platform}</span>
                  {' - '}
                  {r.views.toLocaleString()} views
                  {r.viewed_pct != null && `, ${r.viewed_pct}% viewed`}
                  {r.avg_view_pct != null && `, ${r.avg_view_pct}% avg watched`}
                </p>
                <p className="truncate text-xs text-faint">
                  &ldquo;{r.hook}&rdquo; - {relativeTime(r.recorded_at)}
                </p>
              </div>
              <button
                type="button"
                className="rounded p-1 text-faint hover:text-bad"
                onClick={() => remove(r.id)}
                aria-label="Delete result"
              >
                <XIcon size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
