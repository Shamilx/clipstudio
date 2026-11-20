import { useEffect, useState } from 'react'

import { api, type ClipTemplate } from '../api'
import { Button, ConfirmButton, Field, Input, Select, SwitchRow, cn, useToast } from '../ui'

const PRESETS = ['yellow_pop', 'box', 'karaoke', 'one_word', 'clean']
const LAYOUTS: ClipTemplate['layout_mode'][] = ['auto', 'speaker', 'split']

function slug(s: string) {
  return s.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)
}

/** List, edit, save-as-new and delete clip templates. */
export function TemplatesSection() {
  const toast = useToast()
  const [list, setList] = useState<ClipTemplate[]>([])
  const [current, setCurrent] = useState<ClipTemplate | null>(null)
  const [saving, setSaving] = useState(false)
  const [removing, setRemoving] = useState(false)

  const reload = (select?: string) =>
    api
      .listTemplates()
      .then((l) => {
        setList(l)
        const pick = l.find((t) => t.name === (select ?? current?.name)) ?? l[0]
        setCurrent(pick ? { ...pick } : null)
      })
      .catch((e) => toast.error(e as Error))

  useEffect(() => {
    void reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const up = <K extends keyof ClipTemplate>(key: K, value: ClipTemplate[K]) =>
    setCurrent((c) => (c ? { ...c, [key]: value } : c))

  const save = async (t: ClipTemplate) => {
    setSaving(true)
    try {
      const saved = await api.saveTemplate(t)
      toast.success(`Saved "${saved.label}"`)
      await reload(saved.name)
    } catch (e) {
      toast.error(e as Error)
    } finally {
      setSaving(false)
    }
  }

  const saveAsNew = () => {
    if (!current) return
    const label = window.prompt('Name for the new template:', `${current.label} copy`)
    const name = label ? slug(label) : ''
    if (!label || !name) return
    void save({ ...current, name, label: label.trim(), builtin: false, user: true })
  }

  const remove = async () => {
    if (!current?.user) return
    setRemoving(true)
    try {
      await api.deleteTemplate(current.name)
      toast.success(current.builtin ? 'Reverted to the built-in version' : 'Template deleted')
      await reload()
    } catch (e) {
      toast.error(e as Error)
    } finally {
      setRemoving(false)
    }
  }

  const slider = (
    key: 'zoom_strength' | 'sfx_volume' | 'zoom_min_gap_s' | 'max_emoji',
    label: string,
    min: number,
    max: number,
    step: number,
  ) => (
    <Field label={label} htmlFor={`tpl-${key}`}>
      <input
        id={`tpl-${key}`}
        type="range"
        min={min}
        max={max}
        step={step}
        value={current![key]}
        onChange={(e) => up(key, Number(e.target.value))}
        className="h-1 w-full cursor-pointer accent-accent"
      />
    </Field>
  )

  return (
    <div>
      <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Templates">
        {list.map((t) => (
          <button
            key={t.name}
            type="button"
            role="tab"
            aria-selected={current?.name === t.name}
            onClick={() => setCurrent({ ...t })}
            className={cn(
              'h-7 rounded-control border px-2.5 text-xs font-medium transition-colors',
              current?.name === t.name
                ? 'border-accent bg-accent/10 text-fg'
                : 'border-line-strong text-muted hover:text-fg',
            )}
          >
            {t.label}
            {t.user && <span className="ml-1.5 text-faint">{t.builtin ? 'edited' : 'custom'}</span>}
          </button>
        ))}
      </div>

      {current && (
        <div className="mt-5 grid gap-x-6 gap-y-4 sm:grid-cols-2">
          <p className="text-xs text-faint sm:col-span-2">{current.description}</p>

          <Field label="Caption preset" htmlFor="tpl-preset">
            <Select
              id="tpl-preset"
              value={current.caption_preset}
              onChange={(e) => up('caption_preset', e.target.value)}
            >
              {PRESETS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Layout" htmlFor="tpl-layout">
            <Select
              id="tpl-layout"
              value={current.layout_mode}
              onChange={(e) => up('layout_mode', e.target.value as ClipTemplate['layout_mode'])}
            >
              {LAYOUTS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </Select>
          </Field>

          {(
            [
              ['primary_color', 'Text color'],
              ['accent_color', 'Accent color'],
              ['progress_color', 'Progress bar color'],
            ] as const
          ).map(([key, label]) => (
            <label key={key} className="flex items-center gap-3 text-[13px]">
              <input
                type="color"
                value={current[key]}
                onChange={(e) => up(key, e.target.value.toUpperCase())}
                className="h-8 w-12 cursor-pointer rounded-control border border-line-strong bg-transparent"
              />
              {label}
            </label>
          ))}

          {slider('zoom_strength', `Zoom strength: ${current.zoom_strength.toFixed(2)}x`, 1, 1.3, 0.01)}
          {slider('sfx_volume', `Sound-effect volume: ${Math.round(current.sfx_volume * 100)}%`, 0, 1, 0.05)}
          {slider('zoom_min_gap_s', `Min. seconds between zooms: ${current.zoom_min_gap_s}`, 0, 15, 0.5)}
          {slider('max_emoji', `Max emoji per clip: ${current.max_emoji}`, 0, 6, 1)}

          <div className="grid gap-x-6 sm:col-span-2 sm:grid-cols-3">
            {(
              [
                ['hook_card', 'Hook card'],
                ['sfx_enabled', 'Sound effects'],
                ['progress_bar', 'Progress bar'],
              ] as const
            ).map(([key, label]) => (
              <SwitchRow key={key} label={label} checked={current[key]} onChange={(v) => up(key, v)} />
            ))}
          </div>

          <Field label="Hook style" htmlFor="tpl-hook">
            <Select
              id="tpl-hook"
              value={current.hook_style}
              onChange={(e) => up('hook_style', e.target.value as 'box' | 'plain')}
            >
              <option value="box">box</option>
              <option value="plain">plain</option>
            </Select>
          </Field>

          <div className="sm:col-span-2">
            <p className="mb-2 text-xs font-medium text-muted">Campaign rules</p>
            <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
              <Field label="Max clip length (s, blank = none)" htmlFor="tpl-maxdur">
                <Input
                  id="tpl-maxdur"
                  type="number"
                  min={5}
                  max={600}
                  value={current.max_duration_s ?? ''}
                  onChange={(e) => up('max_duration_s', e.target.value ? Number(e.target.value) : null)}
                />
              </Field>
              <Field label="Required hashtags (space or comma separated)" htmlFor="tpl-tags">
                <Input
                  id="tpl-tags"
                  placeholder="#ad #brand"
                  value={(current.required_hashtags ?? []).join(' ')}
                  onChange={(e) => up('required_hashtags', e.target.value.split(/[\s,]+/).filter(Boolean))}
                />
              </Field>
              <Field label="Credit line" htmlFor="tpl-credit">
                <Input
                  id="tpl-credit"
                  placeholder="Credit: @creator"
                  value={current.credit_text ?? ''}
                  onChange={(e) => up('credit_text', e.target.value)}
                />
              </Field>
              <Field label="Watermark text" htmlFor="tpl-watermark">
                <Input
                  id="tpl-watermark"
                  value={current.watermark_text ?? ''}
                  onChange={(e) => up('watermark_text', e.target.value || null)}
                />
              </Field>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
            <Button variant="primary" loading={saving} onClick={() => void save(current)}>
              Save
            </Button>
            <Button onClick={saveAsNew} disabled={saving}>
              Save as new...
            </Button>
            {current.user && (
              <ConfirmButton
                loading={removing}
                onConfirm={() => void remove()}
                confirmLabel={current.builtin ? 'Confirm revert' : 'Confirm delete'}
              >
                {current.builtin ? 'Revert to built-in' : 'Delete'}
              </ConfirmButton>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
