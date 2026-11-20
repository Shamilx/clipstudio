import { useState } from 'react'

import type { QaResult } from '../qualityApi'
import { badgeClass } from '../ui'

/** Self-QA outcome of the pipeline: pass, or warn with the remaining issues on hover/tap. */
export function QaBadge({ qa }: { qa?: QaResult | null }) {
  const [open, setOpen] = useState(false)
  if (!qa) return null
  const pass = qa.status === 'pass'
  return (
    <span
      className="relative inline-block"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        aria-expanded={open}
        data-testid="qa-badge"
        className={badgeClass(pass ? 'success' : 'warning')}
      >
        QA {pass ? 'pass' : 'warn'}
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-0 top-full z-20 mt-1 block w-60 max-w-[80vw] rounded-control border border-line-strong bg-surface-2 p-2 text-xs leading-snug text-fg shadow-menu"
        >
          {qa.issues.length === 0 ? (
            'Frames checked: no problems.'
          ) : (
            <ul className="space-y-1">
              {qa.issues.map((issue, i) => (
                <li key={i}>{issue.message}</li>
              ))}
            </ul>
          )}
          {qa.fixes.length > 0 && <span className="mt-1 block text-muted">Fixed: {qa.fixes.join(', ')}</span>}
        </span>
      )}
    </span>
  )
}
