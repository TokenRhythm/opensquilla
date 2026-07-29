import { computed, ref, type Ref } from 'vue'
import {
  waitForSessionRpcConnection,
} from '@/composables/chat/sessionBootstrapAdmission'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'

type RpcClient = {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<T>
}

export interface ChatUsageAccumulator {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  cost: number | null
  routedTurns: number
  sessionSaved: number
}

export interface UseChatUsageWidgetOptions {
  rpc: RpcClient
  readCallOptions?: RpcCallOptions
  sessionKey: Ref<string>
  tokenVizEnabled: () => boolean
}

interface PersistedUsageWidget {
  input?: number
  output?: number
  cost?: number | null
  model?: string
}

// Proactive context-window pressure, emitted by the gateway on usage.status
// (rpc_usage.py `_context_status`). pressure is 0..1; warningRatio is the
// gateway's threshold (0.85) above which the user should be warned before
// compaction kicks in. Both camelCase and snake_case are sent for compat.
export interface ContextStatus {
  contextTokens?: number
  context_tokens?: number
  contextWindowTokens?: number
  context_window_tokens?: number
  pressure?: number
  warningRatio?: number
  warning_ratio?: number
}

export interface ContextWarning {
  pct: number
  usedK: number
  windowK: number
}

interface UsageStatusSession {
  session?: string
  sessionKey?: string
  key?: string
  input_tokens?: number
  inputTokens?: number
  output_tokens?: number
  outputTokens?: number
  cache_read_tokens?: number
  cacheReadTokens?: number
  cache_write_tokens?: number
  cacheWriteTokens?: number
  cost_usd?: number
  costUsd?: number
  model?: string
  contextStatus?: ContextStatus | null
  context_status?: ContextStatus | null
}

interface UsageStatusResponse {
  sessions?: UsageStatusSession[]
}

export function createEmptyUsageAccumulator(): ChatUsageAccumulator {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    cost: null,
    routedTurns: 0,
    sessionSaved: 0,
  }
}

export function useChatUsageWidget(options: UseChatUsageWidgetOptions) {
  const usageAccum = ref<ChatUsageAccumulator>(createEmptyUsageAccumulator())
  const usageModel = ref('')
  const savingsPopupLastTs = ref(0)
  const lastSavingsPopupIdentity = ref('')
  const contextStatus = ref<ContextStatus | null>(null)

  // Surfaced as a topbar chip only once the session's context window crosses the
  // gateway's warning ratio (0.85) — a proactive heads-up before compaction,
  // independent of any compaction event. Null when below threshold or unknown.
  const contextWarning = computed<ContextWarning | null>(() => {
    const cs = contextStatus.value
    if (!cs) return null
    const pressure = Number(cs.pressure ?? 0)
    const ratio = Number(cs.warningRatio ?? cs.warning_ratio ?? 0.85)
    const windowTokens = Number(cs.contextWindowTokens ?? cs.context_window_tokens ?? 0)
    if (!(ratio > 0) || !(windowTokens > 0) || pressure < ratio) return null
    const used = Number(cs.contextTokens ?? cs.context_tokens ?? 0)
    return {
      pct: Math.round(Math.min(1, pressure) * 100),
      usedK: Math.round(used / 1000),
      windowK: Math.round(windowTokens / 1000),
    }
  })

  function resetSavingsPopupCooldown() {
    savingsPopupLastTs.value = 0
    lastSavingsPopupIdentity.value = ''
  }

  function saveWidgetState() {
    if (!options.tokenVizEnabled()) return
    if (!options.sessionKey.value) return
    try {
      localStorage.setItem('opensquilla-widget:' + options.sessionKey.value, JSON.stringify({
        input: usageAccum.value.input,
        output: usageAccum.value.output,
        cost: usageAccum.value.cost,
        model: usageModel.value,
      }))
    } catch {
      // Ignore storage failures in private or restricted contexts.
    }
  }

  function restoreWidgetState() {
    if (!options.tokenVizEnabled()) return
    if (!options.sessionKey.value) return
    try {
      const raw = localStorage.getItem('opensquilla-widget:' + options.sessionKey.value)
      if (raw) {
        const d = JSON.parse(raw) as PersistedUsageWidget
        usageAccum.value.input = d.input || 0
        usageAccum.value.output = d.output || 0
        usageAccum.value.cost = d.cost || null
        usageModel.value = d.model || ''
      }
    } catch {
      // Ignore malformed or unavailable persisted widget state.
    }
  }

  async function loadCurrentSessionUsage() {
    if (!options.sessionKey.value) return
    try {
      await waitForSessionRpcConnection(options.rpc, options.readCallOptions)
      const params = { sessionKey: options.sessionKey.value }
      const usage = options.readCallOptions
        ? await options.rpc.call<UsageStatusResponse>(
            'usage.status',
            params,
            options.readCallOptions,
          )
        : await options.rpc.call<UsageStatusResponse>('usage.status', params)
      const sessions = usage?.sessions || []
      const current = sessions.find(s => (s.session || s.sessionKey || s.key) === options.sessionKey.value)
      if (current) {
        usageAccum.value.input = Number(current.input_tokens || current.inputTokens || 0)
        usageAccum.value.output = Number(current.output_tokens || current.outputTokens || 0)
        usageAccum.value.cacheRead = Number(current.cache_read_tokens || current.cacheReadTokens || 0)
        usageAccum.value.cacheWrite = Number(current.cache_write_tokens || current.cacheWriteTokens || 0)
        const costVal = Number(current.cost_usd || current.costUsd || 0)
        usageAccum.value.cost = costVal > 0 ? costVal : null
        usageModel.value = current.model || ''
        // Refresh (or clear) the context-pressure chip for this session. Clearing
        // when absent stops a previous session's warning from sticking after a
        // switch to a session that is well under threshold.
        contextStatus.value = current.contextStatus ?? current.context_status ?? null
        saveWidgetState()
      }
    } catch {
      // Usage endpoint may be unavailable in older gateways.
    }
  }

  return {
    usageAccum,
    usageModel,
    contextWarning,
    resetSavingsPopupCooldown,
    saveWidgetState,
    restoreWidgetState,
    loadCurrentSessionUsage,
  }
}
