import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

import { CheckIcon, XIcon } from './icons'
import { cn } from './util'

interface ToastItem {
  id: number
  kind: 'success' | 'error'
  message: string
}

interface ToastApi {
  success: (message: string) => void
  error: (message: string | Error) => void
}

const ToastContext = createContext<ToastApi>({ success: () => undefined, error: () => undefined })

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const push = useCallback((kind: ToastItem['kind'], message: string) => {
    const id = nextId.current++
    setItems((current) => [...current, { id, kind, message }])
    window.setTimeout(() => setItems((current) => current.filter((t) => t.id !== id)), 3000)
  }, [])

  const api = useMemo<ToastApi>(
    () => ({
      success: (message) => push('success', message),
      error: (message) => push('error', message instanceof Error ? message.message : message),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col items-end gap-2"
        role="status"
        aria-live="polite"
      >
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              'pointer-events-auto flex max-w-sm items-start gap-2 rounded-control border bg-surface-2 px-3 py-2 text-[13px] shadow-menu',
              t.kind === 'success' ? 'border-ok/40' : 'border-bad/50',
            )}
          >
            <span className={cn('mt-0.5 shrink-0', t.kind === 'success' ? 'text-ok' : 'text-bad')}>
              {t.kind === 'success' ? <CheckIcon size={14} /> : <XIcon size={14} />}
            </span>
            <span className="min-w-0 break-words">{t.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  return useContext(ToastContext)
}
