import { useEffect, useState, type ReactNode } from 'react'
import { Link, NavLink, Navigate, Outlet, useLocation, useMatch } from 'react-router-dom'

import { api, type SetupStatus, type SystemStatus } from './api'
import { Badge, LoadingBlock, ToastProvider, buttonClass, cn } from './ui'
import { LayersIcon, ListIcon, PlusIcon, RadioIcon, SettingsIcon } from './ui/icons'

const ACTIVE_QUEUE = ['pending', 'downloading', 'ready', 'running']

export function App() {
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [systemFailed, setSystemFailed] = useState(false)
  const [queueCount, setQueueCount] = useState(0)
  const reviewPage = useMatch('/jobs/:jobId/clips')
  const { pathname } = useLocation()
  // null = not known yet. A failed check leaves it null-ish via setupChecked, so a
  // broken endpoint never locks the user out of the app.
  const [setup, setSetup] = useState<SetupStatus | null>(null)
  const [setupChecked, setSetupChecked] = useState(false)

  // Re-check on every navigation: finishing the setup screen or saving a key in
  // Settings changes the answer, and the call is a cheap local request.
  useEffect(() => {
    api
      .setupStatus()
      .then(setSetup)
      .catch(() => setSetup(null))
      .finally(() => setSetupChecked(true))
  }, [pathname])

  useEffect(() => {
    api
      .system()
      .then(setSystem)
      .catch(() => setSystemFailed(true))
  }, [])

  useEffect(() => {
    const load = () =>
      api
        .getQueue()
        .then((q) => setQueueCount(q.items.filter((i) => ACTIVE_QUEUE.includes(i.status)).length))
        .catch(() => undefined)
    void load()
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [])

  const nav: { to: string; label: string; icon: ReactNode; badge?: number }[] = [
    { to: '/jobs', label: 'Jobs', icon: <LayersIcon /> },
    { to: '/queue', label: 'Queue', icon: <ListIcon />, badge: queueCount },
    { to: '/channels', label: 'Channels', icon: <RadioIcon /> },
    { to: '/settings', label: 'Settings', icon: <SettingsIcon /> },
  ]

  const navLinks = (
    <>
      {nav.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          className={({ isActive }) =>
            cn(
              'flex h-8 items-center gap-2 rounded-control px-2.5 text-[13px] font-medium transition-colors',
              isActive ? 'bg-surface-2 text-fg' : 'text-muted hover:bg-surface hover:text-fg',
            )
          }
        >
          {item.icon}
          <span>{item.label}</span>
          {item.badge ? (
            <Badge tone="info" className="ml-auto">
              {item.badge}
            </Badge>
          ) : null}
        </NavLink>
      ))}
    </>
  )

  const status = system ? (
    <span
      className="flex items-center gap-2 text-xs text-muted"
      title={system.ready ? 'All required components present' : 'Run clipstudio doctor'}
    >
      <span className={cn('size-2 rounded-full', system.ready ? 'bg-ok' : 'bg-bad')} />
      {system.ready ? 'Ready' : 'Not ready'}
      <span className="text-faint">
        {system.accel.toUpperCase()}
        {system.gpu_name && system.accel.toUpperCase() === 'CUDA'
          ? ` ${system.gpu_name.replace('NVIDIA GeForce ', '')}`
          : ''}
      </span>
    </span>
  ) : systemFailed ? (
    <span className="flex items-center gap-2 text-xs text-muted">
      <span className="size-2 rounded-full bg-bad" />
      Server unreachable
    </span>
  ) : null

  // First run: required keys are missing, so send the user to the setup screen.
  // Settings stays reachable ("use another provider"), as does the setup screen.
  const needsSetup = setup !== null && !setup.ready
  const setupRoute = pathname === '/setup'
  if (!setupChecked) {
    return (
      <ToastProvider>
        <div className="min-h-screen bg-bg p-6">
          <LoadingBlock />
        </div>
      </ToastProvider>
    )
  }
  if (needsSetup && !setupRoute && pathname !== '/settings') {
    return <Navigate to="/setup" replace />
  }
  if (setupRoute) {
    return (
      <ToastProvider>
        <div className="min-h-screen bg-bg">
          <Outlet context={{ setSetup }} />
        </div>
      </ToastProvider>
    )
  }

  return (
    <ToastProvider>
      <div className="min-h-screen bg-bg">
        {/* Desktop sidebar */}
        <aside className="fixed inset-y-0 left-0 hidden w-[220px] flex-col border-r border-line bg-bg p-3 md:flex">
          <Link to="/jobs" className="flex items-center gap-2 px-2.5 pb-3 pt-1 text-sm font-semibold">
            <img src="/logo.svg" alt="" className="size-6" />
            ClipStudio
          </Link>
          <Link to="/" className={buttonClass('primary', 'md', 'mb-3 w-full')}>
            <PlusIcon />
            New clip job
          </Link>
          <nav className="flex flex-col gap-0.5" aria-label="Main">
            {navLinks}
          </nav>
          <div className="mt-auto px-2.5 pt-3">{status}</div>
        </aside>

        {/* Mobile top bar */}
        <header className="border-b border-line bg-bg p-3 md:hidden">
          <div className="flex items-center justify-between gap-3">
            <Link to="/jobs" className="flex items-center gap-2 text-sm font-semibold">
              <img src="/logo.svg" alt="" className="size-6" />
              ClipStudio
            </Link>
            <Link to="/" className={buttonClass('primary', 'sm')}>
              <PlusIcon />
              New clip job
            </Link>
          </div>
          <nav className="mt-3 flex flex-wrap gap-1" aria-label="Main">
            {navLinks}
          </nav>
        </header>

        <main className="md:pl-[220px]">
          <div className={cn('mx-auto p-4 md:p-6', reviewPage ? 'max-w-none' : 'max-w-[1200px]')}>
            <Outlet />
          </div>
        </main>
      </div>
    </ToastProvider>
  )
}
