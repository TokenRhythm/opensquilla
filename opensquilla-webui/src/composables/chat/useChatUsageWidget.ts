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

export interface ContextUsage {
  pct: number
  usedK: number
  windowK: number
  /** True once pressure reaches the gateway's own warning ratio. */
  warning: boolean
}

/** The above-threshold subset, under the name it had before the reading existed. */
export type ContextWarning = ContextUsage

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

  // The reading itself, present whenever the gateway resolved a context window.
  // A number that only appears once it is nearly too late is a warning, not a
  // gauge: by the time the chip shows up at 85% the user has already spent the
  // room they would have wanted to spend differently. `warning` carries the
  // threshold so the styling can still change without the number vanishing
  // below it.
  const contextUsage = computed<ContextUsage | null>(() => {
    const cs = contextStatus.value
    if (!cs) return null
    const windowTokens = cs.contextWindowTokens
    if (!(windowTokens > 0)) return null
    const used = cs.contextTokens
    if (!(used >= 0)) return null
    const ratio = cs.warningRatio
    // `pressure` is the gateway's own ratio, but the adapter substitutes 0 for
    // a payload that omits it (`finiteNumber` in usageReportingV4), so a
    // missing ratio is indistinguishable from a genuinely empty window at this
    // layer. Falling back to the quotient whenever the counts disagree with a
    // zero keeps that case from rendering a confident "0%" beside a tooltip
    // reading 115k / 128k.
    const reported = cs.pressure > 0 ? cs.pressure : used / windowTokens
    const pressure = Number.isFinite(reported)
      ? Math.min(1, Math.max(0, reported))
      : Math.min(1, used / windowTokens)
    return {
      // Floor, not round: 99.5% must not present itself as a full window.
      pct: Math.floor(pressure * 100),
      usedK: Math.round(used / 1000),
      windowK: Math.round(windowTokens / 1000),
      warning: ratio > 0 && pressure >= ratio,
    }
  })

  // The above-threshold half, unchanged for callers that only want the warning.
  const contextWarning = computed<ContextWarning | null>(() => (
    contextUsage.value?.warning ? contextUsage.value : null
  ))

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
    contextUsage,
    contextWarning,
    resetSavingsPopupCooldown,
    saveWidgetState,
    restoreWidgetState,
    loadCurrentSessionUsage,
  }
}
