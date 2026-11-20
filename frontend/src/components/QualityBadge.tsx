import { useState } from 'react'

import type { QualityReport } from '../qualityApi'
import { badgeClass } from '../ui'

const LOOK = {
  ready: { label: 'Ready', tone: 'success' },
  review: { label: 'Check', tone: 'warning' },
  rejected: { label: 'Rejected', tone: 'danger' },
} as const

/** Status pill; the issue list shows on hover, or on tap/focus for touch screens. */
export function QualityBadge({ report }: { report?: QualityReport | null }) {
  const [open, setOpen] = useState(false)
  if (!report || report.status === 'qa') return null
  // Unknown statuses (e.g. newer QA states) must never crash the page.
  const look =
    LOOK[report.status as keyof typeof LOOK] ??
    ({ label: String(report.status || 'QA'), tone: 'neutral' } as const)
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
        className={badgeClass(look.tone)}
      >
        {look.label} {report.score}
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-0 top-full z-20 mt-1 block w-60 max-w-[80vw] rounded-control border border-line-strong bg-surface-2 p-2 text-xs leading-snug text-fg shadow-menu"
        >
          {report.issues.length === 0 ? (
            'No issues found.'
          ) : (
            <ul className="space-y-1">
              {report.issues.map((issue, i) => (
                <li key={i} className={issue.severity === 'error' ? 'text-bad' : ''}>
                  {issue.message}
                </li>
              ))}
            </ul>
          )}
        </span>
      )}
    </span>
  )
}
