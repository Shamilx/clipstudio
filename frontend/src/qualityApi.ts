export interface QualityIssue {
  code: string
  message: string
  severity: 'error' | 'warn' | 'info'
}

export interface QaResult {
  status: 'pass' | 'warn'
  issues: { code: string; message: string }[]
  fixes: string[]
  vision: boolean
}

export interface QualityReport {
  clip_id?: string
  /** 'qa' = only the pipeline's self-QA ran so far (no quality check yet). */
  status: 'ready' | 'review' | 'rejected' | 'qa'
  /** Result of the automatic post-export self-QA. */
  qa?: QaResult
  score: number
  issues: QualityIssue[]
  metrics: Record<string, number | null>
  checked_at?: string
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') msg = body.detail
    } catch {
      /* no body */
    }
    throw Object.assign(new Error(msg), { status: res.status })
  }
  return res.json() as Promise<T>
}

export const qualityApi = {
  forJob: (jobId: string) => json<QualityReport[]>(`/api/jobs/${jobId}/quality`),
  run: (clipId: string) => json<QualityReport>(`/api/clips/${clipId}/quality`, { method: 'POST' }),
  /** Downloads the pack as a file; throws an Error with `.status` on failure. */
  async downloadPack(clipId: string): Promise<void> {
    const res = await fetch(`/api/clips/${clipId}/pack`)
    if (!res.ok) throw Object.assign(new Error(`${res.status}`), { status: res.status })
    const blob = await res.blob()
    const name =
      /filename="?([^";]+)"?/.exec(res.headers.get('content-disposition') ?? '')?.[1] ??
      `clip-${clipId}-pack.zip`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    a.click()
    URL.revokeObjectURL(url)
  },
}
