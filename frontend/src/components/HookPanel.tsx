import { useEffect, useState } from 'react'

import { api, type Clip } from '../api'
import { Badge, Button, Input, useToast } from '../ui'
import { CopyIcon } from '../ui/icons'

const FORMULA_LABELS: Record<string, string> = {
  curiosity: 'Curiosity question',
  claim: 'Bold claim',
  stakes: 'Stakes / number',
  in_medias_res: 'In the action',
  contrast: 'Contrast',
}

const CRITERIA_LABELS: Record<string, string> = {
  specificity: 'Specific',
  curiosity: 'Curious',
  readability: 'Readable',
  accuracy: 'Accurate',
  brief: 'Brief',
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    area.remove()
  }
}

/** Active on-screen hook (editable), the alternative with swap/copy, and all judged candidates. */
export function HookPanel({ clip, onSaved }: { clip: Clip; onSaved: (clip: Clip) => void }) {
  const toast = useToast()
  const [hook, setHook] = useState(clip.hook)
  const [saving, setSaving] = useState(false)
  const [swapping, setSwapping] = useState(false)
  const [showAll, setShowAll] = useState(false)

  useEffect(() => {
    setHook(clip.hook)
  }, [clip.id, clip.hook])

  const alt = clip.hook_alt ?? ''
  const candidates = clip.hook_candidates ?? []
  const dirty = hook.trim() !== clip.hook
  const words = hook.trim().split(/\s+/).filter(Boolean).length
  const tooLong = words > 8 || hook.trim().length > 64

  const save = async () => {
    if (!dirty || !hook.trim()) return
    setSaving(true)
    try {
      onSaved(await api.patchClip(clip.id, { hook: hook.trim() }))
      toast.success('Hook saved. Export again to update the final video.')
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setSaving(false)
    }
  }

  const swap = async () => {
    setSwapping(true)
    try {
      onSaved(await api.swapHook(clip.id))
      toast.success('Hook swapped. Export again to update the final video.')
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setSwapping(false)
    }
  }

  const useCandidate = async (text: string) => {
    try {
      onSaved(await api.patchClip(clip.id, { hook: text }))
      toast.success('Hook changed. Export again to update the final video.')
    } catch (err) {
      toast.error(err as Error)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <label htmlFor="meta-hook" className="text-xs font-medium text-muted">
          Hook (on screen in the first seconds)
        </label>
        <Button
          size="sm"
          variant="ghost"
          onClick={async () => {
            await copyText(hook)
            toast.success('Copied')
          }}
          disabled={!hook}
          aria-label="Copy hook"
        >
          <CopyIcon size={14} />
          Copy
        </Button>
      </div>
      <div className="flex items-center gap-2">
        <Input
          id="meta-hook"
          value={hook}
          maxLength={80}
          onChange={(e) => setHook(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && save()}
        />
        <Button variant="primary" onClick={save} loading={saving} disabled={!dirty || !hook.trim()}>
          Save
        </Button>
      </div>
      {tooLong && (
        <p className="text-xs text-warn">
          Long hooks are hard to read in 1.5 seconds. Aim for 8 words or fewer.
        </p>
      )}

      {alt && (
        <div className="flex items-center gap-2 rounded-control border border-line bg-surface-2/40 px-3 py-2">
          <div className="min-w-0 flex-1">
            <p className="text-[11px] font-medium uppercase tracking-wide text-faint">Alternative</p>
            <p className="break-words text-[13px]">{alt}</p>
          </div>
          <Button size="sm" onClick={swap} loading={swapping}>
            Use this
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={async () => {
              await copyText(alt)
              toast.success('Copied')
            }}
            aria-label="Copy alternative hook"
          >
            <CopyIcon size={14} />
            Copy
          </Button>
        </div>
      )}
      <p className="text-xs text-faint">Use a different hook for each platform.</p>

      {candidates.length > 0 && (
        <div>
          <button
            type="button"
            className="text-xs font-medium text-muted underline-offset-2 hover:underline"
            onClick={() => setShowAll((v) => !v)}
            aria-expanded={showAll}
          >
            {showAll ? 'Hide' : 'Show'} all {candidates.length} candidates
          </button>
          {showAll && (
            <ul className="mt-2 flex flex-col gap-1.5">
              {[...candidates]
                .sort((a, b) => (b.score ?? -1) - (a.score ?? -1))
                .map((c) => (
                  <li
                    key={c.text}
                    className="flex items-start gap-2 rounded-control border border-line px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <p className={c.rejected ? 'break-words text-[13px] text-faint line-through' : 'break-words text-[13px]'}>
                        {c.text}
                      </p>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <Badge>{FORMULA_LABELS[c.formula] ?? (c.formula || 'Hook')}</Badge>
                        {c.rejected ? (
                          <span className="text-[11px] text-bad">Rejected: {c.rejected}</span>
                        ) : (
                          Object.entries(c.scores ?? {}).map(([k, v]) => (
                            <span key={k} className="text-[11px] text-faint">
                              {CRITERIA_LABELS[k] ?? k} {v}
                            </span>
                          ))
                        )}
                      </div>
                    </div>
                    <span className="shrink-0 text-xs font-medium tabular-nums text-muted">
                      {c.score != null ? c.score.toFixed(1) : '-'}
                    </span>
                    {!c.rejected && c.text !== clip.hook && (
                      <Button size="sm" variant="ghost" onClick={() => useCandidate(c.text)}>
                        Use
                      </Button>
                    )}
                  </li>
                ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
