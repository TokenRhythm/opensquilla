import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { ConversationSemanticEventKind } from '@/modules/conversationEvents'
import { taskTerminalStatus } from '@/utils/chat/streamEvents'

// Soft content-silence watchdog for the live chat stream.
//
// The negotiated hard idle timeout ends the turn when NO events arrive at all.
// This softer watchdog measures silence of CONTENT events, but deliberately
// suspends while an ensemble phase is governed by its backend deadline.
//
// Two false-positive gates suspend the watchdog entirely:
//  - tool-in-flight: a tool_use_start without its matching tool_result means a
//    long tool execution may legitimately emit nothing for minutes.
//  - approval-pending: an unresolved exec/plugin approval blocks the run on
//    the human, not the provider.
//  - ensemble-running: proposer/aggregator lifecycle is still active.

export const SOFT_STALL_THRESHOLD_MS = 30 * 60_000
const CHECK_INTERVAL_MS = 1_000
// text_delta bursts arrive many times a second; while the banner is down a
// re-check can wait for the 1s ticker, so evaluations this close are skipped.
const EVALUATE_MIN_INTERVAL_MS = 500

// Events that prove the provider/agent is actually making progress. Everything
// outside this set (run_heartbeat, state_change, transport ticks, …) is
// liveness-only and must NOT reset the content-silence clock.
const CONTENT_EVENTS = new Set<ConversationSemanticEventKind>([
  'text-delta',
  'thinking-delta',
  'tool-use-started',
  'tool-use-delta',
  'tool-result',
  'router-decision',
  // Long compaction or ensemble phases emit only these frames; they prove
  // forward progress and must keep the banner down.
  'compaction-progress',
  'ensemble-progress',
])

export type StallSuspendReason = 'tool-running' | 'approval-pending' | 'ensemble-running' | null

export interface UseChatStallWatchdogOptions {
  /** Live-turn flag from useChatStream; the watchdog only runs while true. */
  isStreaming: Ref<boolean>
  /** Negotiated no-event grace; the soft threshold must never be lower. */
  streamIdleGraceMs?: Ref<number>
  /** Injectable clock for tests; defaults to Date.now. */
  now?: () => number
}

function payloadToolId(payload: Record<string, unknown>): string {
  const id = payload.tool_use_id ?? payload.toolUseId ?? payload.id
  return typeof id === 'string' ? id : ''
}

function payloadApprovalId(payload: Record<string, unknown>): string {
  const id = payload.approval_id ?? payload.approvalId
  return typeof id === 'string' ? id : ''
}

function ensembleMemberId(payload: Record<string, unknown>): string {
  const eventType = String(payload.event_type || '')
  const role = eventType.startsWith('aggregator_') ? 'aggregator' : 'proposer'
  return [
    role,
    String(payload.proposer_index ?? ''),
    String(payload.sample_index ?? ''),
    String(payload.proposer_label ?? ''),
    String(payload.proposer_provider ?? ''),
    String(payload.proposer_model ?? ''),
  ].join(':')
}

function ensemblePhase(phase: unknown): 'proposers' | 'aggregator' | '' {
  const normalized = String(phase || '')
  if (normalized.startsWith('ensemble_proposers')) return 'proposers'
  if (normalized.startsWith('ensemble_aggregator')) return 'aggregator'
  return ''
}

// Terminal events end the turn, so the banner clears and per-turn tracking
// resets. Task-group checkpoints remain mid-turn progress.
function isTerminalEvent(event: ConversationSemanticEventKind): boolean {
  if (event === 'turn-completed' || event === 'turn-failed') return true
  return taskTerminalStatus(event) !== ''
}

export function useChatStallWatchdog(options: UseChatStallWatchdogOptions) {
  const now = options.now ?? (() => Date.now())

  const stallActive = ref(false)
  const stallSeconds = ref(0)
  const pendingToolIds = ref<ReadonlySet<string>>(new Set())
  const pendingApprovalIds = ref<ReadonlySet<string>>(new Set())
  const activeEnsembleMemberIds = ref<ReadonlySet<string>>(new Set())
  const ensemblePhaseActive = ref(false)
  const dismissedEpisode = ref(false)
  let lastContentAt = now()
  let currentEnsemblePhase: 'proposers' | 'aggregator' | '' = ''
  let lastEvaluatedAt = 0
  let ticker: ReturnType<typeof setInterval> | null = null

  // Approval-pending wins when both gates hold: a blocked approval usually
  // coincides with an unresolved tool call, and the human is the root cause.
  const suspendReason = computed<StallSuspendReason>(() => {
    if (pendingApprovalIds.value.size > 0) return 'approval-pending'
    if (pendingToolIds.value.size > 0) return 'tool-running'
    if (ensemblePhaseActive.value || activeEnsembleMemberIds.value.size > 0) return 'ensemble-running'
    return null
  })
  const effectiveThresholdMs = computed(() => {
    const negotiated = Number(options.streamIdleGraceMs?.value || 0)
    return Math.max(
      SOFT_STALL_THRESHOLD_MS,
      Number.isFinite(negotiated) && negotiated > 0 ? negotiated : 0,
    )
  })

  function evaluate() {
    lastEvaluatedAt = now()
    if (!options.isStreaming.value || suspendReason.value !== null) {
      stallActive.value = false
      return
    }
    if (dismissedEpisode.value) {
      stallActive.value = false
      return
    }
    const nowMs = now()
    const silence = nowMs - lastContentAt
    if (silence >= effectiveThresholdMs.value) {
      stallSeconds.value = Math.floor(silence / 1000)
      stallActive.value = true
    } else {
      stallActive.value = false
    }
  }

  // A content event restarts the silence clock and drops both the banner and
  // any prior dismissal (fresh progress means the next stall is a new episode).
  function noteContent() {
    lastContentAt = now()
    dismissedEpisode.value = false
    // With no banner up, a re-evaluation this soon after the last one cannot
    // change anything — the ticker re-checks within 1s anyway. An active
    // banner always re-evaluates so fresh content clears it immediately.
    if (!stallActive.value && now() - lastEvaluatedAt < EVALUATE_MIN_INTERVAL_MS) return
    evaluate()
  }

  function addTool(id: string) {
    if (!id) return
    const next = new Set(pendingToolIds.value)
    next.add(id)
    pendingToolIds.value = next
  }

  function removeTool(id: string) {
    if (!id || !pendingToolIds.value.has(id)) return
    const next = new Set(pendingToolIds.value)
    next.delete(id)
    pendingToolIds.value = next
  }

  function addApproval(id: string) {
    if (!id) return
    const next = new Set(pendingApprovalIds.value)
    next.add(id)
    pendingApprovalIds.value = next
    evaluate()
  }

  function removeApproval(id: string) {
    if (!id || !pendingApprovalIds.value.has(id)) return
    const next = new Set(pendingApprovalIds.value)
    next.delete(id)
    pendingApprovalIds.value = next
    // The run just unblocked; measure silence from here, not from before the
    // approval, or the banner would fire the instant the human decides.
    lastContentAt = now()
    evaluate()
  }

  function setEnsemblePhase(next: 'proposers' | 'aggregator') {
    if (next !== currentEnsemblePhase) {
      currentEnsemblePhase = next
      lastContentAt = now()
      dismissedEpisode.value = false
    }
    ensemblePhaseActive.value = true
  }

  function noteEnsembleProgress(record: Record<string, unknown>) {
    const eventType = String(record.event_type || '')
    const id = ensembleMemberId(record)
    if (eventType === 'proposer_start' || eventType === 'aggregator_start') {
      // Aggregation is the next phase, so any proposer row whose finish frame
      // was lost must not keep the soft watchdog suspended after aggregation.
      const next = eventType === 'aggregator_start'
        ? new Set<string>()
        : new Set(activeEnsembleMemberIds.value)
      next.add(id)
      activeEnsembleMemberIds.value = next
      setEnsemblePhase(eventType === 'aggregator_start' ? 'aggregator' : 'proposers')
    } else if (eventType === 'proposer_finish' || eventType === 'aggregator_finish') {
      const next = new Set(activeEnsembleMemberIds.value)
      next.delete(id)
      activeEnsembleMemberIds.value = eventType === 'aggregator_finish' ? new Set() : next
      if (eventType === 'aggregator_finish' || next.size === 0) {
        ensemblePhaseActive.value = false
        currentEnsemblePhase = ''
      }
    }
    noteContent()
  }

  // Terminal: the turn is over — clear the banner and all per-turn tracking so
  // a stopped turn's unresolved tools can never suspend the next turn.
  function clearTurn() {
    pendingToolIds.value = new Set()
    pendingApprovalIds.value = new Set()
    activeEnsembleMemberIds.value = new Set()
    ensemblePhaseActive.value = false
    currentEnsemblePhase = ''
    dismissedEpisode.value = false
    lastContentAt = now()
    stallActive.value = false
    stallSeconds.value = 0
  }

  /**
   * Feed one gateway event (already filtered to the active session by the
   * caller). Content events reset the silence clock; tool start/result and
   * approval requested/resolved drive the suspension gates; terminals clear;
   * generic run_heartbeat remains liveness-only; ensemble phase heartbeats
   * suspend the soft warning while the backend deadline owns the phase.
   */
  function noteEvent(eventKind: ConversationSemanticEventKind, payload?: unknown) {
    const record = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>

    if (eventKind === 'approval-requested') {
      addApproval(payloadApprovalId(record))
      return
    }
    if (eventKind === 'approval-resolved') {
      removeApproval(payloadApprovalId(record))
      return
    }

    if (eventKind.startsWith('task-group-')) return

    if (isTerminalEvent(eventKind)) {
      clearTurn()
      return
    }

    if (eventKind === 'run-heartbeat') {
      const phase = ensemblePhase(record.phase)
      if (phase) setEnsemblePhase(phase)
      evaluate()
      return
    }

    if (eventKind === 'ensemble-progress') {
      noteEnsembleProgress(record)
      return
    }

    if (!CONTENT_EVENTS.has(eventKind)) return
    if (eventKind === 'tool-use-started') addTool(payloadToolId(record))
    else if (eventKind === 'tool-result') removeTool(payloadToolId(record))
    noteContent()
  }

  /** Full reset (session switch): forget every gate, clock, and dismissal. */
  function reset() {
    clearTurn()
  }

  /** "Keep waiting": hide this silence episode until genuine progress. */
  function dismiss() {
    dismissedEpisode.value = true
    stallActive.value = false
  }

  function stopTicker() {
    if (ticker) {
      clearInterval(ticker)
      ticker = null
    }
  }

  watch(options.isStreaming, streaming => {
    if (streaming) {
      // New turn: silence is measured from the turn start.
      lastContentAt = now()
      dismissedEpisode.value = false
      stallActive.value = false
      if (!ticker) ticker = setInterval(evaluate, CHECK_INTERVAL_MS)
    } else {
      stopTicker()
      stallActive.value = false
      dismissedEpisode.value = false
      // Unfinished tools and approvals from an ended turn must not suspend the
      // next one — a missed approval.resolved (e.g. across a WS reconnect)
      // would otherwise gate the watchdog forever.
      pendingToolIds.value = new Set()
      pendingApprovalIds.value = new Set()
      activeEnsembleMemberIds.value = new Set()
      ensemblePhaseActive.value = false
      currentEnsemblePhase = ''
    }
  }, { immediate: true })

  onScopeDispose(stopTicker)

  return {
    stallActive,
    stallSeconds,
    suspendReason,
    effectiveThresholdMs,
    noteEvent,
    reset,
    dismiss,
  }
}
