import { computed, ref, type Ref } from 'vue'
import i18n from '@/i18n'
import type {
  ChatMessage,
  ChatModelCallSegment,
  ChatRunStatus,
  ChatRunStatusSource,
  ChatStreamSegment,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatTimelineSegment,
  RawToolCallPayload,
} from '@/types/chat'
import type {
  ArtifactPayload,
  CompactionPayload,
  ToolDeltaPayload,
  ToolEndPayload,
  ToolResultPayload,
  ToolUsePayload,
} from '@/types/rpc'
import type {
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptViewState,
  StatusPart,
} from '@/types/parts'
import {
  isEmptyToolPreview,
  isInternalToolName,
  normalizeToolInputText,
  normalizeToolName,
  toolCallGroups,
  toolDisplayName,
  toolOperationKey,
  toolResultIsError,
  truncateToolPreview,
} from '@/utils/chat/toolDisplay'
import { segmentsToTimelineItems } from '@/utils/chat/segmentsToTimelineItems'
import { reconcileTextSnapshot } from '@/utils/chat/foldTurn'
import {
  normalizeModelCallSegments,
  splitTextByModelCallSegments,
} from '@/utils/chat/modelCallSegments'
import { useChatTurnLog } from '@/composables/chat/useChatTurnLog'
import type { ReasoningBlock } from '@/types/turnlog'
import { isLegacySilentSentinelOnly } from '@/utils/chat/silentSentinels'
import { createClientMessageId } from '@/utils/chat/messageIdentity'

export const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 630_000
const THINKING_DELAY_MS = 400
const THINKING_TTL_MS = 60000
// Bounds for trusting a server-stamped tool start time against the local clock
// (see serverToolStartedAt). Tolerate small server-ahead skew; reject starts older
// than this (longer than any realistic provider tool run) as skew/garbage.
const SERVER_CLOCK_TOLERANCE_MS = 5000
const MAX_TRUSTED_TOOL_AGE_MS = 60 * 60 * 1000
const SQUILLA_VERBS = ['Planning next step', 'Reading context', 'Preparing output']

// Internal phase labels stay English (they double as stable keys for dedup,
// matching, and the appended status-frame action). Localize only at the display
// boundary via this map; unmapped labels (e.g. tool-specific micro-verbs) fall
// back to their English text.
const STREAM_LABEL_KEYS: Record<string, string> = {
  Sending: 'chat.stream.sending',
  Running: 'chat.status.running',
  'Planning next step': 'chat.stream.planningNextStep',
  'Reading context': 'chat.stream.readingContext',
  'Waiting for model': 'chat.stream.waitingForModel',
  'Preparing output': 'chat.stream.preparingOutput',
  'Generating candidates': 'chat.stream.generatingCandidates',
  'Synthesizing candidates': 'chat.stream.synthesizingCandidates',
}

function localizeStreamLabel(label: string): string {
  const key = STREAM_LABEL_KEYS[label]
  return key ? i18n.global.t(key) : label
}
const SQUILLA_DWELL_MS = 2500
const STALE_SIGNAL_MS = 20000

const TOOL_PROGRESS_VERBS: Record<string, string> = {
  'web.discover': 'Discovering links',
  'web.search': 'Searching the web',
  'web.read': 'Reading a web page',
  'code.python': 'Running Python',
  'command.run': 'Running a command',
  'file.inspect': 'Inspecting files',
  'file.write': 'Writing a file',
  'file.edit': 'Editing a file',
  'artifact.create': 'Creating a file',
  'memory.search': 'Searching memory',
}

export interface UseChatStreamOptions {
  messages: Ref<ChatMessage[]>
  lastHeaderRole: Ref<string>
  aborted: Ref<boolean>
  autoScroll: Ref<boolean>
  runStatus?: Ref<ChatRunStatus>
  applySessionRunState: (source: ChatRunStatusSource | null | undefined) => void
  renderMarkdown: (
    text: string,
    opts?: {
      highlight?: boolean
      cache?: 'settled' | 'none'
      math?: 'full' | 'defer'
    },
  ) => string
  stripDirectiveTags: (text: string) => string
  stripGeneratedArtifactMarkers: (text: string) => string
  scrollToBottom: () => void
  /** Resolution view-state keyed by approval id; threaded into the fold so each
   *  interrupt part is stamped with its resolution/busy/error. The approvals
   *  composable owns the map; the stream only forwards it to the turn log. */
  interruptState?: Ref<ReadonlyMap<string, InterruptViewState>>
  /** Current hello policy. Read lazily so reconnects can replace the timeout. */
  rpcPolicy?: () => Record<string, unknown> | null | undefined
}

export interface StreamTaskClockSnapshot {
  sessionKey: string
  taskId: string
  startedAt?: number | string | null
}

function normalizedTaskStartedAt(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !value.trim()) return null
  const numeric = Number(value)
  if (Number.isSafeInteger(numeric) && numeric > 0) return numeric
  if (typeof value !== 'string') return null
  const parsed = Date.parse(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

export function streamIdleTimeoutFromPolicy(policy: Record<string, unknown> | null | undefined): number {
  const raw = policy?.webui_stream_idle_grace_ms
  return typeof raw === 'number' && Number.isFinite(raw) && raw > 0
    ? raw
    : DEFAULT_STREAM_IDLE_TIMEOUT_MS
}

export function useChatStream(options: UseChatStreamOptions) {
  const isStreaming = ref(false)
  const streamRaw = ref('')
  const streamSegments = ref<ChatStreamSegment[]>([])
  const streamArtifacts = ref<ArtifactPayload[]>([])
  const streamToolCalls = ref<ChatToolCall[]>([])
  const openToolGroups = ref<Set<string>>(new Set())
  const openToolItems = ref<Set<string>>(new Set())
  let streamToolGroupSeq = 0
  let checkpointedRaw = ''
  let checkpointedAcrossToolBoundary = false
  let activeStreamTurnId = ''
  let activeAssistantMessageId = ''
  let streamCheckpointSeq = 0
  const streamCheckpoints: Array<{
    message: ChatMessage | null
    insertIndex: number
    turnId: string
    boundaryKey: string
    messageClientId: string
    rawText: string
    applied: boolean
    modelCallId: string
    iteration: number
  }> = []
  const streamBubble = ref(false)
  const streamShowHeader = ref(false)

  const streamHasVisibleOutput = computed(() => {
    if (useReducer.value === true) {
      const folded = foldedTurn.value
      return Boolean(
        folded.rawText
        || folded.reasoningBlocks.some(block => block.text)
        || folded.toolCalls.length
        || folded.artifacts.length
        || folded.parts.some(part => part.type === 'interrupt'),
      )
    }
    return streamSegments.value.length > 0 ||
      streamToolCalls.value.length > 0 ||
      streamArtifacts.value.length > 0
  })

  const streamActivity = ref({ label: 'Sending', key: 'Sending', startedAt: 0 })
  // Turn ownership is intentionally separate from the mutable phase clock.
  // Provider/router transitions may restart the small phase timer, but the
  // disclosure header must measure one uninterrupted user-visible run.
  const streamTurnStartedAt = ref(0)
  let streamTaskClockIdentity = ''
  const streamActivityTick = ref(0)
  let streamActivityTimer: ReturnType<typeof setInterval> | null = null
  const streamRound = ref(1)
  // Provider deltas can arrive tens of thousands of times per turn. Their
  // timestamp is sampled by the one-second activity clock; making every write
  // reactive invalidated the activity ribbon (and style) once per delta even
  // though no visible label changed.
  let lastSignalAt = 0
  const toolTimes = ref(new Map<string, { startedAt: number; endedAt?: number }>())

  // The ribbon stays up for the whole run, including while tool rows render.
  const streamActivityVisible = computed(() => {
    return isStreaming.value && streamBubble.value
  })

  const streamActivityStale = computed(() => {
    streamActivityTick.value
    return lastSignalAt > 0 && Date.now() - lastSignalAt > STALE_SIGNAL_MS
  })

  // Phase narration on its own, used by the activity head where elapsed and
  // the step chip render as separate elements rather than one packed string.
  const streamPhaseLabel = computed(() => {
    streamActivityTick.value
    // A queued task is durable but has not acquired a TaskRuntime slot. Keep
    // that boundary authoritative: local animation timers must not relabel it
    // as model or router work that has not started.
    if (options.runStatus?.value.status === 'queued') {
      return i18n.global.t('chat.status.queued')
    }
    const now = Date.now()
    if (lastSignalAt > 0 && now - lastSignalAt > STALE_SIGNAL_MS) {
      // Static on purpose: this feeds a polite live region, so a ticking
      // seconds value here would be re-announced every second for the whole
      // stall. The aria-hidden elapsed chip carries the seconds instead.
      return i18n.global.t('chat.activity.stale')
    }
    return localizeStreamLabel(streamActivity.value.label)
  })

  // Elapsed seconds for the current phase, rendered as its own chip.
  const streamPhaseElapsed = computed(() => {
    streamActivityTick.value
    // task.queued has no authoritative server timestamp. Do not present the
    // optimistic browser send clock as durable queue wait time.
    if (options.runStatus?.value.status === 'queued') return ''
    const now = Date.now()
    if (lastSignalAt > 0 && now - lastSignalAt > STALE_SIGNAL_MS) {
      // During a stall the phase label is static for screen readers, so the
      // silence duration ticks here, out of the announced sentence.
      return `${Math.floor((now - lastSignalAt) / 1000)}s`
    }
    const startedAt = streamActivity.value.startedAt || now
    const seconds = Math.max(0, Math.floor((now - startedAt) / 1000))
    return `${seconds}s`
  })

  // A step in progress, not a bare round counter.
  const streamStepLabel = computed(() => `Step ${streamRound.value}`)

  const streamTimelineItems = computed<ChatStreamTimelineItem[]>(() => {
    return segmentsToTimelineItems(streamSegments.value, streamToolCalls.value, 'stream')
  })

  // Append-only turn log. ON is the production default and drives the live
  // activity surface; SHADOW is the development default and parity-checks the fold
  // while legacy refs render; only the explicit OFF kill switch skips frames
  // and restores the legacy render path.
  const turnLog = useChatTurnLog({
    renderMarkdown: options.renderMarkdown,
    toolCallGroups,
    interruptState: options.interruptState,
  })
  const {
    appendFrame,
    checkpointText,
    finalizeToolInputs,
    peekRawText,
    publish: publishTurnLog,
    resetGeneration,
    resetLog,
    useReducer,
    foldedTurn,
  } = turnLog

  const streamTurnElapsed = computed(() => {
    streamActivityTick.value
    if (options.runStatus?.value.status === 'queued') return ''
    // Snapshot/reconnect replay can rebuild a reasoning block whose server
    // start predates the fresh browser stream shell. Use that durable boundary
    // so reloading never makes a long-running turn jump back to zero.
    const replayStartedAt = foldedTurn.value.reasoningBlocks
      .map(block => block.startedAt)
      .filter(value => Number.isFinite(value) && value > 0)
      .sort((left, right) => left - right)[0]
    const localStartedAt = streamTurnStartedAt.value
    const startedAt = replayStartedAt && localStartedAt
      ? Math.min(replayStartedAt, localStartedAt)
      : replayStartedAt || localStartedAt
    if (!startedAt) return '0s'
    return `${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))}s`
  })

  function currentStreamRaw(): string {
    return useReducer.value === true ? peekRawText() : streamRaw.value
  }

  // Bound shadow-parity check: assembles this composable's legacy live surface,
  // injecting the live thinking text (owned by the event handlers) so the fold's
  // thinkingText can be compared. Reactive over events + the legacy refs when
  // run inside a watchEffect (ChatView). DEV/SHADOW-only; never throws.
  function assertLiveParity(thinkingText: Ref<string>) {
    turnLog.assertParity({
      timelineItems: streamTimelineItems,
      rawText: streamRaw,
      toolCalls: streamToolCalls,
      artifacts: streamArtifacts,
      thinkingText,
    })
  }

  const thinkingVisible = ref(false)
  const thinkingText = ref('')
  let thinkingTimer: ReturnType<typeof setInterval> | null = null
  let thinkingDelayTimer: ReturnType<typeof setTimeout> | null = null
  let thinkingStartTime = 0

  let streamIdleTimer: ReturnType<typeof setTimeout> | null = null
  let streamIdleTimerPolicyMs = 0
  let lastStreamEventAt = 0
  const streamIdleTimeoutMs = ref(DEFAULT_STREAM_IDLE_TIMEOUT_MS)
  const streamIdlePausedForApproval = ref(false)
  let streamConnectionAvailable = true
  // Stream render coalescing. Tokens arrive far faster than the display can
  // paint, so re-renders are batched onto the frame clock (requestAnimationFrame)
  // rather than a fixed setTimeout: frame-aligned flushes avoid the mid-frame
  // stutter and the visibly-stepped ~12.5fps the old 80ms timer produced.
  // MIN_FLUSH_INTERVAL caps the heavy markdown re-parse so a fast stream cannot
  // re-render every single frame; the hidden-tab fallback keeps output landing
  // when rAF is paused in a background tab.
  const MIN_FLUSH_INTERVAL_MS = 33
  const MEDIUM_FLUSH_INTERVAL_MS = 50
  const LARGE_FLUSH_INTERVAL_MS = 100
  const MEDIUM_STREAM_CHARS = 8 * 1024
  const LARGE_STREAM_CHARS = 32 * 1024
  const HIDDEN_FLUSH_FALLBACK_MS = 250
  const COARSE_REASONING_BURST_CHARS = 96
  let renderRaf: number | null = null
  let renderFallbackTimer: ReturnType<typeof setTimeout> | null = null
  let lastFlushAt = 0
  let renderDirty = false
  let reasoningCharsSinceFlush = 0
  const reasoningPresentationPending = ref(false)

  function resetStreamState() {
    streamRaw.value = ''
    streamSegments.value = []
    streamToolCalls.value = []
    streamArtifacts.value = []
    toolTimes.value = new Map()
    reasoningCharsSinceFlush = 0
    reasoningPresentationPending.value = false
    streamCheckpointSeq = 0
    streamCheckpoints.length = 0
    // Clear the live-turn log alongside the legacy refs so the next turn's fold
    // starts empty. A steer checkpoint deliberately resets only the current
    // visible segment; checkpointedRaw remains available to de-duplicate an
    // authoritative whole-turn final snapshot.
    resetLog()
  }

  function noteStreamSignal() {
    const now = Date.now()
    const recoveredFromStall = lastSignalAt > 0 && now - lastSignalAt > STALE_SIGNAL_MS
    lastSignalAt = now
    // A fresh delta after a visible stall must clear the warning immediately;
    // ordinary high-frequency progress waits for the existing one-second clock.
    if (recoveredFromStall) streamActivityTick.value++
  }

  // `key` identifies the activity phase: the elapsed counter restarts only
  // when the phase changes, so label refinements (e.g. tool arguments
  // streaming in) keep the same running clock.
  function setStreamActivity(label: string, key = label) {
    noteStreamSignal()
    const current = streamActivity.value
    let isNewPhase = false
    if (current.key === key) {
      if (!current.startedAt) {
        streamActivity.value = { label, key, startedAt: Date.now() }
        isNewPhase = true
      } else if (current.label !== label) {
        streamActivity.value = { label, key, startedAt: current.startedAt }
      }
    } else {
      streamActivity.value = { label, key, startedAt: Date.now() }
      isNewPhase = true
    }
    // Record each accepted phase transition into the append-only log so the
    // finished turn can show the activity timeline. Gated on the reducer like
    // every other frame; OFF mode appends nothing and the history stays empty.
    // Label-only refinements (same key) emit nothing — only a real phase change.
    if (isNewPhase && useReducer.value) {
      const committed = streamActivity.value
      appendFrame({ kind: 'status', action: committed.key, label: committed.label, at: committed.startedAt })
      // TurnAccumulator is intentionally non-reactive. A provider phase can be
      // the only semantic progress for minutes, so publish the status through
      // the same frame clock even when no text/tool delta follows it.
      scheduleRender()
    }
    if (!streamActivityTimer) {
      streamActivityTimer = setInterval(() => {
        streamActivityTick.value++
      }, 1000)
    }
  }

  function recordCompactionActivity(payload: CompactionPayload) {
    noteStreamSignal()
    if (!useReducer.value) return
    const rawStatus = String(payload.status || '').toLowerCase()
    const state = rawStatus === 'skipped'
      ? 'skipped'
      : rawStatus === 'stale'
        ? 'stale'
        : rawStatus === 'cancelled'
          ? 'cancelled'
          : ['completed', 'emergency_ephemeral'].includes(rawStatus)
            ? 'completed'
            : ['failed', 'error', 'timed_out'].includes(rawStatus)
              ? 'failed'
              : 'running'
    const id = String(payload.compaction_id || payload.compactionId || 'current')
    appendFrame({
      kind: 'status',
      action: 'context_compaction',
      label: '',
      at: Number(payload.emitted_at || payload.started_at || Date.now()),
      id,
      category: 'maintenance',
      state,
      source: String(payload.source || 'automatic'),
      durability: String(payload.durability || ''),
      detail: String(payload.detail || payload.phase || ''),
      reason: String(payload.reason || payload.skip_reason || ''),
    })
    scheduleRender()
  }

  function toolNarrationLabel(tc: ChatToolCall): string {
    const verb = TOOL_PROGRESS_VERBS[toolOperationKey(tc.name)]
      || `Running ${tc.name.replace(/[_-]+/g, ' ')}`
    const arg = String(tc.inputPreview || '').replace(/\s+/g, ' ').trim().replace(/^"|"$/g, '')
    if (isEmptyToolPreview(arg)) return `${verb}…`
    return `${verb} · ${truncateToolPreview(arg, 48)}`
  }

  function narrateToolCall(tc: ChatToolCall) {
    setStreamActivity(toolNarrationLabel(tc), `tool:${tc.toolId}`)
  }

  function clearStreamActivity() {
    if (streamActivityTimer) {
      clearInterval(streamActivityTimer)
      streamActivityTimer = null
    }
    streamActivityTick.value++
  }

  function startStreaming() {
    checkpointedRaw = ''
    checkpointedAcrossToolBoundary = false
    activeStreamTurnId = ''
    isStreaming.value = true
    const currentRunStatus = options.runStatus?.value
    const activeTask = currentRunStatus
      && ['queued', 'running', 'approval_pending'].includes(currentRunStatus.status)
      ? currentRunStatus.task
      : null
    options.applySessionRunState({
      run_status: 'running',
      active_task: activeTask
        ? { ...activeTask, status: 'running' }
        : { status: 'running' },
    })
    resetStreamState()
    openToolGroups.value = new Set()
    openToolItems.value = new Set()
    streamToolGroupSeq = 0
    streamRound.value = 1
    streamTaskClockIdentity = ''
    streamTurnStartedAt.value = Date.now()
    noteStreamSignal()
    streamBubble.value = true
    streamShowHeader.value = options.lastHeaderRole.value !== 'assistant'
    setStreamActivity('Sending')
    options.autoScroll.value = true
    resetStreamIdleTimer()
  }

  function reconcileStreamTaskClock(snapshot: StreamTaskClockSnapshot): boolean {
    const sessionKey = snapshot.sessionKey.trim()
    const taskId = snapshot.taskId.trim()
    const startedAt = normalizedTaskStartedAt(snapshot.startedAt)
    if (!sessionKey || !taskId || startedAt === null) return false

    const identity = `${sessionKey}\u0000${taskId}`
    if (streamTaskClockIdentity !== identity) {
      streamTaskClockIdentity = identity
      streamTurnStartedAt.value = startedAt
      return true
    }
    // A repeated hydrate for the same task may be older or richer, but it must
    // never make the visible run look younger than this client already knows.
    if (!streamTurnStartedAt.value || startedAt < streamTurnStartedAt.value) {
      streamTurnStartedAt.value = startedAt
    }
    return true
  }

  function clearStreamTaskClock() {
    streamTaskClockIdentity = ''
    streamTurnStartedAt.value = 0
  }

  function endStreaming(opts?: { reason?: string, suppressed?: boolean }) {
    // Running calls keep fragment chunks during the live phase. Materialize
    // them once before the canonical history row is detached.
    if (useReducer.value === true) {
      finalizeToolInputs()
      // Done can arrive before the frame-clock publish that would expose the
      // final reasoning delta/end. Materialize the canonical accumulator now
      // so the settled row receives the complete structured blocks even when
      // the live component never mounted for that batch.
      publishTurnLog()
    }
    const wasAborted = opts?.reason === 'aborted'
    const preReconcileText = options.stripDirectiveTags(
      options.stripGeneratedArtifactMarkers(currentStreamRaw()),
    ).trim()
    // Compatibility for gateways predating the explicit delivery contract.
    // This only recognizes a response made entirely from standalone boundary
    // marker lines; mixed substantive text stays canonical and is cleaned only
    // in the assistant presentation projection.
    const legacySentinelOnly = !wasAborted
      && isLegacySilentSentinelOnly(preReconcileText)
    const suppressText = !wasAborted
      && (opts?.suppressed === true || legacySentinelOnly)
    // `delivery=suppressed` is authoritative even if stale text deltas arrived
    // first. Reconcile to an explicit empty snapshot while leaving tool groups,
    // artifacts, and interrupts intact. The guard avoids a duplicate final-text
    // frame when the RPC handler already reconciled the Done receipt.
    if (
      suppressText
      && (
        currentStreamRaw() !== ''
        || streamSegments.value.some(segment => segment.type === 'text')
      )
    ) {
      reconcileFinalText('')
    }
    hideThinkingIndicator()
    clearStreamActivity()
    clearStreamIdleTimer()
    // Cancel any frame queued just before the turn ended so it cannot fire a
    // stray scroll after reset (mirrors resetStreamForRouterReplay).
    clearRenderTimer()
    streamIdlePausedForApproval.value = false

    if (streamBubble.value) {
      // `streamRaw` is the canonical backend answer. Do not guess that visible
      // Markdown/XML is a leaked tool protocol: doing so mutates local history,
      // copy/export/share, and can disagree with the durable server transcript.
      const cleanedText = options.stripDirectiveTags(
        options.stripGeneratedArtifactMarkers(currentStreamRaw()),
      ).trim()

      // After Stop, partial streamed output (text, tool rows, artifacts) is
      // kept; only a bubble with nothing visible at all is dropped.
      const terminalFold = foldedTurn.value
      const foldedInterrupts = terminalFold.parts.filter(
        (part): part is Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }> =>
          part.type === 'interrupt',
      )
      const terminalToolCalls = useReducer.value === true
        ? terminalFold.toolCalls
        : streamToolCalls.value
      const terminalAt = Date.now()
      const terminalReasoningBlocks: ReasoningBlock[] = terminalFold.reasoningBlocks.map(block => ({
        ...block,
        status: block.status === 'streaming'
          ? wasAborted ? 'interrupted' : 'completed'
          : block.status,
        endedAt: block.endedAt ?? terminalAt,
      }))
      const emptyStream = !cleanedText
        && terminalReasoningBlocks.every(block => !block.text)
        && streamArtifacts.value.length === 0
        && terminalToolCalls.length === 0
        && foldedInterrupts.length === 0
        && (suppressText || terminalFold.statusHistory.length === 0)
      if (emptyStream) {
        streamBubble.value = false
        isStreaming.value = false
        resetStreamState()
        checkpointedRaw = ''
        checkpointedAcrossToolBoundary = false
        activeStreamTurnId = ''
        clearStreamTaskClock()
        activeAssistantMessageId = ''
        return
      }

      options.messages.value.push({
        role: 'assistant',
        clientId: createClientMessageId(),
        text: cleanedText,
        ts: new Date().toISOString(),
        messageId: activeAssistantMessageId || undefined,
        turnId: activeStreamTurnId || undefined,
        artifacts: streamArtifacts.value.slice(),
        tool_calls: terminalToolCalls.map(streamToolCallToHistoryCall),
        timeline: useReducer.value
          ? terminalFold.timelineSegments.slice()
          : streamTimelineSnapshot(cleanedText),
        interrupts: foldedInterrupts.map(part => ({ ...part })),
        // Detach the fold's activity history from the about-to-be-reset log. In
        // OFF mode this is [], so the field is harmless. A status-only terminal
        // turn remains visible because its activity is the useful result.
        statusHistory: terminalFold.statusHistory.slice(),
        reasoningBlocks: terminalReasoningBlocks,
        reasoningPresentationPending: reasoningPresentationPending.value || undefined,
        interrupted: wasAborted || undefined,
      })
      // Replacing the live block with canonical Markdown/KaTeX can change its
      // measured height after the final stream flush. Pin once after Vue's
      // settled render (and TextPart's post-flush decoration), while preserving
      // the existing autoScroll guard for readers who moved away from the edge.
      if (options.autoScroll.value) options.scrollToBottom()
    }

    streamBubble.value = false
    isStreaming.value = false
    resetStreamState()
    checkpointedRaw = ''
    checkpointedAcrossToolBoundary = false
    activeStreamTurnId = ''
    clearStreamTaskClock()
    activeAssistantMessageId = ''
  }

  function restoreStatusHistory(entries: readonly StatusPart[]) {
    if (!entries.length || useReducer.value === false) return
    if (!isStreaming.value) startStreaming()
    // Consume matching live occurrences one-for-one. A Set would erase valid
    // repeated retry_wait/retrying phases from the durable terminal trace.
    const liveActionCounts = new Map<string, number>()
    for (const entry of foldedTurn.value.statusHistory) {
      liveActionCounts.set(entry.action, (liveActionCounts.get(entry.action) || 0) + 1)
    }
    const currentAction = streamActivity.value.key
    if (currentAction && !liveActionCounts.has(currentAction)) {
      liveActionCounts.set(currentAction, 1)
    }
    for (const entry of entries) {
      if (!entry.action) continue
      const liveCount = liveActionCounts.get(entry.action) || 0
      if (liveCount > 0) {
        liveActionCounts.set(entry.action, liveCount - 1)
        continue
      }
      appendFrame({
        kind: 'status',
        action: entry.action,
        label: entry.label,
        at: entry.at,
        ...(entry.durability ? { durability: entry.durability } : {}),
      })
    }
    publishTurnLog()
  }

  function checkpointForUserMessage(turnId: string, boundaryKey = '') {
    if (!isStreaming.value || !streamBubble.value) return
    const existing = boundaryKey
      ? streamCheckpoints.find(checkpoint => checkpoint.boundaryKey === boundaryKey)
      : undefined
    if (existing) {
      existing.turnId = turnId || existing.turnId
      existing.insertIndex = boundaryMessageIndex(boundaryKey)
      setCheckpointText(existing, existing.rawText)
      return
    }
    activeStreamTurnId = turnId || activeStreamTurnId

    clearRenderTimer()
    const rawText = currentStreamRaw()
    const checkpoint: typeof streamCheckpoints[number] = {
      message: null,
      insertIndex: boundaryMessageIndex(boundaryKey),
      turnId,
      boundaryKey,
      messageClientId: `live-steer-checkpoint:${boundaryKey || streamCheckpointSeq++}`,
      rawText,
      applied: false,
      modelCallId: '',
      iteration: 0,
    }
    streamCheckpoints.push(checkpoint)
    // Reconnect/session restoration may retain the optimistic checkpoint row
    // while resetLiveTurnState clears only its in-memory lifecycle record.
    // Adopt that stable row instead of inserting a duplicate.
    setCheckpointText(checkpoint, rawText)

    checkpointedAcrossToolBoundary = checkpointedAcrossToolBoundary || Boolean(
      streamToolCalls.value.length
      || streamSegments.value.some(segment => segment.type === 'tool-group'),
    )
    checkpointedRaw += currentStreamRaw()
    // A steer is another user message inside the same turn. Split only the
    // answer text so it stays chronologically above that message; the running
    // tools, artifacts, interrupts, reasoning and status history continue to
    // belong to the one live activity disclosure below it.
    if (useReducer.value !== true) streamRaw.value = ''
    streamSegments.value = streamSegments.value.filter(
      segment => segment.type !== 'text',
    )
    checkpointText()
    resetStreamIdleTimer()
  }

  function acknowledgeSteerBoundary(
    boundaryKey: string,
    modelCallId = '',
    iteration = 0,
  ) {
    const checkpoint = streamCheckpoints.find(candidate =>
      !candidate.applied
      && (!boundaryKey || candidate.boundaryKey === boundaryKey),
    ) || streamCheckpoints.find(candidate => !candidate.applied)
    if (!checkpoint) return
    checkpoint.applied = true
    if (modelCallId) checkpoint.modelCallId = modelCallId
    checkpoint.iteration = iteration
  }

  function boundaryMessageIndex(boundaryKey: string): number {
    if (!boundaryKey) return options.messages.value.length
    const userIndex = options.messages.value.findIndex(message =>
      message.role === 'user'
      && (
        message.clientId === boundaryKey
        || message.messageId === boundaryKey
        || message.steerClientMessageId === boundaryKey
        || message.steerClientRequestId === boundaryKey
      ),
    )
    return userIndex >= 0 ? userIndex : options.messages.value.length
  }

  function checkpointMessageInsertIndex(checkpoint: typeof streamCheckpoints[number]): number {
    const userIndex = boundaryMessageIndex(checkpoint.boundaryKey)
    if (userIndex < options.messages.value.length) return userIndex
    return Math.min(checkpoint.insertIndex, options.messages.value.length)
  }

  function setCheckpointText(
    checkpoint: typeof streamCheckpoints[number],
    rawText: string,
  ) {
    const cleanedText = options.stripDirectiveTags(
      options.stripGeneratedArtifactMarkers(rawText),
    ).trim()
    let currentIndex = checkpoint.message
      ? options.messages.value.indexOf(checkpoint.message)
      : -1
    if (currentIndex < 0) {
      currentIndex = options.messages.value.findIndex(message =>
        message.role === 'assistant'
        && message.clientId === checkpoint.messageClientId,
      )
    }
    if (!cleanedText) {
      if (currentIndex >= 0) options.messages.value.splice(currentIndex, 1)
      checkpoint.message = null
      return
    }
    const timeline: ChatTimelineSegment[] = [{ type: 'text', raw: cleanedText }]
    if (currentIndex >= 0) {
      const current = options.messages.value[currentIndex]!
      current.text = cleanedText
      current.timeline = timeline
      const desiredIndex = checkpointMessageInsertIndex(checkpoint)
      const moveTo = desiredIndex > currentIndex ? desiredIndex - 1 : desiredIndex
      if (moveTo !== currentIndex) {
        options.messages.value.splice(currentIndex, 1)
        options.messages.value.splice(moveTo, 0, current)
      }
      checkpoint.message = current
      return
    }
    const insertIndex = checkpointMessageInsertIndex(checkpoint)
    options.messages.value.splice(insertIndex, 0, {
      role: 'assistant',
      text: cleanedText,
      ts: new Date().toISOString(),
      clientId: checkpoint.messageClientId,
      turnId: checkpoint.turnId,
      timeline,
    })
    checkpoint.message = options.messages.value[insertIndex]!
  }

  function appendDeltaBeforeAppliedSteer(text: string): boolean {
    const checkpoint = streamCheckpoints.find(candidate => !candidate.applied)
    if (!checkpoint) return false

    let deltaText = typeof text === 'string' ? text : ''
    if (checkpoint.rawText && deltaText === checkpoint.rawText) return true
    if (checkpoint.rawText && deltaText.startsWith(checkpoint.rawText)) {
      deltaText = deltaText.slice(checkpoint.rawText.length)
    } else if (checkpointedRaw && deltaText.startsWith(checkpointedRaw)) {
      deltaText = deltaText.slice(checkpointedRaw.length)
    }
    if (!deltaText) return true

    checkpoint.rawText += deltaText
    checkpointedRaw += deltaText
    setCheckpointText(checkpoint, checkpoint.rawText)
    noteStreamSignal()
    scheduleRender()
    return true
  }

  function resetStreamForRouterReplay() {
    resetStreamState()
    streamToolGroupSeq = 0
    streamBubble.value = true
    streamShowHeader.value = options.lastHeaderRole.value !== 'assistant'
    setStreamActivity('Switching model')
    clearRenderTimer()
  }

  function resetLiveTurnState() {
    hideThinkingIndicator()
    clearStreamActivity()
    clearStreamIdleTimer()
    streamIdlePausedForApproval.value = false
    isStreaming.value = false
    resetStreamState()
    streamBubble.value = false
    checkpointedRaw = ''
    checkpointedAcrossToolBoundary = false
    activeStreamTurnId = ''
    clearStreamTaskClock()
    activeAssistantMessageId = ''
  }

  function setAssistantMessageId(messageId: string) {
    activeAssistantMessageId = typeof messageId === 'string' ? messageId.trim() : ''
  }

  /**
   * Reset only the answer generation inside the existing live bubble. This is
   * deliberately separate from resetLiveTurnState(): completed tool results
   * and artifacts remain visible while pending tool calls and old text/reasoning
   * are replaced by the authoritative new-generation snapshot.
   */
  function resetAnswerGeneration(optionsArg: {
    textSnapshot?: string
    preserveCompletedTools?: boolean
  } = {}) {
    const preserveCompletedTools = optionsArg.preserveCompletedTools !== false
    const completedToolIds = new Set(
      streamToolCalls.value.filter(call => !call.isRunning).map(call => call.toolId),
    )
    const keptToolCalls = preserveCompletedTools
      ? streamToolCalls.value.filter(call => completedToolIds.has(call.toolId))
      : []
    const keptToolIds = new Set(keptToolCalls.map(call => call.toolId))
    const keptGroupIds = new Set(
      keptToolCalls.flatMap(call => call.groupId ? [call.groupId] : []),
    )

    clearRenderTimer()
    streamRaw.value = typeof optionsArg.textSnapshot === 'string'
      ? optionsArg.textSnapshot
      : ''
    streamSegments.value = streamSegments.value.filter((segment) => {
      if (segment.type === 'text') return false
      if (segment.type === 'tool-group') return keptGroupIds.has(segment.groupId || '')
      return true
    })
    streamToolCalls.value = keptToolCalls
    toolTimes.value = new Map(
      [...toolTimes.value.entries()].filter(([toolId]) => keptToolIds.has(toolId)),
    )
    openToolGroups.value = new Set(
      [...openToolGroups.value].filter(groupId => keptGroupIds.has(groupId)),
    )
    openToolItems.value = new Set(
      [...openToolItems.value].filter(toolId => keptToolIds.has(toolId)),
    )
    checkpointedRaw = ''
    checkpointedAcrossToolBoundary = false
    reasoningCharsSinceFlush = 0
    reasoningPresentationPending.value = false
    streamRound.value = 1
    isStreaming.value = true
    streamBubble.value = true
    noteStreamSignal()

    if (streamRaw.value) {
      streamSegments.value.push({
        type: 'text',
        raw: streamRaw.value,
        html: '',
        dirty: true,
        presentation: 'answer',
      })
    }
    resetGeneration({
      textSnapshot: streamRaw.value,
      preserveCompletedTools,
    })
    scheduleRender()
  }

  function normalizeIncomingTextDelta(text: string): string {
    let raw = typeof text === 'string' ? text : ''
    if (
      checkpointedAcrossToolBoundary
      && checkpointedRaw
      && raw.startsWith(checkpointedRaw)
    ) {
      raw = raw.slice(checkpointedRaw.length)
    }
    const currentRaw = currentStreamRaw()
    if (!raw || !currentRaw) return raw

    const sawToolBoundary =
      streamToolCalls.value.length > 0 ||
      streamSegments.value.some(seg => seg.type === 'tool-group')
    if (!sawToolBoundary) return raw

    if (raw === currentRaw) return ''
    if (raw.startsWith(currentRaw)) return raw.slice(currentRaw.length)
    return raw
  }

  function appendDelta(
    text: string,
    presentation: 'intermediate' | 'answer' = 'answer',
  ) {
    if (options.aborted.value) return
    if (appendDeltaBeforeAppliedSteer(text)) return
    const deltaText = normalizeIncomingTextDelta(text)
    if (!deltaText) return
    if (!isStreaming.value) startStreaming()
    setStreamActivity('Writing reply', `write:${streamRound.value}`)
    if (useReducer.value !== true) streamRaw.value += deltaText

    if (useReducer.value !== true) {
      const lastSegment = streamSegments.value[streamSegments.value.length - 1]
      if (
        !lastSegment
        || lastSegment.type !== 'text'
        || lastSegment.presentation !== presentation
      ) {
        streamSegments.value.push({
          type: 'text',
          raw: deltaText,
          html: '',
          dirty: true,
          presentation,
        })
      } else {
        lastSegment.raw = (lastSegment.raw || '') + deltaText
        lastSegment.dirty = true
      }
    }

    if (useReducer.value) appendFrame({ kind: 'text', text: deltaText, presentation })
    scheduleRender()
  }

  // Coalesce stream-driven DOM work (markdown re-render + autoscroll) onto the
  // frame clock so heavy tool turns do not re-render per event and the reveal
  // stays smooth and vsync-aligned.
  function scheduleRender() {
    renderDirty = true
    if (renderRaf !== null || renderFallbackTimer !== null) return
    // rAF is throttled/paused in background tabs — fall back to a timer there so
    // streamed output still lands while the tab is hidden.
    if (typeof document !== 'undefined' && document.hidden) {
      renderFallbackTimer = setTimeout(runFlush, HIDDEN_FLUSH_FALLBACK_MS)
      return
    }
    if (typeof requestAnimationFrame === 'function') {
      renderRaf = requestAnimationFrame(onRenderFrame)
    } else {
      renderFallbackTimer = setTimeout(runFlush, MIN_FLUSH_INTERVAL_MS)
    }
  }

  function noteReasoningPresentationDelta(text: string) {
    reasoningCharsSinceFlush += text.length
    if (reasoningCharsSinceFlush > COARSE_REASONING_BURST_CHARS) {
      reasoningPresentationPending.value = true
    }
  }

  function completeReasoningPresentation() {
    reasoningPresentationPending.value = false
  }

  function onRenderFrame() {
    renderRaf = null
    // Coalesce bursts to the frame clock but cap the heavy re-parse: if the last
    // flush was very recent, wait for the next frame instead of re-rendering the
    // whole growing segment again this frame.
    if (Date.now() - lastFlushAt < visibleFlushIntervalMs()) {
      renderRaf = requestAnimationFrame(onRenderFrame)
      return
    }
    runFlush()
  }

  function runFlush() {
    renderRaf = null
    renderFallbackTimer = null
    lastFlushAt = Date.now()
    flushRender()
  }

  function flushRender() {
    if (!renderDirty) return

    // The reducer is the production render source. Do not also parse the
    // invisible legacy segment: that used to double the Markdown work on every
    // flush. DEV shadow and the explicit kill switch keep the legacy renderer
    // for parity/rollback.
    if (useReducer.value === false) {
      for (const seg of streamSegments.value) {
        if (seg.type === 'text' && seg.dirty) {
          seg.html = options.renderMarkdown(stabilizeStreamingMarkdown(seg.raw || ''), {
            highlight: false,
            cache: 'none',
            math: 'defer',
          })
          seg.dirty = false
        }
      }
    }

    publishTurnLog()
    reasoningCharsSinceFlush = 0
    if (useReducer.value === 'shadow') {
      // Shadow mode still compares the legacy shape, but reuses the one
      // accumulator render instead of parsing identical Markdown twice.
      const foldedText = foldedTurn.value.timelineItems.filter(
        item => item.type === 'text',
      )
      let textIndex = 0
      for (const segment of streamSegments.value) {
        if (segment.type !== 'text') continue
        const rendered = foldedText[textIndex++]
        if (rendered?.type === 'text') segment.html = rendered.html
        segment.dirty = false
      }
    }
    renderDirty = false
    if (options.autoScroll.value) options.scrollToBottom()
  }

  function visibleFlushIntervalMs(): number {
    const size = currentStreamRaw().length
    if (size >= LARGE_STREAM_CHARS) return LARGE_FLUSH_INTERVAL_MS
    if (size >= MEDIUM_STREAM_CHARS) return MEDIUM_FLUSH_INTERVAL_MS
    return MIN_FLUSH_INTERVAL_MS
  }

  // Render-only stabilization of incomplete markdown during streaming: an
  // unterminated fenced code block (odd number of ``` fences) is temporarily
  // closed so it renders as a single <pre> across flushes rather than collapsing
  // back to a paragraph until the closing fence arrives. Operates on a copy; the
  // committed message still re-renders the true raw text on turn end.
  function stabilizeStreamingMarkdown(raw: string): string {
    const fenceCount = (raw.match(/^[ \t]*```/gm) || []).length
    return fenceCount % 2 === 1 ? `${raw}\n\`\`\`` : raw
  }

  function showThinkingIndicator() {
    if (streamBubble.value) {
      if (!streamHasVisibleOutput.value) setStreamActivity('Planning next step')
      return
    }
    if (thinkingVisible.value || thinkingDelayTimer) return
    thinkingStartTime = Date.now()
    thinkingDelayTimer = setTimeout(() => {
      thinkingDelayTimer = null
      if (streamBubble.value) return
      thinkingVisible.value = true
      updateThinkingText()
      thinkingTimer = setInterval(updateThinkingText, 1000)
    }, THINKING_DELAY_MS)
  }

  function updateThinkingText() {
    const elapsed = Date.now() - thinkingStartTime
    const seconds = Math.floor(elapsed / 1000)
    const verb = SQUILLA_VERBS[Math.floor(elapsed / SQUILLA_DWELL_MS) % SQUILLA_VERBS.length]
    thinkingText.value = `${verb} · ${seconds}s`
    if (seconds >= THINKING_TTL_MS / 1000) {
      hideThinkingIndicator()
      options.messages.value.push({ role: 'system', text: 'Still waiting for agent response...', ts: new Date().toISOString() })
    }
  }

  function hideThinkingIndicator() {
    if (thinkingDelayTimer) { clearTimeout(thinkingDelayTimer); thinkingDelayTimer = null }
    if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null }
    thinkingVisible.value = false
  }

  function resetStreamIdleTimer(opts: { progress?: boolean } = {}) {
    // Every gateway event funnels through here, including run heartbeats, so
    // keep the hard connection timeout alive. Generic heartbeats are not model
    // progress, however, and must not hide a 20s provider-progress stall.
    lastStreamEventAt = Date.now()
    if (opts.progress !== false) noteStreamSignal()
    const nextTimeoutMs = streamIdleTimeoutFromPolicy(options.rpcPolicy?.())
    streamIdleTimeoutMs.value = nextTimeoutMs
    if (
      !isStreaming.value
      || streamIdlePausedForApproval.value
      || !streamConnectionAvailable
      || (typeof document !== 'undefined' && document.hidden)
    ) {
      clearStreamIdleTimer()
      return
    }
    // Keep one watchdog alive across a delta flood. Clearing and allocating a
    // new 630s timer for every reasoning/tool/text fragment retained tens of
    // thousands of cancelled timer records until V8's next collection. At the
    // deadline, compare against the latest event and arm only the remaining
    // interval; policy changes remain immediate.
    if (streamIdleTimer && streamIdleTimerPolicyMs === nextTimeoutMs) return
    clearStreamIdleTimer()
    armStreamIdleTimer(nextTimeoutMs)
  }

  function armStreamIdleTimer(delayMs: number) {
    streamIdleTimerPolicyMs = streamIdleTimeoutMs.value
    streamIdleTimer = setTimeout(() => {
      streamIdleTimer = null
      if (
        !isStreaming.value
        || streamIdlePausedForApproval.value
        || !streamConnectionAvailable
        || (typeof document !== 'undefined' && document.hidden)
      ) return
      const idleForMs = Math.max(0, Date.now() - lastStreamEventAt)
      const remainingMs = streamIdleTimeoutMs.value - idleForMs
      if (remainingMs > 0) {
        armStreamIdleTimer(remainingMs)
        return
      }
      endStreaming()
      const seconds = Math.round(streamIdleTimeoutMs.value / 1000)
      options.messages.value.push({ role: 'error', text: `Response timed out -- no events received for ${seconds}s`, ts: new Date().toISOString() })
    }, Math.max(1, delayMs))
  }

  function clearStreamIdleTimer() {
    if (streamIdleTimer) clearTimeout(streamIdleTimer)
    streamIdleTimer = null
    streamIdleTimerPolicyMs = 0
  }

  function setStreamConnectionAvailable(available: boolean) {
    streamConnectionAvailable = available
    if (!available) {
      clearStreamIdleTimer()
      return
    }
    resetStreamIdleTimer({ progress: false })
  }

  function handleVisibilityChange() {
    if (typeof document === 'undefined') return
    if (document.hidden) clearStreamIdleTimer()
    else resetStreamIdleTimer({ progress: false })
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', handleVisibilityChange)
  }

  // The server-stamped tool start time (epoch ms), or null when absent/invalid.
  // 0 is the backend's "unstamped" sentinel and is treated as absent.
  //
  // The elapsed timer differences this server start against the client's Date.now(),
  // so a gateway whose wall clock is skewed from the browser's (common on remote /
  // non-NTP boxes) would distort elapsed. We bound that: a start in the future
  // (server ahead) or implausibly far in the past (server behind / garbage) is not
  // trusted and we fall back to the local clock — capping any residual error rather
  // than letting a skewed clock render a wildly wrong duration. On synced clocks
  // (e.g. the gateway serving its own UI) this is exact.
  function serverToolStartedAt(payload: ToolUsePayload | ToolResultPayload): number | null {
    const raw = (payload as ToolUsePayload).started_at
    if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0) return null
    const now = Date.now()
    if (raw > now + SERVER_CLOCK_TOLERANCE_MS) return null
    if (raw < now - MAX_TRUSTED_TOOL_AGE_MS) return null
    return raw
  }

  function ensureStreamToolCall(payload: ToolUsePayload | ToolResultPayload, optionsArg: { running: boolean }): ChatToolCall | null {
    if (!payload) return null
    const name = normalizeToolName(payload)
    if (!name) return null
    if (isInternalToolName(name)) return null
    if (!isStreaming.value) startStreaming()
    const input = normalizeToolInputText(payload)
    const toolId = payload.tool_use_id || payload.toolUseId || payload.id || `${name}:${payload.stream_seq || Date.now()}`

    const existing = streamToolCalls.value.find(tc => tc.toolId === toolId)
    if (existing) {
      if (input) {
        existing.inputRaw = input
        existing.inputPreview = truncateToolPreview(input, 200)
        existing.displayName = toolDisplayName(existing.name, input)
      }
      // A running ensure on an existing call mirrors to the fold so an input
      // refinement (re-narration) stays in sync; result-only ensures emit no
      // tool-start (the tool-result frame finalizes the call there).
      if (useReducer.value && optionsArg.running) {
        appendFrame({ kind: 'tool-start', toolId, name: existing.name, input: existing.inputRaw || '', at: Date.now() })
      }
      return existing
    }

    // Only calls observed from their start get a wall clock; result-only
    // calls (and replayed history) never show a fabricated elapsed time.
    // Prefer the server-stamped start time (epoch ms) so the elapsed timer is
    // stable across page switches / stream replay, where the component remounts
    // and would otherwise restart the clock from now (issue #329). Fall back to
    // the local clock when the server did not stamp one.
    if (optionsArg.running && !toolTimes.value.has(toolId)) {
      const serverStartedAt = serverToolStartedAt(payload)
      toolTimes.value.set(toolId, { startedAt: serverStartedAt ?? Date.now() })
    }

    const operationKey = toolOperationKey(name)
    const lastSegment = streamSegments.value[streamSegments.value.length - 1]
    const groupId = lastSegment?.type === 'tool-group' && lastSegment.operationKey === operationKey && lastSegment.groupId
      ? lastSegment.groupId
      : `stream:tool-group:${operationKey}:${streamToolGroupSeq++}`

    if (lastSegment?.type !== 'tool-group' || lastSegment.groupId !== groupId) {
      streamSegments.value.push({ type: 'tool-group', groupId, operationKey })
    }

    const call: ChatToolCall = {
      toolId,
      name,
      displayName: toolDisplayName(name, input),
      groupId,
      inputRaw: input,
      inputPreview: truncateToolPreview(input, 200),
      isRunning: optionsArg.running,
      status: '',
      isError: false,
      result: '',
      resultPreview: '',
      isOpen: false,
    }
    streamToolCalls.value.push(call)
    // Running creates emit a tool-start (fold stamps the clock here, matching the
    // toolTimes seed above). Result-only creates emit nothing: the tool-result
    // frame creates the call in the fold without a clock.
    if (useReducer.value && optionsArg.running) {
      appendFrame({ kind: 'tool-start', toolId, name, input, at: Date.now() })
    }
    return call
  }

  function appendToolCall(payload: ToolUsePayload) {
    const tc = ensureStreamToolCall(payload, { running: true })
    if (!tc) return
    narrateToolCall(tc)
    scheduleRender()
  }

  function appendToolDelta(payload: ToolDeltaPayload) {
    if (!payload || options.aborted.value) return
    const toolId = payload.tool_use_id || payload.toolUseId || payload.id || ''
    const fragment = payload.json_fragment ?? payload.jsonFragment ?? payload.fragment ?? ''
    const fragmentText = typeof fragment === 'string' ? fragment : String(fragment || '')
    if (!toolId || !fragmentText) return

    const existing = streamToolCalls.value.find(t => t.toolId === toolId)
    const tc = existing || ensureStreamToolCall(payload, { running: true })
    if (!tc) return

    // The accumulator owns the complete production input. The legacy call is
    // retained only as bounded narration metadata; concatenating the same 10k
    // fragment prefix here doubled both allocation churn and retained state.
    const previousInput = tc.inputRaw || ''
    const nextInput = useReducer.value === true
      ? previousInput.length >= 200
        ? previousInput
        : `${previousInput}${fragmentText}`.slice(0, 200)
      : `${previousInput}${fragmentText}`
    if (nextInput !== previousInput) {
      tc.inputRaw = nextInput
      if (!isEmptyToolPreview(nextInput)) {
        tc.inputPreview = truncateToolPreview(nextInput, 200)
        tc.displayName = toolDisplayName(tc.name, nextInput)
      }
    }
    if (tc.isRunning && (useReducer.value !== true || nextInput !== previousInput)) {
      narrateToolCall(tc)
    }
    // The fold concats the same fragment onto the same call's inputRaw. When
    // this delta created the call, ensureStreamToolCall already emitted the
    // seeding tool-start above, so the call exists in the fold before this.
    if (useReducer.value) appendFrame({ kind: 'tool-delta', toolId, fragment: fragmentText })
    scheduleRender()
  }

  function appendToolEnd(payload: ToolEndPayload) {
    if (!payload || options.aborted.value) return
    const toolId = payload.tool_use_id || payload.toolUseId || payload.id || ''
    if (!toolId) return
    const tc = streamToolCalls.value.find(t => t.toolId === toolId)
      || ensureStreamToolCall(payload, { running: true })
    if (!tc) return
    if (payload.arguments && typeof payload.arguments === 'object') {
      const input = JSON.stringify(payload.arguments)
      tc.inputRaw = input
      tc.inputPreview = truncateToolPreview(input, 200)
      tc.displayName = toolDisplayName(tc.name, input)
    }
    scheduleRender()
  }

  function appendToolResult(payload: ToolResultPayload) {
    if (!payload) return
    const name = normalizeToolName(payload)
    if (name && isInternalToolName(name)) return
    if (!isStreaming.value) startStreaming()
    const raw = payload.result || payload.content || payload.output || ''
    const content = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2)
    const toolId = payload.tool_use_id || payload.toolUseId || payload.id || ''

    const tc = streamToolCalls.value.find(t => t.toolId === toolId) || ensureStreamToolCall(payload, { running: false })
    if (tc) {
      const input = normalizeToolInputText(payload)
      if (input) {
        tc.inputRaw = input
        tc.inputPreview = truncateToolPreview(input, 200)
        tc.displayName = toolDisplayName(tc.name, input)
      }
      tc.isRunning = false
      tc.status = toolResultIsError(payload) ? 'error' : 'success'
      tc.isError = toolResultIsError(payload)
      tc.result = content
      tc.resultPreview = truncateToolPreview(content, 200)

      const timing = toolTimes.value.get(tc.toolId)
      if (timing && !timing.endedAt) timing.endedAt = Date.now()

      // Mirror the finalized call. A result-only call (no prior tool-start) is
      // created by the fold here without a clock; the result frame carries the
      // same payload input legacy used so a result-only fold call seeds it.
      if (useReducer.value) {
        appendFrame({ kind: 'tool-result', toolId: tc.toolId, name: tc.name, result: content, isError: tc.isError, input, at: Date.now() })
      }

      const stillRunning = streamToolCalls.value.find(t => t.isRunning)
      if (stillRunning) {
        narrateToolCall(stillRunning)
      } else {
        // All tools in the batch came back: the model starts its next round.
        streamRound.value++
        setStreamActivity('Planning next step', `plan:${streamRound.value}`)
      }
    }

    scheduleRender()
  }

  function streamToolElapsedText(call: Pick<ChatToolCall, 'toolId'>): string {
    streamActivityTick.value
    const timing = toolTimes.value.get(call.toolId)
    if (!timing) return ''
    const end = timing.endedAt ?? Date.now()
    const seconds = Math.max(0, end - timing.startedAt) / 1000
    if (timing.endedAt && seconds < 10) return `${seconds.toFixed(1)}s`
    const whole = Math.floor(seconds)
    if (whole < 60) return `${whole}s`
    return `${Math.floor(whole / 60)}m ${whole % 60}s`
  }

  function streamToolCallToHistoryCall(tc: ChatToolCall): RawToolCallPayload {
    return {
      id: tc.toolId,
      toolId: tc.toolId,
      tool_use_id: tc.toolId,
      name: tc.name,
      tool_name: tc.name,
      input: tc.inputRaw || tc.inputPreview,
      groupId: tc.groupId,
      result: tc.result,
      is_error: tc.isError,
      isError: tc.isError,
      execution_status: tc.status ? { status: tc.status } : undefined,
    }
  }

  function streamTimelineSnapshot(fallbackText = ''): ChatTimelineSegment[] {
    const segments = streamSegments.value
      .flatMap((seg): ChatTimelineSegment[] => {
        if (seg.type === 'text') {
          const raw = String(seg.raw || '')
          return raw ? [{ type: 'text', raw, presentation: seg.presentation }] : []
        }
        if (seg.type === 'tool-group') {
          return [{
            type: 'tool-group',
            groupId: seg.groupId,
            operationKey: seg.operationKey,
          }]
        }
        return []
      })
    if (segments.length === 0 && fallbackText) {
      return [{ type: 'text', raw: fallbackText, presentation: 'answer' }]
    }
    return segments
  }

  function appendArtifact(payload: ArtifactPayload) {
    if (!payload) return
    noteStreamSignal()
    streamArtifacts.value.push(payload)
    if (useReducer.value) appendFrame({ kind: 'artifact', artifact: payload })
    scheduleRender()
  }

  // Append an interrupt (approval/clarify) frame into the turn log so the fold
  // materializes it as an inline interrupt part. Mirrors appendArtifact: a plain
  // frame append gated on the reducer, then a batched render. The approvals
  // composable owns the payload and the resolution side-map; the stream only
  // records the frame on the live turn's log.
  function appendInterruptFrame(input: {
    interruptKind: 'approval' | 'clarify'
    approvalId: string
    data: InterruptApprovalData | InterruptClarifyData
    at: number
  }) {
    noteStreamSignal()
    if (useReducer.value) appendFrame({ kind: 'interrupt', ...input })
    scheduleRender()
  }

  // Open a render bubble for an interrupt that arrived with no live turn
  // streaming (a queued/background turn, or a reload that recovered a pending
  // approval). This is deliberately lighter than startStreaming(): it does NOT
  // reset the log or the activity refs, so an interrupt frame appended right
  // after it survives. The turn stays open because an unresolved approval pauses
  // the idle timer, so the fold-driven activity surface keeps rendering the part.
  function ensureInterruptBubble() {
    if (streamBubble.value) return
    if (!streamTurnStartedAt.value) streamTurnStartedAt.value = Date.now()
    streamBubble.value = true
    streamShowHeader.value = options.lastHeaderRole.value !== 'assistant'
    isStreaming.value = true
  }

  function reconcileCheckpointedText(
    finalText: string,
    modelCallSegments: ChatModelCallSegment[] | null | undefined,
  ): string | null {
    if (streamCheckpoints.length === 0) return null
    const chunks = splitTextByModelCallSegments(finalText, modelCallSegments)
    if (!chunks) return null
    const normalized = normalizeModelCallSegments(
      modelCallSegments,
      Array.from(finalText).length,
    )
    const checkpointTexts = streamCheckpoints.map(() => '')
    const hasAppliedIdentities = streamCheckpoints.every(checkpoint => checkpoint.modelCallId)
    if (hasAppliedIdentities) {
      let checkpointIndex = 0
      for (let segmentIndex = 0; segmentIndex < normalized.length; segmentIndex++) {
        const segment = normalized[segmentIndex]!
        const groupStart = checkpointIndex
        while (
          checkpointIndex < streamCheckpoints.length
          && streamCheckpoints[checkpointIndex]!.modelCallId === segment.modelCallId
        ) {
          checkpointIndex++
        }
        if (checkpointIndex === groupStart) return null
        checkpointTexts[groupStart] = chunks[segmentIndex] || ''
      }
      if (checkpointIndex !== streamCheckpoints.length) return null
    } else {
      // Compatibility with gateways that predate applied model-call identity.
      if (
        streamCheckpoints.some(checkpoint => checkpoint.modelCallId)
        || chunks.length !== streamCheckpoints.length + 1
      ) return null
      streamCheckpoints.forEach((_checkpoint, index) => {
        checkpointTexts[index] = chunks[index] || ''
      })
    }
    streamCheckpoints.forEach((checkpoint, index) => {
      checkpoint.rawText = checkpointTexts[index] || ''
      setCheckpointText(checkpoint, checkpoint.rawText)
    })
    return chunks[chunks.length - 1]!
  }

  function reconcileFinalText(
    finalText: string | null | undefined,
    modelCallSegments?: ChatModelCallSegment[] | null,
  ) {
    // null/undefined means the terminal event carried no authoritative text
    // snapshot, so streamed deltas remain canonical. Empty string is distinct:
    // it intentionally clears stale text while preserving tool history.
    if (finalText == null) return

    const boundaryTail = reconcileCheckpointedText(finalText, modelCallSegments)
    const segmentFinalText = boundaryTail != null
      ? boundaryTail
      : checkpointedRaw && finalText.startsWith(checkpointedRaw)
        ? finalText.slice(checkpointedRaw.length)
        : finalText
    if (useReducer.value === true) {
      const changed = currentStreamRaw() !== segmentFinalText
      appendFrame({ kind: 'final-text', text: segmentFinalText })
      if (changed) scheduleRender()
      return
    }
    const reconciled = reconcileTextSnapshot(
      streamSegments.value,
      streamRaw.value,
      segmentFinalText,
    )
    if (reconciled.changed) {
      streamRaw.value = reconciled.rawText
      streamSegments.value = reconciled.segments
      scheduleRender()
    }
    if (useReducer.value) appendFrame({ kind: 'final-text', text: segmentFinalText })
  }

  function isToolGroupOpen(groupId: string): boolean {
    return openToolGroups.value.has(groupId)
  }

  function toggleToolGroup(groupId: string) {
    const next = new Set(openToolGroups.value)
    next.has(groupId) ? next.delete(groupId) : next.add(groupId)
    openToolGroups.value = next
  }

  function isToolItemOpen(itemId: string): boolean {
    return openToolItems.value.has(itemId)
  }

  function toggleToolItem(itemId: string) {
    const next = new Set(openToolItems.value)
    next.has(itemId) ? next.delete(itemId) : next.add(itemId)
    openToolItems.value = next
  }

  function clearRenderTimer() {
    renderDirty = false
    if (renderRaf !== null) {
      if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(renderRaf)
      renderRaf = null
    }
    if (renderFallbackTimer !== null) {
      clearTimeout(renderFallbackTimer)
      renderFallbackTimer = null
    }
  }

  function cleanup() {
    clearRenderTimer()
    clearStreamIdleTimer()
    hideThinkingIndicator()
    clearStreamActivity()
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }

  return {
    isStreaming,
    streamArtifacts,
    streamBubble,
    streamHasVisibleOutput,
    streamTimelineItems,
    streamActivityVisible,
    streamActivityStale,
    streamPhaseLabel,
    streamPhaseElapsed,
    streamTurnElapsed,
    streamStepLabel,
    streamToolElapsedText,
    streamIdleTimeoutMs,
    thinkingVisible,
    thinkingText,
    startStreaming,
    reconcileStreamTaskClock,
    endStreaming,
    checkpointForUserMessage,
    acknowledgeSteerBoundary,
    resetStreamForRouterReplay,
    resetLiveTurnState,
    resetAnswerGeneration,
    setAssistantMessageId,
    appendDelta,
    scheduleRender,
    appendToolCall,
    appendToolDelta,
    appendToolEnd,
    appendToolResult,
    appendArtifact,
    appendInterruptFrame,
    ensureInterruptBubble,
    reconcileFinalText,
    resetStreamIdleTimer,
    setStreamConnectionAvailable,
    clearStreamIdleTimer,
    setStreamActivity,
    restoreStatusHistory,
    recordCompactionActivity,
    showThinkingIndicator,
    hideThinkingIndicator,
    isToolGroupOpen,
    toggleToolGroup,
    isToolItemOpen,
    toggleToolItem,
    cleanup,
    // live-turn shadow log surface: appendFrame/useReducer let the event handlers
    // (which own the thinking ref) append their frame; assertLiveParity is run
    // from a DEV watchEffect; foldedTurn is the fold output (not rendered yet).
    appendFrame,
    noteReasoningPresentationDelta,
    completeReasoningPresentation,
    useReducer,
    foldedTurn,
    getThinkingText: () => foldedTurn.value.thinkingText,
    assertLiveParity,
  }
}
