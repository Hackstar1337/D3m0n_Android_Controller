export const DEFAULT_AGENT_PROVIDER = 'ollama'
export const DEFAULT_AGENT_MODEL = 'qwen3.5:4b'

export type AgentProviderOption = {
  id: string
  label: string
  models?: string[]
}

export type AgentOllamaModel = {
  name: string
  status: 'loaded' | 'downloaded' | string
  vram_gb?: number
  size_gb?: number
  parameter_size?: string
  capabilities?: string[]
}

export type AgentPullStatus = {
  status: 'pulling' | 'done' | 'error' | 'unknown' | string
  model?: string
  completed?: number
  total?: number
  percent?: number
  message?: string
  error?: string
}

export function resolveAgentSelection(
  providers: AgentProviderOption[],
  rememberedProvider?: string | null,
  rememberedModel?: string | null,
): { provider: string; model: string } {
  const available = providers.filter((item) => item && typeof item.id === 'string')
  const provider = available.find((item) => item.id === rememberedProvider)?.id
    || available.find((item) => item.id === DEFAULT_AGENT_PROVIDER)?.id
    || available[0]?.id
    || DEFAULT_AGENT_PROVIDER
  const models = available.find((item) => item.id === provider)?.models || []
  const model = models.includes(String(rememberedModel || ''))
    ? String(rememberedModel)
    : provider === DEFAULT_AGENT_PROVIDER && models.includes(DEFAULT_AGENT_MODEL)
      ? DEFAULT_AGENT_MODEL
      : (models[0] || (provider === DEFAULT_AGENT_PROVIDER ? DEFAULT_AGENT_MODEL : ''))
  return { provider, model }
}

export function clampAgentOptions(input: Partial<{ contextLength: number; maxOutputTokens: number; maxTurns: number }>) {
  const integer = (value: unknown, fallback: number, min: number, max: number) => {
    const number = Number(value)
    return Number.isFinite(number) ? Math.round(Math.min(max, Math.max(min, number))) : fallback
  }
  return {
    contextLength: integer(input.contextLength, 8192, 1024, 32768),
    maxOutputTokens: integer(input.maxOutputTokens, 1024, 128, 8192),
    maxTurns: integer(input.maxTurns, 12, 1, 24),
  }
}

export function formatPullProgress(progress: AgentPullStatus | null): string {
  if (!progress) return ''
  if (progress.status === 'done') return 'Downloaded'
  if (progress.status === 'error') return progress.error || 'Download failed'
  if (typeof progress.percent === 'number' && Number.isFinite(progress.percent)) return `${Math.max(0, Math.min(100, Math.round(progress.percent)))}%`
  if (progress.completed != null && progress.total) return `${Math.round((progress.completed / progress.total) * 100)}%`
  return progress.message || (progress.status === 'pulling' ? 'Downloading…' : 'Checking…')
}

export async function pollAgentPull(
  fetchImpl: (input: string) => Promise<{ ok?: boolean; status?: number; json(): Promise<AgentPullStatus> }>,
  model: string,
  options: { intervalMs?: number; maxAttempts?: number; signal?: AbortSignal; onProgress?: (progress: AgentPullStatus) => void } = {},
): Promise<AgentPullStatus> {
  const intervalMs = Math.max(0, options.intervalMs ?? 1000)
  const maxAttempts = Math.max(1, options.maxAttempts ?? 1800)
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (options.signal?.aborted) throw new DOMException('Download monitoring canceled', 'AbortError')
    if (intervalMs) await new Promise((resolve) => setTimeout(resolve, intervalMs))
    const response = await fetchImpl(`/api/agent-chat/ollama/pull/${encodeURIComponent(model)}`)
    if (response.ok === false) throw new Error(`Download status request failed (${response.status})`)
    const progress = await response.json()
    options.onProgress?.({ ...progress, model: progress.model || model })
    if (progress.status === 'done' || progress.status === 'error') return { ...progress, model: progress.model || model }
  }
  return { status: 'error', model, error: 'Download progress timed out' }
}

function parseSseBlock(block: string): unknown | null {
  const data = block.split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n')
  if (!data) return null
  try { return JSON.parse(data) as unknown } catch { return null }
}

export function createAgentSseParser() {
  let buffer = ''
  const parse = (flush = false): unknown[] => {
    const events: unknown[] = []
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      const event = parseSseBlock(block)
      if (event !== null) events.push(event)
    }
    if (flush && buffer.trim()) {
      const event = parseSseBlock(buffer)
      if (event !== null) events.push(event)
      buffer = ''
    }
    return events
  }
  return {
    push(chunk: string): unknown[] {
      buffer += chunk
      return parse(false)
    },
    flush(): unknown[] { return parse(true) },
  }
}
