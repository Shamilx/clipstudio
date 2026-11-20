import { useEffect, useState } from 'react'

import { formatTimecode, type Word } from '../api'
import { Button } from '../ui'

/**
 * Word-level caption editing.
 *
 * The job here is fixing what Whisper misheard: names, jargon, acronyms. Only
 * the text is editable; timings stay locked to the measured audio, because a
 * hand-typed timestamp is how captions drift out of sync.
 */
export function CaptionEditor({
  words,
  onChange,
  onSave,
  saving,
  dirty,
}: {
  words: Word[]
  onChange: (words: Word[]) => void
  onSave: () => void
  saving: boolean
  dirty: boolean
}) {
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  useEffect(() => setEditing(null), [words.length])

  const commit = (index: number) => {
    const text = draft.trim()
    if (text && text !== words[index].text) {
      const next = words.map((word, i) => (i === index ? { ...word, text } : word))
      onChange(next)
    }
    setEditing(null)
  }

  if (words.length === 0) {
    return <p className="text-[13px] text-muted">No transcript for this clip yet.</p>
  }

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-muted">Transcript</span>
        <span className="text-xs tabular-nums text-faint">{words.length} words</span>
      </div>

      <div className="mt-2 max-h-[45vh] overflow-y-auto rounded-control border border-line bg-bg p-3 text-[13px] leading-[1.9]">
        {words.map((word, index) =>
          editing === index ? (
            <input
              key={index}
              autoFocus
              aria-label="Edit word"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => commit(index)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit(index)
                if (e.key === 'Escape') setEditing(null)
                if (e.key === 'Tab') {
                  e.preventDefault()
                  commit(index)
                  const next = index + (e.shiftKey ? -1 : 1)
                  if (next >= 0 && next < words.length) {
                    setDraft(words[next].text)
                    setEditing(next)
                  }
                }
              }}
              className="mr-1 rounded border border-accent bg-surface-2 px-1 text-fg outline-none"
              style={{ width: `${Math.max(4, draft.length + 2)}ch` }}
            />
          ) : (
            <button
              key={index}
              type="button"
              onClick={() => {
                setDraft(word.text)
                setEditing(index)
              }}
              title={formatTimecode(word.start)}
              className="mr-0.5 rounded px-0.5 text-fg transition-colors hover:bg-accent/20"
            >
              {word.text}
            </button>
          ),
        )}
      </div>

      <div className="mt-3 flex items-center gap-3">
        <Button variant="primary" onClick={onSave} disabled={!dirty} loading={saving}>
          Save captions
        </Button>
        <span className="text-xs text-faint">
          {dirty ? 'Unsaved changes' : 'Click any word to correct it'}
        </span>
      </div>
    </div>
  )
}
