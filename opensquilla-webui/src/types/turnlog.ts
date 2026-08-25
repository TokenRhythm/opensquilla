import type { ArtifactPayload } from '@/types/rpc'
import type {
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptResolution,
} from '@/types/parts'

/**
 * A normalized, already-gated record of one accepted live-stream event.
 *
 * Frames are NOT raw RPC payloads: epoch/seq/abort gating runs in the event
 * handlers BEFORE a frame is ever appended (see useChatRpcEventHandlers), so the
 * log only ever holds frames legacy would also have applied. `seq` is assigned
 * by `appendFrame` as a monotonic append index (seq-less frames are ordered
 * by arrival), used only for ordering/dedup — never for gating.
 */
type FrameBody =
  | {
      kind: 'text'
      seq: number
      text: string
      presentation?: 'intermediate' | 'answer'
    }
  | { kind: 'tool-start'; seq: number; toolId: string; name: string; input: string; at: number }
  | { kind: 'tool-delta'; seq: number; toolId: string; fragment: string }
  | { kind: 'tool-result'; seq: number; toolId: string; name: string; result: string; isError: boolean; input: string; at: number }
  | { kind: 'artifact'; seq: number; artifact: ArtifactPayload }
  | {
      kind: 'thinking-start'
      seq: number
      blockId: string
      blockIndex: number
      at: number
      contentKind?: 'summary' | 'reasoning'
    }
  | {
      kind: 'thinking'
      seq: number
      text: string
      at: number
      blockId?: string
      blockIndex?: number
    }
  | {
      kind: 'thinking-end'
      seq: number
      blockId: string
      blockIndex: number
      status: ReasoningBlockStatus
      at: number
    }
  | { kind: 'final-text'; seq: number; text: string }
  | {
      kind: 'interrupt'
      seq: number
      interruptKind: 'approval' | 'clarify'
      approvalId: string                 // approval id, or clarify runId|step composite
      data: InterruptApprovalData | InterruptClarifyData
      at: number
    }
  | {
      // Optional/defensive: the authoritative resolution path is the
      // composable-owned interruptState map, so the live fold reads resolution
      // from there and can ignore this frame. It exists as the forward wire
      // format for history persistence.
      kind: 'interrupt-resolved'
      seq: number
      approvalId: string
      resolution: InterruptResolution
      at: number
    }
  | {
      // One accepted activity phase transition. Recorded when the live ribbon
      // moves to a new phase so the finished turn can show what the agent was
      // doing; the fold accumulates these into a per-turn activity history.
      kind: 'status'
      seq: number
      action: string   // stable phase key (streamActivity.key)
      label: string    // human label (streamActivity.label)
      at: number       // phase startedAt
      id?: string
      category?: 'phase' | 'maintenance'
      state?: 'running' | 'completed' | 'skipped' | 'stale' | 'cancelled' | 'failed'
      source?: string
      durability?: string
      detail?: string
      reason?: string
    }

export type Frame = FrameBody & {
  /** Authoritative Gateway stream position for cross-refresh chronology. */
  activityOrder?: number
}

/** A frame as emitted by a mutator; `appendFrame` stamps the `seq` index. */
export type FrameInput =
  | Omit<Extract<Frame, { kind: 'text' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'tool-start' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'tool-delta' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'tool-result' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'artifact' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'thinking-start' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'thinking' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'thinking-end' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'final-text' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'interrupt' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'interrupt-resolved' }>, 'seq'>
  | Omit<Extract<Frame, { kind: 'status' }>, 'seq'>

export type ReasoningBlockStatus = 'streaming' | 'completed' | 'interrupted' | 'error'

export interface ReasoningBlock {
  id: string
  index: number
  text: string
  status: ReasoningBlockStatus
  startedAt: number
  endedAt?: number
  contentKind: 'summary' | 'reasoning'
  /** First accepted event position for interleaving with phases and tools. */
  activityOrder?: number
}
