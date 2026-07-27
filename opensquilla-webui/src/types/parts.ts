import type { ArtifactPayload } from '@/types/rpc'

/**
 * The four-state tool machine. Maps from today's ChatToolCall.{status,isRunning,result}:
 *   isRunning === true                          -> 'input-available'   (dispatched, awaiting result)
 *   !isRunning && result === '' && status===''  -> 'input-streaming'   (args still arriving; pre-result)
 *   !isRunning && status === 'success'          -> 'output-available'
 *   !isRunning && (status === 'error'||isError) -> 'output-error'
 * Note: persisted/history calls never set isRunning, so history folds to
 * output-available / output-error only. 'input-streaming' is reachable today only
 * via the live stream path (not yet populated via the live stream path, but the state exists
 * so later steps need no type change).
 */
export type ToolPartState =
  | 'input-streaming'
  | 'input-available'
  | 'output-available'
  | 'output-error'

/**
 * Inline interrupt payloads. An interrupt is an approval or clarify request that
 * blocks the run mid-turn and is rendered inline through the part registry. The
 * two sub-kinds keep a single renderer and registry entry while carrying their
 * distinct payloads.
 */
export interface InterruptApprovalData {
  approvalId: string
  namespace: string                       // 'exec' | plugin ns
  toolName: string
  command: string                         // '' when arg-only
  approvalKind: string                    // e.g. 'sandbox_network' / 'sandbox_path'
  args: Record<string, unknown> | null    // may be null until hydrated
  warning: string                         // '' until hydrated
  agent: string
  sessionKey: string
  deadline: number                        // epoch seconds the request expires; 0 when unknown
}

export interface InterruptClarifyField {
  name: string
  prompt: string
  type: string
  required: boolean
  defaultValue: string
  choices: string[]
}

export interface InterruptClarifyData {
  intro: string
  fields: InterruptClarifyField[]
  runId: string
  step: string
}

export type InterruptResolution =
  | 'approved'
  | 'denied'    // approval outcomes (explicit human deny)
  | 'expired'   // approval lapsed without a response
  | 'unavailable' // approval no longer exists on the authoritative Gateway
  | 'replied'   // clarify submitted

/**
 * Resolution view-state for one interrupt, owned by a composable-side map keyed
 * by approval id (not stored on the append-only frame). The fold reads it to
 * stamp each interrupt part, mirroring how `toolTimes` is a side-map.
 */
export interface InterruptViewState {
  resolution: InterruptResolution | null
  busy: boolean
  error: string
}

export type Part =
  | { type: 'reasoning'; text: string; seconds: number }
  | { type: 'text'; html: string; rawText: string }
  | {
      type: 'tool'
      // identity / grouping
      callId: string            // ChatToolCall.toolId
      groupId: string           // owning group id (see key families in useChatRenderedMessages)
      operationKey: string      // toolOperationKey(name) e.g. 'web.search'
      // display (already-normalized, ready to render)
      toolName: string          // ChatToolCall.name
      displayName: string       // ChatToolCall.displayName
      // state machine
      state: ToolPartState
      isRunning: boolean        // mirror; lets the renderer keep current bullet/ring logic verbatim
      status: '' | 'success' | 'error'
      isError: boolean
      // payloads (raw + already-truncated preview, both kept — tool row sections need both)
      input: string             // ChatToolCall.inputRaw ?? ''
      inputPreview: string      // ChatToolCall.inputPreview
      output: string            // ChatToolCall.result
      outputPreview: string     // ChatToolCall.resultPreview
      error?: string            // == output when isError, else undefined
    }
  | { type: 'artifact'; artifact: ArtifactPayload }
  | {
      type: 'source'
      sourceId: number
      url: string
      title: string
      domain: string
      canonicalUrl?: string
      provider?: string
      fetched?: boolean
      fetchStatus?: string
    }
  | {
      type: 'interrupt'
      interruptKind: 'approval' | 'clarify'
      // exactly one of the two is set per interruptKind:
      approval?: InterruptApprovalData
      clarify?: InterruptClarifyData
      // resolution view-state, stamped by the fold from interruptState:
      resolution: InterruptResolution | null
      busy: boolean
      error: string
    }

export type ChatPart = Part & { key: string }

/**
 * Per-message parts container. Only `parts` and `sources` are filled today;
 * `statusHistory` is declared now (frozen shape) but stays `[]` because no status
 * frames exist outside the live stream yet.
 */
export interface SourcePart {
  sourceId: number
  url: string
  title: string
  domain: string
  canonicalUrl?: string
  provider?: string
  fetched?: boolean
  fetchStatus?: string
}

export interface StatusPart {
  action: string
  label: string
  at: number
}

export interface TurnMessageParts {
  parts: ChatPart[]
  sources: SourcePart[]
  statusHistory: StatusPart[]
}
