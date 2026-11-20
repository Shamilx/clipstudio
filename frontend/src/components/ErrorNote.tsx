import { ApiError } from '../api'
import { Alert } from '../ui'

/** A failure and, where the server offered one, the fix. */
export function ErrorNote({ error, onDismiss }: { error: Error; onDismiss?: () => void }) {
  const hint = error instanceof ApiError ? error.hint : ''
  return (
    <Alert tone="danger" title={error.message} onDismiss={onDismiss}>
      {hint}
    </Alert>
  )
}
