import { customRef, ref, shallowRef, watch, type Ref } from 'vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallGroup,
} from '@/types/chat'
import type { InterruptViewState } from '@/types/parts'
import type { ArtifactPayload } from '@/types/rpc'
import type { Frame, FrameInput } from '@/types/turnlog'
import { TurnAccumulator, type FoldedTurn } from '@/utils/chat/foldTurn'
import { diffFoldVsLegacy } from '@/composables/chat/turnParity'

// Three-mode flag: ON (true, prod default) appends frames and renders the live
// work-card from the fold; SHADOW ('shadow', DEV default) appends + asserts
// fold-vs-legacy parity while legacy stays the rendered source; OFF (false, the
// `foldLiveTurn=0` kill switch) stops appends and renders legacy — the one-flag
// rollback lever.
export type FoldLiveTurnMode = false | 'shadow' | true

const USE_REDUCER_KEY = 'opensquilla.chat.foldLiveTurn'

// Default ON in production: the fold is authoritative for the live work-card.
// Setting the key to '0' forces the legacy render (kept as a one-flag rollback
// lever for one release); any other value, or no key, is ON.
function readFlag(): FoldLiveTurnMode {
  try {
    return localStorage.getItem(USE_REDUCER_KEY) === '0' ? false : true
  } catch {
    return true
  }
}

/** Legacy live render surface the shadow parity check compares the fold against. */
export interface TurnLogLegacySurface {
  timelineItems: Ref<ChatStreamTimelineItem[]>
  rawText: Ref<string>
  toolCalls: Ref<ChatToolCall[]>
  artifacts: Ref<ArtifactPayload[]>
  thinkingText: Ref<string>
}

export interface UseChatTurnLogOptions {
  renderMarkdown: (
    text: string,
    opts?: {
      highlight?: boolean
      cache?: 'settled' | 'none'
      math?: 'full' | 'defer'
    },
  ) => string
  toolCallGroups: (calls: ChatToolCall[] | undefined, baseKey: string) => ChatToolCallGroup[]
  /** Resolution view-state keyed by approval id; the fold reads it to stamp each
   *  interrupt part. Defaults to an empty map until a producer threads one in. */
  interruptState?: Ref<ReadonlyMap<string, InterruptViewState>>
}

export function useChatTurnLog(options: UseChatTurnLogOptions) {
  const events = shallowRef<Frame[]>([])
  const useReducer = ref<FoldLiveTurnMode>(import.meta.env.DEV ? 'shadow' : readFlag())
  const accumulator = new TurnAccumulator()
  let acceptedFrames: Frame[] = []
  let appendIndex = 0
  let acceptedActivityOrder: number | undefined
  let snapshotDirty = false
  let publishPending = false
  let triggerSnapshot: () => void = () => {}

  const liveRenderMarkdown = (text: string) => options.renderMarkdown(text, {
    highlight: false,
    cache: 'none',
    math: 'defer',
  })

  let currentSnapshot = accumulator.snapshot(
    liveRenderMarkdown,
    options.toolCallGroups,
    undefined,
    options.interruptState?.value,
    useReducer.value !== true,
    useReducer.value !== true,
  )

  function refreshSnapshot(): void {
    currentSnapshot = accumulator.snapshot(
      liveRenderMarkdown,
      options.toolCallGroups,
      undefined,
      options.interruptState?.value,
      useReducer.value !== true,
      useReducer.value !== true,
    )
    snapshotDirty = false
  }

  // A lazy getter keeps direct unit-test/finalizer reads authoritative without
  // making frame acceptance reactive. UI consumers are invalidated only by
  // publish(), which is called from the shared frame scheduler.
  const foldedTurn = customRef<FoldedTurn>((track, trigger) => {
    triggerSnapshot = trigger
    return {
      get() {
        track()
        if (snapshotDirty) refreshSnapshot()
        return currentSnapshot
      },
      set() {},
    }
  })

  function coalesceAcceptedFrame(frame: Frame): void {
    const previous = acceptedFrames[acceptedFrames.length - 1]
    if (previous?.kind === 'text' && frame.kind === 'text'
      && previous.presentation === frame.presentation) {
      previous.text += frame.text
      return
    }
    if (previous?.kind === 'thinking' && frame.kind === 'thinking') {
      previous.text += frame.text
      return
    }
    if (previous?.kind === 'tool-delta' && frame.kind === 'tool-delta'
      && previous.toolId === frame.toolId) {
      previous.fragment += frame.fragment
      return
    }
    acceptedFrames.push(frame)
  }

  function appendFrame(frame: FrameInput) {
    const accepted = {
      ...frame,
      seq: appendIndex++,
      ...(frame.activityOrder !== undefined
        ? { activityOrder: frame.activityOrder }
        : acceptedActivityOrder !== undefined
          ? { activityOrder: acceptedActivityOrder }
          : {}),
    } as Frame
    accumulator.append(accepted)
    // The published frame stream is diagnostic-only in production, but the
    // compact accepted log is needed to rebuild the accumulator after an answer
    // generation reset.
    coalesceAcceptedFrame(accepted)
    snapshotDirty = true
    publishPending = true
  }

  function publish() {
    if (!publishPending) return
    if (snapshotDirty) refreshSnapshot()
    // A shallow immutable publication prevents accepted deltas from mutating a
    // reactive array between display frames. The production reducer has no
    // frame-array consumer: publishing it there retained the previous growing
    // text/tool string until the next flush in addition to the accumulator's
    // canonical state. Keep the diagnostic stream only for SHADOW/rollback.
    events.value = useReducer.value === true
      ? []
      : acceptedFrames.map(frame => ({ ...frame }))
    publishPending = false
    triggerSnapshot()
  }

  function resetLog() {
    accumulator.reset()
    acceptedFrames = []
    events.value = []
    appendIndex = 0
    acceptedActivityOrder = undefined
    snapshotDirty = true
    publishPending = false
    refreshSnapshot()
    triggerSnapshot()
  }

  function setAcceptedActivityOrder(value: number | undefined): void {
    acceptedActivityOrder = Number.isSafeInteger(value) && Number(value) > 0
      ? Number(value)
      : undefined
  }

  function checkpointText() {
    if (useReducer.value === true) {
      accumulator.checkpointText()
      acceptedFrames = []
      snapshotDirty = true
      publishPending = true
      publish()
      return
    }
    acceptedFrames = acceptedFrames.filter(
      frame => frame.kind !== 'text' && frame.kind !== 'final-text',
    )
    accumulator.reset()
    for (const frame of acceptedFrames) accumulator.append(frame)
    snapshotDirty = true
    publishPending = true
    publish()
  }

  /**
   * Replace only the current answer generation. Completed tool frames and
   * artifacts belong to the same live bubble; old text, reasoning, terminal
   * snapshots, and pending tool frames belong to the generation being replaced.
   */
  function resetGeneration(optionsArg: {
    textSnapshot?: string
    preserveCompletedTools?: boolean
  } = {}) {
    const preserveCompletedTools = optionsArg.preserveCompletedTools !== false
    const completedToolIds = new Set(
      acceptedFrames
        .filter((frame): frame is Extract<Frame, { kind: 'tool-result' }> => frame.kind === 'tool-result')
        .map(frame => frame.toolId),
    )

    acceptedFrames = acceptedFrames.filter((frame) => {
      if (frame.kind === 'text' || frame.kind === 'thinking' || frame.kind === 'final-text') {
        return false
      }
      if (
        frame.kind === 'tool-start'
        || frame.kind === 'tool-delta'
        || frame.kind === 'tool-result'
      ) {
        return preserveCompletedTools && completedToolIds.has(frame.toolId)
      }
      return true
    })

    accumulator.reset()
    for (const frame of acceptedFrames) accumulator.append(frame)
    snapshotDirty = true
    publishPending = true

    if (typeof optionsArg.textSnapshot === 'string' && optionsArg.textSnapshot) {
      appendFrame({
        kind: 'text',
        text: optionsArg.textSnapshot,
        presentation: 'answer',
      })
    }

    publish()
  }

  function peekRawText(): string {
    return accumulator.currentRawText()
  }

  function finalizeToolInputs(): void {
    if (!accumulator.finalizeToolInputs()) return
    snapshotDirty = true
    publishPending = true
  }

  if (options.interruptState) {
    watch(options.interruptState, () => {
      snapshotDirty = true
      publishPending = true
      // Interrupt decisions are rare user actions and must update immediately;
      // unlike provider deltas they do not form a high-frequency stream.
      publish()
    })
  }

  // DEV/SHADOW parity: compare the fold against the legacy live surface and log
  // the parity marker on divergence so the console-clarity e2e turns any drift into
  // a hard failure. Wrapped so it never throws into the render pipeline.
  function checkParity(legacy: TurnLogLegacySurface): string[] {
    try {
      // Unwrap the live refs into plain values and delegate to the pure diff so
      // the comparison (including the full tool-call `result`, not just its
      // 200-char preview) is exercised the same way the unit tests exercise it.
      return diffFoldVsLegacy(
        foldedTurn.value,
        {
          timelineItems: legacy.timelineItems.value,
          rawText: legacy.rawText.value,
          toolCalls: legacy.toolCalls.value,
          artifacts: legacy.artifacts.value,
          thinkingText: legacy.thinkingText.value,
        },
        options.interruptState?.value,
      )
    } catch (err) {
      return [`parity threw: ${String(err)}`]
    }
  }

  function assertParity(legacy: TurnLogLegacySurface): void {
    if (!import.meta.env.DEV || useReducer.value === false) return
    const problems = checkParity(legacy)
    if (problems.length) {
      console.error('[live-turn parity]', { live: true, problems })
    }
  }

  return {
    events,
    useReducer,
    appendFrame,
    setAcceptedActivityOrder,
    publish,
    resetLog,
    checkpointText,
    peekRawText,
    finalizeToolInputs,
    resetGeneration,
    foldedTurn,
    assertParity,
  }
}
