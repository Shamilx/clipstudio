import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import {
  EDIT_LABELS,
  api,
  type EditToggles,
  type ProviderStatus,
  type Settings as SettingsData,
  type SystemStatus,
} from '../api'
import { ErrorNote } from '../components/ErrorNote'
import { TemplatesSection } from '../components/TemplatesSection'
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfirmButton,
  Field,
  Input,
  LoadingBlock,
  PageHeader,
  Select,
  SwitchRow,
  cn,
  useToast,
} from '../ui'

const SECRET_LABELS: Record<string, string> = {
  anthropic: 'Anthropic API key',
  groq: 'Groq API key (transcription)',
  nvidia: 'NVIDIA API key (default LLM)',
  openai: 'OpenAI-compatible API key (other providers)',
  gemini: 'Google Gemini API key',
  huggingface_token: 'HuggingFace token',
}

const SECTIONS = [
  ['keys', 'API keys'],
  ['provider', 'Provider & models'],
  ['transcription', 'Transcription'],
  ['clips', 'Clips'],
  ['editing', 'Editing defaults'],
  ['export', 'Export'],
  ['ingest', 'Ingest'],
  ['templates', 'Templates'],
  ['system', 'System'],
] as const

export function Settings() {
  const toast = useToast()
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [providers, setProviders] = useState<ProviderStatus[]>([])
  const [system, setSystem] = useState<SystemStatus | null>(null)
  const [error, setError] = useState<Error | null>(null)

  const reload = () => {
    void api.getSettings().then(setSettings).catch((e) => setError(e as Error))
    void api.providerStatus().then(setProviders).catch(() => undefined)
    void api.system().then(setSystem).catch(() => undefined)
  }

  useEffect(reload, [])

  const patch = async (update: Partial<SettingsData>) => {
    try {
      setSettings(await api.putSettings(update))
      toast.success('Saved')
    } catch (err) {
      toast.error(err as Error)
    }
  }

  if (!settings) {
    return (
      <div>
        <PageHeader title="Settings" />
        {error ? <ErrorNote error={error} /> : <LoadingBlock label="Loading settings" />}
      </div>
    )
  }

  const active = settings.providers[settings.active_provider]

  return (
    <div>
      <PageHeader title="Settings" subtitle="Changes save as you make them." />

      {error && (
        <div className="mb-4">
          <ErrorNote error={error} onDismiss={() => setError(null)} />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[180px_minmax(0,1fr)]">
        <nav className="hidden lg:block" aria-label="Settings sections">
          <ul className="sticky top-6 flex flex-col gap-0.5">
            {SECTIONS.map(([id, label]) => (
              <li key={id}>
                <button
                  type="button"
                  onClick={() => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
                  className="h-8 w-full rounded-control px-2.5 text-left text-[13px] text-muted hover:bg-surface hover:text-fg"
                >
                  {label}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <div className="flex min-w-0 max-w-3xl flex-col gap-4">
          {settings.insecure_secret_storage && (
            <Alert tone="warning" title="No OS keyring available">
              API keys are stored in plain text in config.json. On headless Linux, installing a Secret
              Service provider or keyrings.alt fixes this.
            </Alert>
          )}

          <Card id="keys" title="API keys" className="scroll-mt-6">
            <p className="mb-4 text-xs text-faint">
              Stored in your OS keyring. Never sent anywhere but the provider.{' '}
              <Link to="/setup" className="text-[#7cb0ff] hover:underline">
                Open the setup guide
              </Link>{' '}
              for free keys.
            </p>
            <div className="flex flex-col gap-4">
              {Object.entries(SECRET_LABELS).map(([key, label]) => (
                <SecretField
                  key={key}
                  secretKey={key}
                  label={label}
                  present={settings.keys_present[key] ?? false}
                  onChanged={reload}
                />
              ))}
            </div>
          </Card>

          <Card id="provider" title="Provider & models" className="scroll-mt-6">
            <p className="mb-3 text-xs text-faint">Which model picks the clips.</p>
            <div className="flex flex-col gap-1.5" role="radiogroup" aria-label="Active provider">
              {providers.map((provider) => {
                const selected = provider.name === settings.active_provider
                return (
                  <button
                    key={provider.name}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => !selected && void patch({ active_provider: provider.name })}
                    className={cn(
                      'rounded-control border px-3 py-2 text-left transition-colors',
                      selected ? 'border-accent bg-accent/10' : 'border-line hover:border-line-strong',
                    )}
                  >
                    <div className="flex items-center justify-between gap-4">
                      <span className="text-[13px] font-medium">{provider.name}</span>
                      <Badge tone={provider.available ? 'success' : 'neutral'}>
                        {provider.available ? 'Reachable' : provider.detail || 'Unavailable'}
                      </Badge>
                    </div>
                    {provider.models.length > 0 && (
                      <span className="mt-0.5 block truncate text-xs text-faint">
                        {provider.models.slice(0, 6).join(' · ')}
                      </span>
                    )}
                  </button>
                )
              })}
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <TextSetting
                id="model"
                label="Model"
                value={active?.model ?? ''}
                placeholder="model name"
                onCommit={(value) =>
                  patch({ providers: { [settings.active_provider]: { ...active, model: value } } })
                }
              />
              <TextSetting
                id="base-url"
                label="Base URL"
                hint="Point at OpenRouter, Groq, DeepSeek, or a local server."
                value={active?.base_url ?? ''}
                placeholder="https://..."
                onCommit={(value) =>
                  patch({
                    providers: { [settings.active_provider]: { ...active, base_url: value || null } },
                  })
                }
              />
            </div>
          </Card>

          <Card id="transcription" title="Transcription" className="scroll-mt-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Transcription engine" htmlFor="whisper-backend">
                <Select
                  id="whisper-backend"
                  value={settings.whisper.backend}
                  onChange={(e) =>
                    void patch({ whisper: { ...settings.whisper, backend: e.target.value as 'groq' | 'local' } })
                  }
                >
                  <option value="groq">groq</option>
                  <option value="local">local</option>
                </Select>
              </Field>
              <TextSetting
                id="whisper-language"
                label="Language"
                hint="Leave empty to detect automatically."
                value={settings.whisper.language}
                placeholder="auto"
                onCommit={(value) => patch({ whisper: { ...settings.whisper, language: value } })}
              />
            </div>
            <div className="mt-2">
              <SwitchRow
                label="Identify speakers"
                hint={
                  !system?.diarization_available ? (
                    <>
                      Needs the diarization extra:{' '}
                      <code className="font-mono">uv pip install &apos;clipstudio[diarization]&apos;</code>
                    </>
                  ) : undefined
                }
                checked={settings.whisper.diarization}
                disabled={!system?.diarization_available}
                onChange={(v) => void patch({ whisper: { ...settings.whisper, diarization: v } })}
              />
            </div>
          </Card>

          <Card id="clips" title="Clips" className="scroll-mt-6">
            <div className="grid gap-4 sm:grid-cols-3">
              <NumberSetting
                id="clips-min"
                label="Min length (s)"
                value={settings.clips.min_duration_s}
                onCommit={(value) => patch({ clips: { ...settings.clips, min_duration_s: value } })}
              />
              <NumberSetting
                id="clips-max"
                label="Max length (s)"
                value={settings.clips.max_duration_s}
                onCommit={(value) => patch({ clips: { ...settings.clips, max_duration_s: value } })}
              />
              <NumberSetting
                id="clips-count"
                label="Max clips"
                value={settings.clips.max_clips}
                onCommit={(value) => patch({ clips: { ...settings.clips, max_clips: value } })}
              />
            </div>
          </Card>

          <Card id="editing" title="Editing defaults" className="scroll-mt-6">
            <p className="mb-1 text-xs text-faint">Defaults for new jobs. Each can be changed per job.</p>
            <div className="divide-y divide-line">
              {(Object.keys(EDIT_LABELS) as (keyof EditToggles)[]).map((key) => (
                <SwitchRow
                  key={key}
                  label={EDIT_LABELS[key][0]}
                  hint={EDIT_LABELS[key][1]}
                  checked={Boolean(settings.edit?.[key])}
                  onChange={(v) => void patch({ edit: { ...settings.edit, [key]: v } })}
                />
              ))}
            </div>
          </Card>

          <Card id="export" title="Export" className="scroll-mt-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Default ratio" htmlFor="export-ratio">
                <Select
                  id="export-ratio"
                  value={settings.export.ratio}
                  onChange={(e) => void patch({ export: { ...settings.export, ratio: e.target.value } })}
                >
                  {['9:16', '1:1', '16:9'].map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </Select>
              </Field>
              <NumberSetting
                id="export-lufs"
                label="Loudness target (LUFS)"
                value={settings.export.loudness_lufs}
                onCommit={(value) => patch({ export: { ...settings.export, loudness_lufs: value } })}
              />
            </div>
            <div className="mt-2 divide-y divide-line">
              <SwitchRow
                label="Use GPU encoding when available"
                hint={
                  system && !system.nvenc_works
                    ? 'Not usable on this machine. Exports will use the CPU encoder: same quality, slower.'
                    : undefined
                }
                checked={settings.export.prefer_hardware_encoder}
                onChange={(v) => void patch({ export: { ...settings.export, prefer_hardware_encoder: v } })}
              />
              <SwitchRow
                label="Write an .srt sidecar"
                checked={settings.export.write_srt}
                onChange={(v) => void patch({ export: { ...settings.export, write_srt: v } })}
              />
            </div>
          </Card>

          <Card id="ingest" title="Ingest" className="scroll-mt-6">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                label="YouTube cookies from"
                htmlFor="ingest-cookies"
                hint="YouTube blocks most anonymous downloads. Sign in in that browser and close it before downloading."
              >
                <Select
                  id="ingest-cookies"
                  value={settings.ingest.cookies_from_browser}
                  onChange={(e) =>
                    void patch({ ingest: { ...settings.ingest, cookies_from_browser: e.target.value } })
                  }
                >
                  {['', 'chrome', 'firefox', 'edge', 'brave', 'chromium', 'safari'].map((b) => (
                    <option key={b} value={b}>
                      {b || 'None'}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
          </Card>

          <Card id="templates" title="Templates" className="scroll-mt-6">
            <p className="mb-4 text-xs text-faint">
              Look and feel per content type. New jobs pick one automatically unless you choose.
            </p>
            <TemplatesSection />
          </Card>

          {system && (
            <Card id="system" title="System" className="scroll-mt-6">
              <dl className="grid gap-x-8 text-[13px] sm:grid-cols-2">
                <Row label="Platform" value={system.platform} />
                <Row label="Python" value={system.python_version} />
                <Row label="ffmpeg" value={system.ffmpeg_version ?? 'not found'} />
                <Row label="Acceleration" value={system.accel.toUpperCase()} />
                <Row label="Device" value={system.gpu_name ?? '-'} />
                <Row label="Whisper compute" value={system.compute_type} />
                <Row label="GPU encode" value={system.nvenc_works ? 'available' : 'unavailable'} />
                <Row label="Captions" value={system.has_libass ? 'libass present' : 'libass missing'} />
              </dl>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line py-2">
      <dt className="text-muted">{label}</dt>
      <dd className="truncate text-right tabular-nums" title={value}>
        {value}
      </dd>
    </div>
  )
}

/** Commits on blur rather than per keystroke, so a PUT isn't fired per letter. */
function TextSetting({
  id,
  label,
  hint,
  value,
  placeholder,
  onCommit,
}: {
  id: string
  label: string
  hint?: string
  value: string
  placeholder?: string
  onCommit: (value: string) => void
}) {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])

  return (
    <Field label={label} hint={hint} htmlFor={id}>
      <Input
        id={id}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => draft !== value && onCommit(draft)}
        onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
        spellCheck={false}
      />
    </Field>
  )
}

function NumberSetting({
  id,
  label,
  value,
  onCommit,
}: {
  id: string
  label: string
  value: number
  onCommit: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])

  return (
    <Field label={label} htmlFor={id}>
      <Input
        id={id}
        type="number"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const parsed = Number(draft)
          if (!Number.isNaN(parsed) && parsed !== value) onCommit(parsed)
        }}
        onKeyDown={(e) => e.key === 'Enter' && e.currentTarget.blur()}
      />
    </Field>
  )
}

function SecretField({
  secretKey,
  label,
  present,
  onChanged,
}: {
  secretKey: string
  label: string
  present: boolean
  onChanged: () => void
}) {
  const toast = useToast()
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState<'save' | 'remove' | null>(null)
  const id = `secret-${secretKey}`

  const save = async () => {
    if (!value.trim()) return
    setBusy('save')
    try {
      await api.putSecret(secretKey, value.trim())
      setValue('')
      toast.success(`${label} saved`)
      onChanged()
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setBusy(null)
    }
  }

  const remove = async () => {
    setBusy('remove')
    try {
      await api.deleteSecret(secretKey)
      toast.success(`${label} removed`)
      onChanged()
    } catch (err) {
      toast.error(err as Error)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex flex-wrap items-end gap-2">
      <div className="min-w-56 flex-1">
        <div className="mb-1 flex items-center gap-2">
          <label htmlFor={id} className="text-xs font-medium text-muted">
            {label}
          </label>
          <Badge tone={present ? 'success' : 'neutral'}>{present ? 'Set' : 'Not set'}</Badge>
        </div>
        <Input
          id={id}
          type="password"
          value={value}
          placeholder={present ? '************ (enter a new value to replace)' : 'Paste to add'}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void save()}
          autoComplete="off"
        />
      </div>
      <Button onClick={save} loading={busy === 'save'} disabled={!value.trim() || busy !== null}>
        {present ? 'Replace' : 'Save'}
      </Button>
      {present && (
        <ConfirmButton variant="ghost" loading={busy === 'remove'} disabled={busy !== null} onConfirm={() => void remove()}>
          Remove
        </ConfirmButton>
      )}
    </div>
  )
}
