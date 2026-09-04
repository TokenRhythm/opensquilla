import { computed, ref, type Ref } from 'vue'
import type {
  UsageContextStatus,
  UsageReporting,
  UsageReportingRequestOptions,
} from '@/modules/usageReporting'

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
  usageReporting: UsageReporting
  readOptions?: UsageReportingRequestOptions
  sessionKey: Ref<string>
  tokenVizEnabled: () => boolean
}

interface PersistedUsageWidget {
  input?: number
  output?: number
  cost?: number | null
  model?: string
}

export interface ContextWarning {
  pct: number
  usedK: number
  windowK: number
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
  const usageReporting = options.usageReporting
  const usageAccum = ref<ChatUsageAccumulator>(createEmptyUsageAccumulator())
  const usageModel = ref('')
  const savingsPopupLastTs = ref(0)
  const lastSavingsPopupIdentity = ref('')
  const contextStatus = ref<UsageContextStatus | null>(null)

  // Surfaced as a topbar chip only once the session's context window crosses the
  // gateway's warning ratio (0.85) — a proactive heads-up before compaction,
  // independent of any compaction event. Null when below threshold or unknown.
  const contextWarning = computed<ContextWarning | null>(() => {
    const cs = contextStatus.value
    if (!cs) return null
    const pressure = Number(cs.pressure ?? 0)
    const ratio = cs.warningRatio
    const windowTokens = cs.contextWindowTokens
    if (!(ratio > 0) || !(windowTokens > 0) || pressure < ratio) return null
    const used = cs.contextTokens
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
      const usage = await usageReporting.status(
        options.sessionKey.value,
        options.readOptions,
      )
      const current = usage.sessions.find(s => s.sessionKey === options.sessionKey.value)
      if (current) {
        usageAccum.value.input = current.inputTokens ?? 0
        usageAccum.value.output = current.outputTokens ?? 0
        usageAccum.value.cacheRead = current.cacheReadTokens ?? 0
        usageAccum.value.cacheWrite = current.cacheWriteTokens ?? 0
        const costVal = current.costUsd
        usageAccum.value.cost = costVal != null && costVal > 0 ? costVal : null
        usageModel.value = current.model || ''
        // Refresh (or clear) the context-pressure chip for this session. Clearing
        // when absent stops a previous session's warning from sticking after a
        // switch to a session that is well under threshold.
        contextStatus.value = current.contextStatus
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
