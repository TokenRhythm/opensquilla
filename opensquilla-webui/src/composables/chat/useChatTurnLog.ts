import { customRef, watch, type Ref } from 'vue'
import type {
  ChatToolCall,
  ChatToolCallGroup,
} from '@/types/chat'
import type { InterruptViewState } from '@/types/parts'
import type { Frame, FrameInput } from '@/types/turnlog'
import { TurnAccumulator, type FoldedTurn } from '@/utils/chat/foldTurn'

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
    false,
    false,
  )

  function refreshSnapshot(): void {
    currentSnapshot = accumulator.snapshot(
      liveRenderMarkdown,
      options.toolCallGroups,
      undefined,
      options.interruptState?.value,
      false,
      false,
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
    // The compact accepted log is retained only to rebuild the accumulator
    // after an answer-generation reset; it is never a second render source.
    coalesceAcceptedFrame(accepted)
    snapshotDirty = true
    publishPending = true
  }

  function publish() {
    if (!publishPending) return
    if (snapshotDirty) refreshSnapshot()
    publishPending = false
    triggerSnapshot()
  }

  function resetLog() {
    accumulator.reset()
    acceptedFrames = []
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
    accumulator.checkpointText()
    acceptedFrames = []
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

  function peekToolCall(toolId: string): ChatToolCall | null {
    return accumulator.currentToolCall(toolId)
  }

  function peekRunningToolCall(): ChatToolCall | null {
    return accumulator.currentRunningToolCall()
  }

  function hasToolBoundary(): boolean {
    return accumulator.hasToolBoundary()
  }

  function peekToolTiming(toolId: string): { startedAt: number; endedAt?: number } | null {
    return accumulator.currentToolTiming(toolId)
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

  return {
    appendFrame,
    setAcceptedActivityOrder,
    publish,
    resetLog,
    checkpointText,
    peekRawText,
    peekToolCall,
    peekRunningToolCall,
    hasToolBoundary,
    peekToolTiming,
    finalizeToolInputs,
    resetGeneration,
    foldedTurn,
  }
}
