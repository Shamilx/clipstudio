import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useOutletContext } from 'react-router-dom'

import { api, type KeyCheck, type SetupStatus, type SetupStep } from '../api'
import { Alert, Badge, Button, Card, Input, LoadingBlock, buttonClass, cn } from '../ui'
import { CheckIcon } from '../ui/icons'

interface SetupContext {
  setSetup?: (status: SetupStatus) => void
}

interface StepCopy {
  title: string
  free: boolean
  purpose: string
  howTo: string[]
  consoleLabel: string
  placeholder: string
}

/** Copy for the two default providers; any other key gets a generic card. */
const COPY: Record<string, StepCopy> = {
  groq: {
    title: 'Groq key (transcription)',
    free: true,
    purpose: 'Turns the speech in your videos into text with timestamps, using Whisper.',
    howTo: [
      'Open console.groq.com/keys',
      'Sign in (or create an account)',
      'Click "Create API key", then paste it here',
    ],
    consoleLabel: 'Open Groq console',
    placeholder: 'gsk_...',
  },
  nvidia: {
    title: 'NVIDIA key (finds the best moments)',
    free: true,
    purpose: 'A language model reads the transcript and picks the moments worth clipping.',
    howTo: [
      'Open build.nvidia.com and sign in (or create an account)',
      'Pick any model, then click "Get API Key"',
      'Generate the key and paste it here',
    ],
    consoleLabel: 'Open NVIDIA build',
    placeholder: 'nvapi-...',
  },
}

function StepCard({
  index,
  step,
  onSaved,
}: {
  index: number
  step: SetupStep
  onSaved: () => Promise<void>
}) {
  const copy: StepCopy = COPY[step.key] ?? {
    title: `${step.label} API key`,
    free: false,
    purpose: step.purpose,
    howTo: [`Sign in to your ${step.label} account`, 'Create an API key', 'Paste it here'],
    consoleLabel: `Open ${step.label}`,
    placeholder: '',
  }
  const [value, setValue] = useState('')
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<KeyCheck | null>(null)
  const inputId = `setup-key-${step.key}`

  const verifyAndSave = async () => {
    const key = value.trim()
    if (!key) {
      setResult({ ok: false, message: 'Paste a key first.' })
      return
    }
    setBusy(true)
    setResult(null)
    try {
      const check = await api.validateKey(step.key, key)
      if (!check.ok) {
        setResult(check)
        return
      }
      await api.putSecret(step.key, key)
      setValue('')
      setShow(false)
      setResult({ ok: true, message: `${check.message} Saved.` })
      await onSaved()
    } catch (err) {
      setResult({ ok: false, message: (err as Error).message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <span
            className={cn(
              'flex size-5 items-center justify-center rounded-full border text-[11px] font-semibold',
              step.present ? 'border-ok/40 bg-ok/10 text-ok' : 'border-line-strong text-muted',
            )}
          >
            {step.present ? <CheckIcon size={12} /> : index}
          </span>
          {copy.title}
        </span>
      }
      actions={
        <>
          {copy.free && <Badge tone="info">Free tier available</Badge>}
          {!step.required && <Badge>Optional</Badge>}
          <Badge tone={step.present ? 'success' : 'neutral'}>
            {step.present ? 'Saved' : 'Not set'}
          </Badge>
        </>
      }
    >
      <p className="text-[13px] text-muted">{copy.purpose}</p>

      <ol className="mt-3 list-decimal space-y-1 pl-5 text-[13px] text-fg">
        {copy.howTo.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ol>

      {step.get_url && (
        <a
          href={step.get_url}
          target="_blank"
          rel="noopener noreferrer"
          className={buttonClass('secondary', 'md', 'mt-3')}
        >
          {copy.consoleLabel}
        </a>
      )}

      <div className="mt-4">
        <label htmlFor={inputId} className="mb-1 block text-xs font-medium text-muted">
          API key
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex min-w-56 flex-1 items-center gap-2">
            <Input
              id={inputId}
              type={show ? 'text' : 'password'}
              value={value}
              autoComplete="off"
              spellCheck={false}
              placeholder={
                step.present
                  ? `${step.preview ?? 'Saved'} (paste a new key to replace)`
                  : copy.placeholder || 'Paste your key'
              }
              onChange={(e) => {
                setValue(e.target.value)
                setResult(null)
              }}
              onKeyDown={(e) => e.key === 'Enter' && void verifyAndSave()}
            />
            <Button
              variant="ghost"
              onClick={() => setShow((s) => !s)}
              aria-pressed={show}
              aria-label={show ? 'Hide key' : 'Show key'}
            >
              {show ? 'Hide' : 'Show'}
            </Button>
          </div>
          <Button
            variant="primary"
            loading={busy}
            disabled={!value.trim()}
            onClick={() => void verifyAndSave()}
          >
            {busy ? 'Verifying...' : 'Verify & save'}
          </Button>
        </div>
        {result && (
          <Alert tone={result.ok ? 'success' : 'danger'} className="mt-3">
            {result.message}
          </Alert>
        )}
      </div>
    </Card>
  )
}

export function Setup() {
  const navigate = useNavigate()
  const ctx = useOutletContext<SetupContext | null>()
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const fresh = await api.setupStatus()
      setStatus(fresh)
      ctx?.setSetup?.(fresh)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    }
  }, [ctx])

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 p-4 md:p-8">
      <header>
        <h1 className="text-xl font-semibold">Set up ClipStudio</h1>
        <p className="mt-1 text-[13px] text-muted">
          ClipStudio uses two online services to transcribe speech and pick clips. Both offer a
          free tier, so you can get going without paying. Keys are stored in your operating
          system&apos;s keyring and are only sent to the provider they belong to.
        </p>
      </header>

      {error && <Alert tone="danger">Could not load setup status: {error}</Alert>}
      {!status && !error && <LoadingBlock />}

      {status?.steps.map((step, i) => (
        <StepCard key={step.key} index={i + 1} step={step} onSaved={load} />
      ))}

      {status && (
        <p className="text-xs text-faint">
          Want to use a different model provider (Anthropic, Gemini, Ollama, another
          OpenAI-compatible service)?{' '}
          <Link to="/settings" className="text-[#7cb0ff] hover:underline">
            Use another provider in Settings
          </Link>
          .
        </p>
      )}

      <div className="flex items-center gap-3">
        <Button
          variant="primary"
          disabled={!status?.ready}
          onClick={() => navigate('/')}
        >
          Start using ClipStudio
        </Button>
        {status && !status.ready && (
          <span className="text-xs text-faint">Save the required keys above to continue.</span>
        )}
      </div>
    </div>
  )
}
