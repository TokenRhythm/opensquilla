import { computed, ref, type Ref } from 'vue'
import i18n from '@/i18n'
import type {
  ChatMessage,
  ChatRunStatusSource,
  ChatStreamSegment,
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatTimelineSegment,
  RawToolCallPayload,
} from '@/types/chat'
import type {
  ArtifactPayload,
  ToolDeltaPayload,
  ToolResultPayload,
  ToolUsePayload,
} from '@/types/rpc'
import type {
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptViewState,
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
import { useChatTurnLog } from '@/composables/chat/useChatTurnLog'

export const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 630_000
const THINKING_DELAY_MS = 400
const THINKING_TTL_MS = 60000
// Bounds for trusting a server-stamped tool start time against the local clock
// (see serverToolStartedAt). Tolerate small server-ahead skew; reject starts older
// than this (longer than any realistic provider tool run) as skew/garbage.
const SERVER_CLOCK_TOLERANCE_MS = 5000
const MAX_TRUSTED_TOOL_AGE_MS = 60 * 60 * 1000
const SQUILLA_VERBS = ['Planning next step', 'Reading context', 'Waiting for model', 'Preparing output']

// Internal phase labels stay English (they double as stable keys for dedup,
// matching, and the appended status-frame action). Localize only at the display
// boundary via this map; unmapped labels (e.g. tool-specific micro-verbs) fall
// back to their English text.
const STREAM_LABEL_KEYS: Record<string, string> = {
  Sending: 'chat.stream.sending',
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
  applySessionRunState: (source: ChatRunStatusSource | null | undefined) => void
  renderMarkdown: (text: string, opts?: { highlight?: boolean }) => string
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
  const streamBubble = ref(false)
  const streamShowHeader = ref(false)

  const streamHasVisibleOutput = computed(() => {
    return streamSegments.value.length > 0 ||
      streamToolCalls.value.length > 0 ||
      streamArtifacts.value.length > 0
  })

  const streamActivity = ref({ label: 'Sending', key: 'Sending', startedAt: 0 })
  const streamActivityTick = ref(0)
  let streamActivityTimer: ReturnType<typeof setInterval> | null = null
  const streamRound = ref(1)
  const lastSignalAt = ref(0)
  const toolTimes = ref(new Map<string, { startedAt: number; endedAt?: number }>())

  // The ribbon stays up for the whole run, including while tool rows render.
  const streamActivityVisible = computed(() => {
    return isStreaming.value && streamBubble.value
  })

  const streamActivityStale = computed(() => {
    streamActivityTick.value
    return lastSignalAt.value > 0 && Date.now() - lastSignalAt.value > STALE_SIGNAL_MS
  })

  // Phase narration on its own, used by the activity head where elapsed and
  // the step chip render as separate elements rather than one packed string.
  const streamPhaseLabel = computed(() => {
    streamActivityTick.value
    const now = Date.now()
    if (lastSignalAt.value > 0 && now - lastSignalAt.value > STALE_SIGNAL_MS) {
      // Static on purpose: this feeds a polite live region, so a ticking
      // seconds value here would be re-announced every second for the whole
      // stall. The aria-hidden elapsed chip carries the seconds instead.
      return i18n.global.t('chat.activity.stale')
    }
    const startedAt = streamActivity.value.startedAt || now
    const seconds = Math.max(0, Math.floor((now - startedAt) / 1000))
    return seconds >= 10 && streamActivity.value.label === 'Planning next step'
      ? i18n.global.t('chat.stream.stillWaiting')
      : localizeStreamLabel(streamActivity.value.label)
  })

  // Elapsed seconds for the current phase, rendered as its own chip.
  const streamPhaseElapsed = computed(() => {
    streamActivityTick.value
    const now = Date.now()
    if (lastSignalAt.value > 0 && now - lastSignalAt.value > STALE_SIGNAL_MS) {
      // During a stall the phase label is static for screen readers, so the
      // silence duration ticks here, out of the announced sentence.
      return `${Math.floor((now - lastSignalAt.value) / 1000)}s`
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
  const { appendFrame, resetLog, useReducer, foldedTurn } = turnLog

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

  const streamIdleTimer = ref<ReturnType<typeof setTimeout> | null>(null)
  const streamIdleTimeoutMs = ref(DEFAULT_STREAM_IDLE_TIMEOUT_MS)
  const streamIdlePausedForApproval = ref(false)
  // Stream render coalescing. Tokens arrive far faster than the display can
  // paint, so re-renders are batched onto the frame clock (requestAnimationFrame)
  // rather than a fixed setTimeout: frame-aligned flushes avoid the mid-frame
  // stutter and the visibly-stepped ~12.5fps the old 80ms timer produced.
  // MIN_FLUSH_INTERVAL caps the heavy markdown re-parse so a fast stream cannot
  // re-render every single frame; the hidden-tab fallback keeps output landing
  // when rAF is paused in a background tab.
  const MIN_FLUSH_INTERVAL_MS = 33
  const HIDDEN_FLUSH_FALLBACK_MS = 250
  let renderRaf: number | null = null
  let renderFallbackTimer: ReturnType<typeof setTimeout> | null = null
  let lastFlushAt = 0
  let renderDirty = false

  function resetStreamState() {
    streamRaw.value = ''
    streamSegments.value = []
    streamToolCalls.value = []
    streamArtifacts.value = []
    toolTimes.value = new Map()
    // Clear the live-turn log alongside the legacy refs so the next turn's fold
    // starts empty. Every reset path (start/end/router-replay/live-turn) funnels
    // through here, so this single call covers them all. Steer never calls
    // a reset, so an in-flight steered turn's log is preserved.
    resetLog()
  }

  function noteStreamSignal() {
    lastSignalAt.value = Date.now()
  }

  // `key` identifies the activity phase: the elapsed counter restarts only
  // when the phase changes, so label refinements (e.g. tool arguments
  // streaming in) keep the same running clock.
  function setStreamActivity(label: string, key = label) {
    noteStreamSignal()
    const current = streamActivity.value
    let isNewPhase = false
    if (current.key === key) {
      if (current.label !== label) {
        streamActivity.value = { label, key, startedAt: current.startedAt || Date.now() }
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
    }
    streamActivityTick.value++
    if (!streamActivityTimer) {
      streamActivityTimer = setInterval(() => {
        streamActivityTick.value++
      }, 1000)
    }
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
    isStreaming.value = true
    options.applySessionRunState({ run_status: 'running', active_task: { status: 'running' } })
    resetStreamState()
    openToolGroups.value = new Set()
    openToolItems.value = new Set()
    streamToolGroupSeq = 0
    streamRound.value = 1
    noteStreamSignal()
    streamBubble.value = true
    streamShowHeader.value = options.lastHeaderRole.value !== 'assistant'
    setStreamActivity('Sending')
    options.autoScroll.value = true
    resetStreamIdleTimer()
  }

  function endStreaming(opts?: { reason?: string }) {
    const wasAborted = opts?.reason === 'aborted'
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
        options.stripGeneratedArtifactMarkers(streamRaw.value),
      ).trim()

      const sentinelOnly = !wasAborted && ['NO_REPLY', 'HEARTBEAT_OK'].includes(cleanedText)
      // After Stop, partial streamed output (text, tool rows, artifacts) is
      // kept; only a bubble with nothing visible at all is dropped.
      const foldedInterrupts = foldedTurn.value.parts.filter(
        (part): part is Extract<import('@/types/parts').ChatPart, { type: 'interrupt' }> =>
          part.type === 'interrupt',
      )
      const emptyStream = !cleanedText
        && streamArtifacts.value.length === 0
        && streamToolCalls.value.length === 0
        && foldedInterrupts.length === 0
      if (sentinelOnly || emptyStream) {
        streamBubble.value = false
        isStreaming.value = false
        resetStreamState()
        return
      }

      options.messages.value.push({
        role: 'assistant',
        text: cleanedText,
        ts: new Date().toISOString(),
        artifacts: streamArtifacts.value.slice(),
        tool_calls: streamToolCalls.value.map(streamToolCallToHistoryCall),
        timeline: useReducer.value
          ? foldedTurn.value.timelineSegments.slice()
          : streamTimelineSnapshot(cleanedText),
        interrupts: foldedInterrupts.map(part => ({ ...part })),
        // Detach the fold's activity history from the about-to-be-reset log. In
        // OFF mode this is [], so the field is harmless. The empty/sentinel drop
        // path above returns before this push, so a status-only ghost turn never
        // persists an orphan history.
        statusHistory: foldedTurn.value.statusHistory.slice(),
        interrupted: wasAborted || undefined,
      })
    }

    streamBubble.value = false
    isStreaming.value = false
    resetStreamState()
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
  }

  function normalizeIncomingTextDelta(text: string): string {
    const raw = typeof text === 'string' ? text : ''
    if (!raw || !streamRaw.value) return raw

    const sawToolBoundary =
      streamToolCalls.value.length > 0 ||
      streamSegments.value.some(seg => seg.type === 'tool-group')
    if (!sawToolBoundary) return raw

    if (raw === streamRaw.value) return ''
    if (raw.startsWith(streamRaw.value)) return raw.slice(streamRaw.value.length)
    return raw
  }

  function appendDelta(text: string) {
    if (options.aborted.value) return
    const deltaText = normalizeIncomingTextDelta(text)
    if (!deltaText) return
    if (!isStreaming.value) startStreaming()
    setStreamActivity('Writing reply', `write:${streamRound.value}`)
    streamRaw.value += deltaText

    const lastSegment = streamSegments.value[streamSegments.value.length - 1]
    if (!lastSegment || lastSegment.type !== 'text') {
      streamSegments.value.push({ type: 'text', raw: deltaText, html: '', dirty: true })
    } else {
      lastSegment.raw = (lastSegment.raw || '') + deltaText
      lastSegment.dirty = true
    }

    if (useReducer.value) appendFrame({ kind: 'text', text: deltaText })
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

  function onRenderFrame() {
    renderRaf = null
    // Coalesce bursts to the frame clock but cap the heavy re-parse: if the last
    // flush was very recent, wait for the next frame instead of re-rendering the
    // whole growing segment again this frame.
    if (Date.now() - lastFlushAt < MIN_FLUSH_INTERVAL_MS) {
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

    for (const seg of streamSegments.value) {
      if (seg.type === 'text' && seg.dirty) {
        // Live reveal renders without syntax highlighting (the heaviest per-flush
        // cost); the committed message re-renders with full highlight on end.
        // A half-streamed ``` fence is closed for the render only (raw untouched)
        // so a code block renders stably as a <pre> while it grows, instead of
        // flickering paragraph↔block on every flush — the worst mid-stream jump.
        seg.html = options.renderMarkdown(stabilizeStreamingMarkdown(seg.raw || ''), { highlight: false })
        seg.dirty = false
      }
    }

    renderDirty = false
    if (options.autoScroll.value) options.scrollToBottom()
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

  function resetStreamIdleTimer() {
    // Every gateway event funnels through here, including run heartbeats, so
    // it doubles as the liveness signal for the staleness note.
    noteStreamSignal()
    clearStreamIdleTimer()
    streamIdleTimeoutMs.value = streamIdleTimeoutFromPolicy(options.rpcPolicy?.())
    if (!isStreaming.value || streamIdlePausedForApproval.value) return
    streamIdleTimer.value = setTimeout(() => {
      if (isStreaming.value && !streamIdlePausedForApproval.value) {
        endStreaming()
        const seconds = Math.round(streamIdleTimeoutMs.value / 1000)
        options.messages.value.push({ role: 'error', text: `Response timed out -- no events received for ${seconds}s`, ts: new Date().toISOString() })
      }
    }, streamIdleTimeoutMs.value)
  }

  function clearStreamIdleTimer() {
    if (streamIdleTimer.value) { clearTimeout(streamIdleTimer.value); streamIdleTimer.value = null }
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

    const nextInput = `${tc.inputRaw || ''}${fragmentText}`
    tc.inputRaw = nextInput
    if (!isEmptyToolPreview(nextInput)) {
      tc.inputPreview = truncateToolPreview(nextInput, 200)
      tc.displayName = toolDisplayName(tc.name, nextInput)
    }
    if (tc.isRunning) narrateToolCall(tc)
    // The fold concats the same fragment onto the same call's inputRaw. When
    // this delta created the call, ensureStreamToolCall already emitted the
    // seeding tool-start above, so the call exists in the fold before this.
    if (useReducer.value) appendFrame({ kind: 'tool-delta', toolId, fragment: fragmentText })
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
          return raw ? [{ type: 'text', raw }] : []
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
    if (segments.length === 0 && fallbackText) return [{ type: 'text', raw: fallbackText }]
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
    streamBubble.value = true
    streamShowHeader.value = options.lastHeaderRole.value !== 'assistant'
    isStreaming.value = true
  }

  function reconcileFinalText(finalText: string | null | undefined) {
    // null/undefined means the terminal event carried no authoritative text
    // snapshot, so streamed deltas remain canonical. Empty string is distinct:
    // it intentionally clears stale text while preserving tool history.
    if (finalText == null) return

    const reconciled = reconcileTextSnapshot(
      streamSegments.value,
      streamRaw.value,
      finalText,
    )
    if (reconciled.changed) {
      streamRaw.value = reconciled.rawText
      streamSegments.value = reconciled.segments
      scheduleRender()
    }
    if (useReducer.value) appendFrame({ kind: 'final-text', text: finalText })
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
    streamStepLabel,
    streamToolElapsedText,
    streamIdleTimeoutMs,
    thinkingVisible,
    thinkingText,
    startStreaming,
    endStreaming,
    resetStreamForRouterReplay,
    resetLiveTurnState,
    appendDelta,
    scheduleRender,
    appendToolCall,
    appendToolDelta,
    appendToolResult,
    appendArtifact,
    appendInterruptFrame,
    ensureInterruptBubble,
    reconcileFinalText,
    resetStreamIdleTimer,
    clearStreamIdleTimer,
    setStreamActivity,
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
    useReducer,
    foldedTurn,
    assertLiveParity,
  }
}
