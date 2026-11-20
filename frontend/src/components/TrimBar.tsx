import { useCallback, useEffect, useRef, useState } from 'react'

import { formatTimecode, type Word } from '../api'

/**
 * Trim handles that snap to word boundaries.
 *
 * Snapping is the whole point: a cut placed mid-word is always wrong, and free
 * dragging makes it the default outcome. The handles move continuously and
 * settle onto the nearest word edge on release.
 */
export function TrimBar({
  words,
  startS,
  endS,
  originalStart,
  originalEnd,
  onCommit,
}: {
  words: Word[]
  startS: number
  endS: number
  originalStart: number
  originalEnd: number
  onCommit: (start: number, end: number) => void
}) {
  const track = useRef<HTMLDivElement>(null)
  const [dragging, setDragging] = useState<'start' | 'end' | null>(null)
  const [local, setLocal] = useState<{ start: number; end: number }>({ start: startS, end: endS })

  useEffect(() => setLocal({ start: startS, end: endS }), [startS, endS])

  // Padding either side so a trim can be widened as well as tightened.
  const windowStart = originalStart - 5
  const windowEnd = originalEnd + 5
  const span = Math.max(0.1, windowEnd - windowStart)

  const toFraction = (t: number) => (t - windowStart) / span
  const toTime = (fraction: number) => windowStart + fraction * span

  const snap = useCallback(
    (t: number, edge: 'start' | 'end') => {
      if (words.length === 0) return t
      const candidates = words.map((w) => (edge === 'start' ? w.start : w.end))
      let best = candidates[0]
      for (const candidate of candidates) {
        if (Math.abs(candidate - t) < Math.abs(best - t)) best = candidate
      }
      // Only snap when a word edge is genuinely nearby; otherwise the handle
      // would jump somewhere the user didn't point at.
      return Math.abs(best - t) < 0.5 ? best : t
    },
    [words],
  )

  // Latest values for the window listeners, which are attached once per drag.
  const latest = useRef({ local, snap, onCommit, startS, endS, toTime })
  latest.current = { local, snap, onCommit, startS, endS, toTime }

  // Listeners are attached synchronously on pointerdown (not in an effect), so a
  // quick drag cannot release before they exist.
  const startDrag = (edge: 'start' | 'end', clientX?: number) => {
    setDragging(edge)
    let current = latest.current.local

    const update = (x: number) => {
      const rect = track.current?.getBoundingClientRect()
      if (!rect) return
      const fraction = Math.min(1, Math.max(0, (x - rect.left) / rect.width))
      const time = latest.current.toTime(fraction)
      current =
        edge === 'start'
          ? { ...current, start: Math.min(time, current.end - 1) }
          : { ...current, end: Math.max(time, current.start + 1) }
      setLocal(current)
    }
    if (clientX !== undefined) update(clientX)

    const move = (event: PointerEvent) => update(event.clientX)
    const release = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', release)
      window.removeEventListener('pointercancel', release)
      const { snap: snapTo, onCommit: commit, startS: s0, endS: e0 } = latest.current
      const snapped = { start: snapTo(current.start, 'start'), end: snapTo(current.end, 'end') }
      setLocal(snapped)
      setDragging(null)
      if (Math.abs(snapped.start - s0) > 0.01 || Math.abs(snapped.end - e0) > 0.01) {
        commit(snapped.start, snapped.end)
      }
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', release)
    window.addEventListener('pointercancel', release)
  }

  // Clicking the track moves the nearer handle there.
  const onTrackDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget && !(event.target as HTMLElement).dataset.track) return
    const rect = track.current?.getBoundingClientRect()
    if (!rect) return
    event.preventDefault()
    const time = toTime((event.clientX - rect.left) / rect.width)
    const edge = Math.abs(time - local.start) <= Math.abs(time - local.end) ? 'start' : 'end'
    startDrag(edge, event.clientX)
  }

  const left = toFraction(local.start)
  const right = toFraction(local.end)
  const duration = local.end - local.start

  return (
    <div className="select-none">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-muted">Trim</span>
        <span className="text-xs tabular-nums text-muted">
          {formatTimecode(local.start)} to {formatTimecode(local.end)}
          <span className="ml-3 text-fg">{duration.toFixed(1)}s</span>
        </span>
      </div>

      <div
        ref={track}
        onPointerDown={onTrackDown}
        data-track="1"
        className={`relative mt-2 h-12 cursor-ew-resize touch-none rounded-control bg-surface-2 ${dragging ? 'ring-1 ring-accent' : ''}`}
      >
        {/* Word density as a crude waveform stand-in: taller where speech is
            dense, which is enough to aim a cut by. */}
        <div
          data-track="1"
          className="absolute inset-0 flex items-end gap-px overflow-hidden rounded-control px-px opacity-70"
        >
          {buildDensity(words, windowStart, windowEnd, 90).map((value, index) => (
            <div
              key={index}
              className="flex-1 bg-line-strong"
              style={{ height: `${12 + value * 78}%` }}
            />
          ))}
        </div>

        <div
          data-track="1"
          className="absolute inset-y-0 bg-accent/20"
          style={{ left: `${left * 100}%`, right: `${(1 - right) * 100}%` }}
        />

        <Handle position={left} edge="start" onGrab={() => startDrag('start')} />
        <Handle position={right} edge="end" onGrab={() => startDrag('end')} />
      </div>
    </div>
  )
}

function Handle({
  position,
  edge,
  onGrab,
}: {
  position: number
  edge: 'start' | 'end'
  onGrab: () => void
}) {
  return (
    <button
      onPointerDown={(e) => {
        e.preventDefault()
        onGrab()
      }}
      aria-label={`Drag ${edge} of clip`}
      className="absolute inset-y-0 w-6 -translate-x-1/2 cursor-ew-resize touch-none"
      style={{ left: `${position * 100}%` }}
    >
      <span className="absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-accent" />
      <span className="absolute left-1/2 top-1/2 h-6 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent" />
    </button>
  )
}

/** Words per bucket, normalised — a proxy for speech density. */
function buildDensity(words: Word[], start: number, end: number, buckets: number): number[] {
  const values = new Array(buckets).fill(0)
  const span = Math.max(0.1, end - start)

  for (const word of words) {
    const index = Math.floor(((word.start - start) / span) * buckets)
    if (index >= 0 && index < buckets) values[index] += 1
  }

  const peak = Math.max(1, ...values)
  return values.map((value) => value / peak)
}
