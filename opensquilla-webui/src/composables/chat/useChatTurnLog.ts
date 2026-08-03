import { computed, ref, type ComputedRef, type Ref } from 'vue'
import type {
  ChatStreamTimelineItem,
  ChatToolCall,
  ChatToolCallGroup,
} from '@/types/chat'
import type { InterruptViewState } from '@/types/parts'
import type { ArtifactPayload } from '@/types/rpc'
import type { Frame, FrameInput } from '@/types/turnlog'
import { foldTurn, type FoldedTurn } from '@/utils/chat/foldTurn'
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
  renderMarkdown: (text: string) => string
  toolCallGroups: (calls: ChatToolCall[] | undefined, baseKey: string) => ChatToolCallGroup[]
  /** Resolution view-state keyed by approval id; the fold reads it to stamp each
   *  interrupt part. Defaults to an empty map until a producer threads one in. */
  interruptState?: Ref<ReadonlyMap<string, InterruptViewState>>
}

export function useChatTurnLog(options: UseChatTurnLogOptions) {
  const events = ref<Frame[]>([])
  const useReducer = ref<FoldLiveTurnMode>(import.meta.env.DEV ? 'shadow' : readFlag())
  let appendIndex = 0

  function appendFrame(frame: FrameInput) {
    events.value.push({ ...frame, seq: appendIndex++ } as Frame)
  }

  function resetLog() {
    events.value = []
    appendIndex = 0
  }

  function checkpointText() {
    events.value = events.value.filter(
      frame => frame.kind !== 'text' && frame.kind !== 'final-text',
    )
  }

  const foldedTurn: ComputedRef<FoldedTurn> = computed(() =>
    foldTurn(
      events.value,
      options.renderMarkdown,
      options.toolCallGroups,
      undefined,
      options.interruptState?.value,
    ),
  )

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
    resetLog,
    checkpointText,
    foldedTurn,
    assertParity,
  }
}
