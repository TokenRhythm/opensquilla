// PetMetering — usage + cost for the pet panel.
//
// Primary source: `done` events from the message stream (always available and
// per-turn precise — each carries input/output/reasoning/cached tokens and
// cost). Secondary: a best-effort poll of `usage.status` for lifetime totals.

import { GatewayRpc } from './rpc.js'

export interface MeteringDeps {
  rpc: () => GatewayRpc | null
}

export interface MeteringStats {
  today: { input: number; output: number; cacheCreate: number; cacheRead: number; tokens: number; cost: number; messages: number; msgs?: number }
  window5h: { tokens: number; cost: number; startTs: number; resetTs: number }
  byModel: Record<string, { tokens: number; cost: number }>
  hourly: number[]
  hourlyTok: number[]
  daily: Record<string, unknown>
  diagnostics: Record<string, unknown>
}

function startOfDay(now = Date.now()): number {
  const d = new Date(now)
  d.setHours(0, 0, 0, 0)
  return d.getTime()
}

export function createPetMetering(deps: MeteringDeps) {
  let today = {
    input: 0, output: 0, cacheCreate: 0, cacheRead: 0, tokens: 0, cost: 0, messages: 0,
  }
  let dayBoundary = startOfDay()
  const byModel: Record<string, { tokens: number; cost: number }> = {}
  let lifetime: { tokens: number; cost: number } | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null

  // 24h trend (cost / tokens by clock hour, today) + daily calendar history.
  const hourly: number[] = new Array(24).fill(0)
  const hourlyTok: number[] = new Array(24).fill(0)
  const daily: Record<string, { cost: number; tokens: number; msgs: number }> = {}

  function dKey(d: Date): string {
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${y}-${m}-${day}`
  }

  function rollDay(): void {
    const now = startOfDay()
    if (now > dayBoundary) {
      today = { input: 0, output: 0, cacheCreate: 0, cacheRead: 0, tokens: 0, cost: 0, messages: 0 }
      dayBoundary = now
      // New day: zero the hourly buckets (daily history is kept).
      for (let i = 0; i < 24; i++) { hourly[i] = 0; hourlyTok[i] = 0 }
    }
  }

  function onDone(info: { inputTokens: number; outputTokens: number; reasoningTokens: number; cachedTokens: number; costUsd: number; model: string }): void {
    rollDay()
    const tokens = info.inputTokens + info.outputTokens + info.reasoningTokens + info.cachedTokens
    today.input += info.inputTokens
    today.output += info.outputTokens
    today.cacheRead += info.cachedTokens
    today.tokens += tokens
    today.cost += info.costUsd
    today.messages += 1
    const model = info.model || 'unknown'
    const m = byModel[model] || { tokens: 0, cost: 0 }
    m.tokens += tokens
    m.cost += info.costUsd
    byModel[model] = m
    // Bucket into today's clock-hour trend + daily calendar.
    const now = new Date()
    const h = now.getHours()
    hourly[h] = (hourly[h] || 0) + info.costUsd
    hourlyTok[h] = (hourlyTok[h] || 0) + tokens
    const k = dKey(now)
    const d = daily[k] || { cost: 0, tokens: 0, msgs: 0 }
    d.cost += info.costUsd
    d.tokens += tokens
    d.msgs += 1
    daily[k] = d
  }

  async function poll(): Promise<void> {
    const rpc = deps.rpc()
    if (!rpc) return
    try {
      const res = await rpc.call<any>('usage.status', {})
      if (res && typeof res === 'object') {
        lifetime = {
          tokens: Number(res.totalTokens) || 0,
          cost: Number(res.totalCostUsd) || 0,
        }
      }
    } catch {
      // durable accounting unavailable — lifetime stays null
    }
  }

  function start(): void {
    if (pollTimer) return
    void poll()
    pollTimer = setInterval(() => { void poll() }, 30000)
    if (pollTimer.unref) pollTimer.unref()
  }

  function stop(): void {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  }

  function getStats(): MeteringStats {
    rollDay()
    // window5h: approximate the rolling 5h by summing the last 5 clock-hour
    // buckets (closest cheap proxy; exact rolling window would need per-event ts).
    let w5Tokens = 0, w5Cost = 0
    const now = new Date().getHours()
    for (let i = 0; i < 5; i++) {
      const h = (now - i + 24) % 24
      w5Tokens += hourlyTok[h] || 0
      w5Cost += hourly[h] || 0
    }
    return {
      today: { ...today, msgs: today.messages },
      window5h: { tokens: w5Tokens, cost: w5Cost, startTs: 0, resetTs: 0 },
      byModel,
      hourly: hourly.slice(),
      hourlyTok: hourlyTok.slice(),
      daily: { ...daily },
      diagnostics: {
        lifetimeTokens: lifetime ? lifetime.tokens : null,
        lifetimeCost: lifetime ? lifetime.cost : null,
        source: 'done-events + usage.status',
      },
    }
  }

  return { start, stop, onDone, getStats }
}

export type PetMetering = ReturnType<typeof createPetMetering>
