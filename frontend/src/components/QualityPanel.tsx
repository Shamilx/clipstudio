import { useState } from 'react'

import { qualityApi, type QualityReport } from '../qualityApi'
import { Button, useToast } from '../ui'
import { DownloadIcon } from '../ui/icons'
import { QaBadge } from './QaBadge'
import { QualityBadge } from './QualityBadge'

export function QualityPanel({
  clipId,
  report,
  onReport,
}: {
  clipId: string
  report?: QualityReport | null
  onReport: (r: QualityReport) => void
}) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)

  const run = async () => {
    setBusy(true)
    try {
      onReport(await qualityApi.run(clipId))
    } catch (err) {
      toast.error((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const pack = async () => {
    try {
      await qualityApi.downloadPack(clipId)
    } catch (err) {
      toast.error(
        (err as { status?: number }).status === 404
          ? 'The download pack is not available for this clip yet.'
          : 'Could not download the pack.',
      )
    }
  }

  return (
    <div>
      <p className="text-xs font-medium text-muted">Quality check</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <QualityBadge report={report} />
        <QaBadge qa={report?.qa} />
        <Button size="sm" onClick={run} loading={busy}>
          Run quality check
        </Button>
        <Button size="sm" variant="ghost" onClick={pack}>
          <DownloadIcon size={14} />
          Download pack
        </Button>
      </div>
      {report?.qa && report.qa.issues.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-muted" data-testid="qa-issues">
          {report.qa.issues.map((issue, i) => (
            <li key={i}>
              <span className="text-warn">QA</span> {issue.message}
            </li>
          ))}
        </ul>
      )}
      {report?.qa && report.qa.fixes.length > 0 && (
        <p className="mt-2 text-xs text-muted">Auto-fixed: {report.qa.fixes.join(', ')}</p>
      )}
      {report && report.status !== 'qa' && report.issues.length > 0 && (
        <ul className="mt-3 space-y-1 text-xs text-muted">
          {report.issues.map((issue, i) => (
            <li key={i}>
              <span className={issue.severity === 'error' ? 'text-bad' : 'text-warn'}>
                {issue.severity === 'error' ? 'Problem' : 'Check'}
              </span>{' '}
              {issue.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
