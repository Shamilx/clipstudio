import {
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'

import { XIcon } from './icons'
import { cn } from './util'

export { ToastProvider, useToast } from './Toast'
export { cn, relativeTime, parseUtc } from './util'

/* ----------------------------------------------------------------- Spinner */

export function Spinner({ size = 14, className }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      className={cn('animate-spin', className)}
      aria-hidden
    >
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M14 8a6 6 0 0 0-6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

/* ------------------------------------------------------------------ Button */

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md'

export function buttonClass(variant: Variant = 'secondary', size: Size = 'md', extra?: string) {
  return cn(
    'inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-control border font-medium transition-colors',
    'disabled:cursor-not-allowed disabled:opacity-50',
    size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-[13px]',
    variant === 'primary' &&
      'border-transparent bg-accent text-white hover:bg-accent-hover disabled:hover:bg-accent',
    variant === 'secondary' &&
      'border-line-strong bg-surface-2 text-fg hover:border-faint disabled:hover:border-line-strong',
    variant === 'ghost' &&
      'border-transparent bg-transparent text-muted hover:bg-surface-2 hover:text-fg disabled:hover:bg-transparent',
    variant === 'danger' &&
      'border-bad/40 bg-transparent text-bad hover:bg-bad/10 disabled:hover:bg-transparent',
    extra,
  )
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading,
  disabled,
  className,
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      disabled={disabled || loading}
      className={buttonClass(variant, size, className)}
      {...rest}
    >
      {loading && <Spinner size={12} />}
      {children}
    </button>
  )
}

export function IconButton({
  label,
  variant = 'ghost',
  size = 'sm',
  className,
  children,
  ...rest
}: Omit<ButtonProps, 'aria-label' | 'title'> & { label: string }) {
  return (
    <Button
      variant={variant}
      size={size}
      aria-label={label}
      title={label}
      className={cn(size === 'sm' ? 'w-7 !px-0' : 'w-8 !px-0', className)}
      {...rest}
    >
      {children}
    </Button>
  )
}

/** Two-step destructive button: first click arms it, second click confirms. */
export function ConfirmButton({
  onConfirm,
  children,
  confirmLabel = 'Confirm',
  variant = 'danger',
  size = 'md',
  loading,
  disabled,
  className,
  ...rest
}: Omit<ButtonProps, 'onClick'> & { onConfirm: () => void; confirmLabel?: string }) {
  const [armed, setArmed] = useState(false)

  useEffect(() => {
    if (!armed) return
    const t = window.setTimeout(() => setArmed(false), 4000)
    return () => window.clearTimeout(t)
  }, [armed])

  return (
    <Button
      variant={armed ? 'danger' : variant}
      size={size}
      loading={loading}
      disabled={disabled}
      className={cn(armed && '!bg-bad/15', className)}
      onBlur={() => setArmed(false)}
      onClick={() => {
        if (armed) {
          setArmed(false)
          onConfirm()
        } else {
          setArmed(true)
        }
      }}
      {...rest}
    >
      {armed ? confirmLabel : children}
    </Button>
  )
}

/* ------------------------------------------------------------------ Inputs */

const controlBase =
  'w-full rounded-control border border-line-strong bg-surface-2 px-2.5 text-[13px] text-fg placeholder:text-faint transition-colors hover:border-faint focus:border-accent focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50'

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={cn(controlBase, 'h-8', rest.type === 'number' && 'tabular-nums', className)}
      {...rest}
    />
  )
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(controlBase, 'py-2 leading-relaxed', className)} {...rest} />
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(controlBase, 'h-8 cursor-pointer', className)} {...rest}>
      {children}
    </select>
  )
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean
  onChange: (checked: boolean) => void
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'border-accent bg-accent' : 'border-line-strong bg-surface-2',
      )}
    >
      <span
        className={cn(
          'inline-block size-3.5 rounded-full bg-white transition-transform',
          checked ? 'translate-x-[18px]' : 'translate-x-[2px]',
        )}
      />
    </button>
  )
}

/** A switch with a label and hint on its left, as used in option lists. */
export function SwitchRow({
  label,
  hint,
  checked,
  onChange,
  disabled,
}: {
  label: string
  hint?: ReactNode
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <div className="min-w-0">
        <div className="text-[13px] text-fg">{label}</div>
        {hint && <div className="text-xs text-faint">{hint}</div>}
      </div>
      <Switch checked={checked} onChange={onChange} label={label} disabled={disabled} />
    </div>
  )
}

export function Field({
  label,
  hint,
  error,
  htmlFor,
  className,
  children,
}: {
  label: string
  hint?: ReactNode
  error?: string | null
  htmlFor?: string
  className?: string
  children: ReactNode
}) {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={htmlFor} className="text-xs font-medium text-muted">
        {label}
      </label>
      {children}
      {hint && !error && <p className="text-xs text-faint">{hint}</p>}
      {error && <p className="text-xs text-bad">{error}</p>}
    </div>
  )
}

/* ------------------------------------------------------------------- Cards */

export function Card({
  title,
  actions,
  children,
  className,
  bodyClassName,
  id,
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  id?: string
}) {
  return (
    <section id={id} className={cn('rounded-card border border-line bg-surface', className)}>
      {(title || actions) && (
        <header className="flex min-h-11 items-center justify-between gap-3 border-b border-line px-4 py-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn('p-4', bodyClassName)}>{children}</div>
    </section>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="truncate text-xl font-semibold" title={typeof title === 'string' ? title : undefined}>
          {title}
        </h1>
        {subtitle && <div className="mt-0.5 text-[13px] text-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center px-4 py-10 text-center">
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="mt-1 max-w-md text-[13px] text-muted">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function LoadingBlock({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-8 text-[13px] text-muted" role="status">
      <Spinner />
      {label}
    </div>
  )
}

/* ------------------------------------------------------------------ Badges */

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger'

export function badgeClass(tone: Tone = 'neutral', extra?: string) {
  return cn(
    'inline-flex items-center gap-1 whitespace-nowrap rounded-[4px] border px-1.5 text-[11px] font-medium leading-[18px] tabular-nums',
    tone === 'neutral' && 'border-line-strong bg-surface-2 text-muted',
    tone === 'info' && 'border-accent/40 bg-accent/10 text-[#7cb0ff]',
    tone === 'success' && 'border-ok/40 bg-ok/10 text-ok',
    tone === 'warning' && 'border-warn/40 bg-warn/10 text-warn',
    tone === 'danger' && 'border-bad/40 bg-bad/10 text-bad',
    extra,
  )
}

export function Badge({
  tone = 'neutral',
  className,
  children,
  title,
}: {
  tone?: Tone
  className?: string
  children: ReactNode
  title?: string
}) {
  return (
    <span className={badgeClass(tone, className)} title={title}>
      {children}
    </span>
  )
}

const STATUS: Record<string, [Tone, string]> = {
  queued: ['neutral', 'Queued'],
  pending: ['neutral', 'Pending'],
  candidate: ['neutral', 'Pending'],
  running: ['info', 'Running'],
  downloading: ['info', 'Downloading'],
  ready: ['info', 'Downloaded'],
  done: ['success', 'Done'],
  kept: ['success', 'Approved'],
  exported: ['success', 'Exported'],
  failed: ['danger', 'Failed'],
  discarded: ['danger', 'Rejected'],
  cancelled: ['neutral', 'Cancelled'],
}

export function StatusBadge({ status }: { status: string }) {
  const [tone, label] = STATUS[status] ?? ['neutral', status]
  return <Badge tone={tone}>{label}</Badge>
}

/* ---------------------------------------------------------------- Progress */

/** value is 0..1; null renders an indeterminate bar. */
export function ProgressBar({
  value,
  size = 'md',
  tone = 'accent',
  className,
  label,
}: {
  value: number | null
  size?: 'sm' | 'md'
  tone?: 'accent' | 'success' | 'danger'
  className?: string
  label?: string
}) {
  const color = tone === 'success' ? 'bg-ok' : tone === 'danger' ? 'bg-bad' : 'bg-accent'
  return (
    <div
      className={cn('w-full overflow-hidden rounded-full bg-surface-2', size === 'sm' ? 'h-1' : 'h-1.5', className)}
      role="progressbar"
      aria-label={label ?? 'Progress'}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value === null ? undefined : Math.round(value * 100)}
    >
      {value === null ? (
        <div className={cn('h-full w-1/3 animate-pulse rounded-full', color)} />
      ) : (
        <div
          className={cn('h-full rounded-full transition-[width] duration-500', color)}
          style={{ width: `${Math.round(Math.min(1, Math.max(0, value)) * 100)}%` }}
        />
      )}
    </div>
  )
}

/* -------------------------------------------------------------------- Tabs */

export function Tabs<T extends string>({
  items,
  value,
  onChange,
  ariaLabel,
  className,
}: {
  items: { value: T; label: string; count?: number }[]
  value: T
  onChange: (value: T) => void
  ariaLabel?: string
  className?: string
}) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={cn('inline-flex rounded-control border border-line bg-surface p-0.5', className)}
    >
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          aria-selected={item.value === value}
          onClick={() => onChange(item.value)}
          className={cn(
            'inline-flex h-7 items-center gap-1.5 rounded-[5px] px-3 text-xs font-medium transition-colors',
            item.value === value ? 'bg-surface-2 text-fg' : 'text-muted hover:text-fg',
          )}
        >
          {item.label}
          {item.count !== undefined && (
            <span className="tabular-nums text-faint">{item.count}</span>
          )}
        </button>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------- Alert */

export function Alert({
  tone = 'danger',
  title,
  children,
  onDismiss,
  className,
}: {
  tone?: 'info' | 'success' | 'warning' | 'danger'
  title?: ReactNode
  children?: ReactNode
  onDismiss?: () => void
  className?: string
}) {
  return (
    <div
      role={tone === 'danger' ? 'alert' : 'status'}
      className={cn(
        'flex items-start justify-between gap-3 rounded-control border px-3 py-2 text-[13px]',
        tone === 'danger' && 'border-bad/40 bg-bad/10',
        tone === 'warning' && 'border-warn/40 bg-warn/10',
        tone === 'success' && 'border-ok/40 bg-ok/10',
        tone === 'info' && 'border-accent/40 bg-accent/10',
        className,
      )}
    >
      <div className="min-w-0">
        {title && <p className="font-medium text-fg">{title}</p>}
        {children && (
          <div className={cn('whitespace-pre-line text-muted', title && 'mt-0.5')}>{children}</div>
        )}
      </div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss"
          className="shrink-0 rounded p-0.5 text-muted hover:text-fg"
        >
          <XIcon size={14} />
        </button>
      )}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex min-w-5 items-center justify-center rounded border border-line-strong bg-surface-2 px-1 font-mono text-[11px] text-muted">
      {children}
    </kbd>
  )
}

/** Small hover/click popover wrapper used for quality tooltips. */
export function useOutsideClose(onClose: () => void, active: boolean) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!active) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [active, onClose])
  return ref
}
