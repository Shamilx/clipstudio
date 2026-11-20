/**
 * Typed client for the ClipStudio API.
 *
 * Same-origin throughout: the server hands out the built bundle, so there is no
 * base URL to configure and no CORS to negotiate.
 */

export interface Source {
  id: string
  type: string
  title: string
  url: string | null
  filename: string | null
  channel: string | null
  duration_s: number
  width: number | null
  height: number | null
  fps: number | null
  has_audio: boolean
  has_video: boolean
  created_at: string
}

export type JobStatus = 'queued' | 'running' | 'failed' | 'done' | 'cancelled'

export interface CampaignRules {
  required_hashtags: string[]
  required_caption_text: string[]
  caption_style: string
  forbidden: string[]
  footage_type: 'film' | 'talking' | 'event' | 'gaming' | 'other'
  onscreen_text_required: boolean
  warnings: string[]
  vocabulary?: string[]
}

export interface Job {
  id: string
  source_id: string
  status: JobStatus
  current_stage: string
  progress: number
  error: string | null
  provider: string
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  /** What the job is doing right now (written by the runner, CLI or server). */
  activity?: string
  /** UTC ISO time of the last activity update. */
  heartbeat_at?: string | null
  activity_log?: { t: string; stage: string; msg: string }[]
  source: Source | null
  brief?: string
  campaign?: CampaignRules | null
  /** Content profile chosen for this job (film / talking / event / gaming). */
  profile?: string
  /** Set while a "generate more clips" run is queued/running. */
  more_count?: number | null
  /** False when word captions are not burned (footage already has subtitles). */
  burn_captions?: boolean
}

export interface ExportRecord {
  id: string
  clip_id: string
  ratio: string
  style: string
  size_bytes: number
  created_at: string
  download_url: string
}

export type ClipStatus = 'candidate' | 'kept' | 'discarded' | 'exported'

export type RejectReason =
  | 'ad_or_announcement'
  | 'boring_no_story'
  | 'bad_framing'
  | 'product_not_visible'
  | 'other'

export const REJECT_REASONS: { value: RejectReason; label: string }[] = [
  { value: 'ad_or_announcement', label: 'Ad / announcement' },
  { value: 'boring_no_story', label: 'Boring, no story' },
  { value: 'bad_framing', label: 'Bad framing' },
  { value: 'product_not_visible', label: 'Product not visible' },
  { value: 'other', label: 'Other' },
]

export interface Clip {
  id: string
  job_id: string
  rank: number
  start_s: number
  end_s: number
  duration_s: number
  start_word: number
  end_word: number
  title: string
  hook: string
  score: number
  reason: string
  status: ClipStatus
  user_trimmed: boolean
  description: string
  hashtags: string[]
  llm_score: number
  heuristic_score: number
  reject_reason?: RejectReason | ''
  reject_note?: string
  caption_style: string
  ratio: string
  exports: ExportRecord[]
}

export interface Word {
  text: string
  start: number
  end: number
  speaker: string | null
}

export interface CropKeyframe {
  t: number
  x: number
  y: number
}

export interface CropSegment {
  start_s: number
  end_s: number
  width: number
  height: number
  keyframes: CropKeyframe[]
  strategy: 'track' | 'wide' | 'general'
  zoom: number
  fit: boolean
}

/** Mirrors clipstudio.pipeline.reframe.croppath.CropPath. */
export interface CropPath {
  source_width: number
  source_height: number
  segments: CropSegment[]
}

export interface CaptionStyle {
  key: string
  label: string
  description: string
  preview: {
    font: string
    primary: string
    accent: string | null
    outline: string
    outlineWidth: number
    allCaps: boolean
    sizeRatio: number
    marginRatio: number
    boxed: boolean
    animation: string
    maxWords: number
  }
}

export interface ProviderStatus {
  name: string
  available: boolean
  detail: string
  models: string[]
  requires_key: boolean
  has_key: boolean
}

export interface EditToggles {
  trim_silence: boolean
  remove_fillers: boolean
  punch_zoom: boolean
  keyword_emphasis: boolean
  progress_bar: boolean
  normalize_audio: boolean
  background_music: boolean
  burn_captions: boolean
}

export const EDIT_LABELS: Record<keyof EditToggles, [string, string]> = {
  trim_silence: ['Cut silences', 'Shorten long pauses'],
  remove_fillers: ['Remove fillers', 'Cut "um", "uh" and similar'],
  punch_zoom: ['Punch-in zooms', 'Zoom on key moments and jump cuts'],
  keyword_emphasis: ['Highlight key words', 'Numbers and key words in colour'],
  progress_bar: ['Progress bar', 'Thin bar at the top'],
  normalize_audio: ['Normalize loudness', 'About -14 LUFS'],
  background_music: ['Background music', 'Needs royalty-free tracks in the music/ folder'],
  burn_captions: ['Burn captions', 'Turn off if the video already has subtitles (hook stays)'],
}

export interface Settings {
  active_provider: string
  providers: Record<string, { model: string; base_url: string | null }>
  whisper: {
    backend: 'groq' | 'local'
    groq_model: string
    model: string
    compute_type: string
    language: string
    diarization: boolean
  }
  edit: EditToggles & Record<string, unknown>
  clips: { min_duration_s: number; max_duration_s: number; max_clips: number }
  ingest: {
    ytdlp_format: string
    cookies_from_browser: string
    prefer_youtube_captions: boolean
  }
  export: {
    ratio: string
    caption_style: string
    loudness_lufs: number
    prefer_hardware_encoder: boolean
    crf: number
    write_srt: boolean
  }
  insecure_secret_storage: boolean
  keys_present: Record<string, boolean>
}

export interface SetupStep {
  key: string
  label: string
  purpose: string
  required: boolean
  present: boolean
  /** Masked tail of the stored key; the full key is never returned. */
  preview: string | null
  get_url: string
}

export interface SetupStatus {
  ready: boolean
  steps: SetupStep[]
}

export interface KeyCheck {
  ok: boolean
  message: string
}

export type JobEditOverrides = Partial<EditToggles>

export interface SystemStatus {
  ready: boolean
  python_version: string
  platform: string
  ffmpeg_version: string | null
  has_libass: boolean
  nvenc_works: boolean
  accel: string
  gpu_name: string | null
  compute_type: string
  diarization_available: boolean
}

export interface ClipTemplate {
  name: string
  label: string
  description: string
  caption_preset: string
  primary_color: string
  accent_color: string
  font: string
  hook_card: boolean
  hook_style: 'box' | 'plain'
  zoom_strength: number
  zoom_min_gap_s: number
  max_emoji: number
  sfx_enabled: boolean
  sfx_volume: number
  progress_bar: boolean
  progress_color: string
  layout_mode: 'auto' | 'speaker' | 'split'
  max_duration_s: number | null
  required_hashtags: string[]
  credit_text: string
  watermark_text: string | null
  builtin?: boolean
  user?: boolean
}

export interface QueueItem {
  id: string
  url: string
  template: string
  status: 'pending' | 'downloading' | 'ready' | 'running' | 'done' | 'failed' | 'cancelled'
  position: number
  title: string
  source_id: string | null
  job_id: string | null
  progress: number
  stage: string
  error: string | null
  origin: string
}

export interface QueueState {
  paused: boolean
  current: string | null
  items: QueueItem[]
  added?: number
  skipped?: number
}

export interface Channel {
  id: string
  url: string
  title: string
  template: string
  interval_min: number
  min_duration_s: number | null
  max_duration_s: number | null
  include_keywords: string
  exclude_keywords: string
  enabled: boolean
  added_at: string
  last_checked: number | null
  last_error: string | null
  enqueued_count: number
}

export interface ChannelInput {
  url: string
  template: string
  interval_min: number
  min_duration_s: number | null
  max_duration_s: number | null
  include_keywords: string
  exclude_keywords: string
}

export interface JobSettingsOverrides extends Partial<EditToggles> {
  provider?: string
  whisper_model?: string
  language?: string
  diarization?: boolean
  min_duration_s?: number
  max_duration_s?: number
  max_clips?: number
  caption_style?: string
  ratio?: string
}

/** An API error carrying the server's message and its actionable hint. */
export class ApiError extends Error {
  hint: string
  status: number

  constructor(message: string, status: number, hint = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.hint = hint
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`
    let hint = ''
    try {
      const body = await response.json()
      const detail = body.detail
      if (typeof detail === 'string') {
        message = detail
      } else if (detail && typeof detail === 'object') {
        // The ingest endpoints return {message, hint} so the UI can show the
        // fix alongside the failure.
        message = detail.message ?? message
        hint = detail.hint ?? ''
      }
    } catch {
      /* Response had no JSON body; the status line is the best we have. */
    }
    throw new ApiError(message, response.status, hint)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  health: () => request<{ status: string; version: string }>('/api/health'),
  system: () => request<SystemStatus>('/api/system'),
  fetchModels: () => request<void>('/api/system/models', { method: 'POST' }),

  listSources: () => request<Source[]>('/api/sources'),

  ingestYouTube: (url: string, cookiesFromBrowser?: string, progressId?: string) =>
    request<Source>('/api/sources/youtube', {
      method: 'POST',
      body: JSON.stringify({
        url,
        cookies_from_browser: cookiesFromBrowser || null,
        progress_id: progressId || null,
      }),
    }),

  ingestProgress: (progressId: string) =>
    request<{ fraction: number | null; message: string }>(
      `/api/sources/progress/${encodeURIComponent(progressId)}`,
    ),

  uploadSource: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Source>('/api/sources/upload', { method: 'POST', body: form })
  },

  listJobs: (limit = 25) => request<Job[]>(`/api/jobs?limit=${limit}`),
  getJob: (id: string) => request<Job>(`/api/jobs/${id}`),

  createJob: (
    sourceId: string,
    settings: JobSettingsOverrides = {},
    template = 'auto',
    brief = '',
  ) =>
    request<Job>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ source_id: sourceId, settings, template, brief }),
    }),
  generateMore: (id: string, count = 5) =>
    request<Job>(`/api/jobs/${id}/more`, { method: 'POST', body: JSON.stringify({ count }) }),

  getQueue: () => request<QueueState>('/api/queue'),
  enqueue: (text: string, template: string) =>
    request<QueueState>('/api/queue', { method: 'POST', body: JSON.stringify({ text, template }) }),
  queueAction: (action: 'pause' | 'resume' | 'clear-finished') =>
    request<QueueState>(`/api/queue/${action}`, { method: 'POST' }),
  queueRemove: (id: string) => request<QueueState>(`/api/queue/${id}`, { method: 'DELETE' }),
  queueMove: (id: string, direction: 'up' | 'down') =>
    request<QueueState>(`/api/queue/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ direction }),
    }),
  queueRetry: (id: string) => request<QueueState>(`/api/queue/${id}/retry`, { method: 'POST' }),

  listChannels: () => request<Channel[]>('/api/channels'),
  addChannel: (c: ChannelInput) =>
    request<Channel>('/api/channels', { method: 'POST', body: JSON.stringify(c) }),
  patchChannel: (id: string, patch: Partial<ChannelInput> & { enabled?: boolean }) =>
    request<Channel>(`/api/channels/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  removeChannel: (id: string) => request<void>(`/api/channels/${id}`, { method: 'DELETE' }),
  checkChannel: (id: string) =>
    request<{ enqueued: { id: string; title: string }[]; channel: Channel }>(
      `/api/channels/${id}/check`,
      { method: 'POST' },
    ),

  listTemplates: () => request<ClipTemplate[]>('/api/templates'),
  saveTemplate: (t: ClipTemplate) =>
    request<ClipTemplate>(`/api/templates/${encodeURIComponent(t.name)}`, {
      method: 'PUT',
      body: JSON.stringify(t),
    }),
  deleteTemplate: (name: string) =>
    request<void>(`/api/templates/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  rerenderClip: (clipId: string, template: string) =>
    request<unknown>(`/api/clips/${clipId}/rerender`, {
      method: 'POST',
      body: JSON.stringify({ template }),
    }),

  cancelJob: (id: string) => request<Job>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),

  listClips: (jobId: string) => request<Clip[]>(`/api/jobs/${jobId}/clips`),
  getClip: (clipId: string) => request<Clip>(`/api/clips/${clipId}`),

  getCropPath: (clipId: string) => request<CropPath>(`/api/clips/${clipId}/crop-path`),
  getClipWords: (clipId: string) => request<Word[]>(`/api/clips/${clipId}/words`),

  patchClip: (
    clipId: string,
    patch: {
      start_s?: number
      end_s?: number
      title?: string
      description?: string
      hashtags?: string[]
      status?: ClipStatus
      reject_reason?: RejectReason
      reject_note?: string
    },
  ) => request<Clip>(`/api/clips/${clipId}`, { method: 'PATCH', body: JSON.stringify(patch) }),

  patchCaptions: (
    clipId: string,
    patch: { words?: Word[]; caption_style?: string; ratio?: string },
  ) =>
    request<Clip>(`/api/clips/${clipId}/captions`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  exportClip: (clipId: string, ratio: string, style: string, writeSrt = false) =>
    request<ExportRecord>(`/api/clips/${clipId}/export`, {
      method: 'POST',
      body: JSON.stringify({ ratio, style, write_srt: writeSrt }),
    }),

  captionStyles: () => request<CaptionStyle[]>('/api/caption-styles'),
  providerStatus: () => request<ProviderStatus[]>('/api/providers/status'),

  getSettings: () => request<Settings>('/api/settings'),
  putSettings: (patch: Partial<Settings>) =>
    request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  putSecret: (key: string, value: string) =>
    request<void>('/api/settings/secrets', {
      method: 'PUT',
      body: JSON.stringify({ key, value }),
    }),

  setupStatus: () => request<SetupStatus>('/api/setup/status'),
  validateKey: (key: string, value?: string) =>
    request<KeyCheck>('/api/setup/validate', {
      method: 'POST',
      body: JSON.stringify({ key, value }),
    }),

  deleteSecret: (key: string) =>
    request<void>(`/api/settings/secrets/${key}`, { method: 'DELETE' }),

  mediaUrl: (jobId: string) => `/api/jobs/${jobId}/media`,
  exportVideoUrl: (exportId: string) => `/api/exports/${exportId}/video`,
}

/** Format seconds as m:ss, or h:mm:ss past an hour. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const s = total % 60
  const m = Math.floor(total / 60) % 60
  const h = Math.floor(total / 3600)
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
}

/** Format seconds as timecode with centiseconds, for trim handles. */
export function formatTimecode(seconds: number): string {
  const total = Math.max(0, seconds)
  const cs = Math.floor((total % 1) * 100)
  const s = Math.floor(total) % 60
  const m = Math.floor(total / 60)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(m)}:${pad(s)}.${pad(cs)}`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}
