import type { Ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { RpcClientError } from '@/lib/rpc'
import type {
  Attachment,
  ChatMessage,
  HiddenControlDispatchResult,
  ChatPendingItem,
  ChatSteerCapability,
} from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { CollaborationMode } from '@/types/plans'
import type { SandboxRunMode } from '@/types/sandbox'
import { normalizeSandboxRunMode } from '@/types/sandbox'
import type {
  ChatSendParams,
  ChatSendResponse,
  SessionSteerV2Params,
  SessionSteerV2Response,
} from '@/types/rpc'
import type { ChatRpcStreamApi } from '@/composables/chat/useChatRpcEventHandlers'
import type {
  BusySendMode,
  PendingQueueOwner,
  PendingQueueOwnerContext,
} from '@/composables/chat/useChatPendingQueue'
import { recordSessionNavigationDiag } from '@/utils/chat/sessionNavigationDiag'
import {
  hasSendableModelInputImageAttachment,
  isSendableAttachment,
  serializeDisplayAttachment,
  serializeSendableAttachment,
  type SendableAttachment,
} from '@/utils/chat/attachments'
import { localizedChatErrorMessage } from '@/utils/chat/errors'
import { rehomePromotedSteerRows } from '@/utils/chat/historyMerge'
import { isControlInput } from '@/utils/chat/inputSemantics'
import { createClientMessageId, createClientRequestId } from '@/utils/chat/messageIdentity'
import {
  type HiddenControlStorage,
  listHiddenControls,
  persistHiddenControlResult,
  removeHiddenControl,
} from '@/utils/chat/hiddenControlOutbox'
import {
  listPendingMetaDiscards,
  type MetaDiscardStorage,
  persistPendingMetaDiscard,
  removePendingMetaDiscard,
} from '@/utils/chat/metaDiscardOutbox'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
  STOPPED_STREAM_TASK_ID,
  taskTerminalMessage,
} from '@/utils/chat/streamEvents'

type RpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

interface SteerAttempt {
  clientRequestId: string
  clientMessageId: string
  expectedTurnId: string
  text: string
  visibleCommitted: boolean
}

interface SendAttempt {
  clientRequestId: string
  clientMessageId: string
  composerText: string
  requestSessionKey: string
  queueMode?: 'steer'
  text: string
  attachments: SendableAttachment[]
  intent: string | null
  initialCollaborationMode: CollaborationMode | null
  forkBeforeMessageId: string | null
  workspaceId: string | null
  params: ChatSendParams
  requiresIdempotentReplay?: boolean
}

export type ChatSendOutcome = 'accepted' | 'deferred' | 'not_sent' | 'retryable_failure'

interface ExplicitSendPayload {
  attachments: Attachment[]
  intent: string | null
  forkBeforeMessageId: string | null
  workspaceId?: string | null
  initialCollaborationMode?: CollaborationMode | null
}

interface ComposerSnapshot {
  revision: number | null
  inputText: string
  attachmentRefs: Attachment[]
  payloadAttachments: Attachment[]
  intent: string | null
  forkBeforeMessageId: string | null
  workspaceId: string | null
  initialCollaborationMode: CollaborationMode | null
}

interface DispatchSendOptions {
  composerText?: string
  queueMode?: 'steer'
  payload?: ExplicitSendPayload
  preserveComposer?: boolean
  composerSnapshot?: ComposerSnapshot
  cancelIfComposerChanged?: boolean
  retryAttempt?: SendAttempt | null
  rememberRetryableAttempt?: (attempt: SendAttempt) => void
}

interface ResponseHandoffGate {
  requestSessionKey: string
  ownerRequestId: string
  targetSessionKey: string | null
  stoppedByUser: boolean
  acceptedTaskId: string
  terminalResponse: boolean
  authoritativeIdle: boolean
  backgroundOnly: boolean
}

interface FreshSendToken {
  stoppedByUser: boolean
}

export type SendResponseSessionDecision =
  | { action: 'ignore'; reason: 'missing_response_session' | 'current_session_changed' | 'same_session' }
  | { action: 'persist'; responseSessionKey: string }

export function decideSendResponseSession(input: {
  requestSessionKey: string
  currentSessionKey: string
  responseSessionKey?: string | null
}): SendResponseSessionDecision {
  const responseSessionKey = input.responseSessionKey || ''
  if (!responseSessionKey) return { action: 'ignore', reason: 'missing_response_session' }
  if (input.currentSessionKey !== input.requestSessionKey) {
    return { action: 'ignore', reason: 'current_session_changed' }
  }
  if (responseSessionKey === input.currentSessionKey) {
    return { action: 'ignore', reason: 'same_session' }
  }
  return { action: 'persist', responseSessionKey }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

function errorCode(err: unknown): string | undefined {
  const code = (err as RpcClientError | null | undefined)?.code
  return typeof code === 'string' && code ? code : undefined
}

function sendFailureMessage(err: unknown): string {
  return localizedChatErrorMessage(errorCode(err), 'Send failed: ' + errorMessage(err))
}

function shouldRestoreSendAttempt(err: unknown): boolean {
  // Unknown acceptance (for example a lost response) is safe to retry because
  // the exact attempt keeps its durable clientRequestId. Only a positive
  // accepted signal proves that restoring the composer would be misleading.
  return (err as RpcClientError | null | undefined)?.accepted !== true
}

function hasUnknownAcceptance(err: unknown): boolean {
  const accepted = (err as RpcClientError | null | undefined)?.accepted
  return accepted !== true && accepted !== false
}

function rpcErrorDetail(err: unknown, key: string): unknown {
  const rpcError = err as RpcClientError | null | undefined
  if (rpcError && Object.prototype.hasOwnProperty.call(rpcError, key)) {
    return (rpcError as unknown as Record<string, unknown>)[key]
  }
  const details = rpcError?.details
  return details && typeof details === 'object'
    ? (details as Record<string, unknown>)[key]
    : undefined
}

function steerFallbackSafe(err: unknown): boolean {
  return rpcErrorDetail(err, 'fallback_safe') === true
    || rpcErrorDetail(err, 'fallbackSafe') === true
}

interface AcceptedErrorInfo {
  messageId: string
  sessionKey: string
  terminalWithoutTask: boolean
}

function acceptedErrorInfo(err: unknown): AcceptedErrorInfo | null {
  const rpcError = err as RpcClientError | null | undefined
  if (rpcError?.accepted !== true) return null
  const details = rpcError.details && typeof rpcError.details === 'object'
    ? rpcError.details as Record<string, unknown>
    : {}
  const rawMessageId = details.orphan_message_id ?? details.orphanMessageId
  const rawSessionKey = details.session_key ?? details.sessionKey
  return {
    messageId: typeof rawMessageId === 'string' ? rawMessageId : '',
    sessionKey: typeof rawSessionKey === 'string' ? rawSessionKey : '',
    terminalWithoutTask: rpcError.code === 'QUEUE_FULL_DIRTY',
  }
}

const TERMINAL_TASK_STATUSES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'timeout',
  'abandoned',
])

function terminalResponseStatus(response: ChatSendResponse | null | undefined): string {
  const status = String(response?.task_status || response?.taskStatus || '').toLowerCase()
  return TERMINAL_TASK_STATUSES.has(status) ? status : ''
}

function terminalReplayMessage(response: ChatSendResponse, status: string): string {
  const supplied = response.terminal_message || response.terminalMessage ||
    response.terminal_reason || response.terminalReason || response.reason
  if (typeof supplied === 'string' && supplied.trim()) return supplied.trim()
  return taskTerminalMessage(status, {})
}

function terminalReplayErrorCode(response: ChatSendResponse, status: string): string {
  const reason = response.terminal_reason || response.terminalReason || response.reason
  const normalized = typeof reason === 'string' ? reason.trim().toLowerCase() : ''
  return /^[a-z][a-z0-9_.-]*$/.test(normalized) ? normalized : status
}

function sameSendableAttachments(
  attachments: SendableAttachment[],
  attempt: SendAttempt,
): boolean {
  if (attachments.length !== attempt.attachments.length) return false
  return attachments.every((attachment, index) => {
    const prior = attempt.attachments[index]
    return (
      prior?.local_id === attachment.local_id &&
      JSON.stringify(serializeSendableAttachment(prior)) ===
        JSON.stringify(serializeSendableAttachment(attachment))
    )
  })
}

function matchesRecoveredDraft(
  attempt: SendAttempt,
  input: {
    requestSessionKey: string
    text: string
    attachments: SendableAttachment[]
    intent: string | null
    initialCollaborationMode: CollaborationMode | null
    forkBeforeMessageId: string | null
    workspaceId: string | null
  },
): boolean {
  return (
    attempt.requestSessionKey === input.requestSessionKey &&
    attempt.text === input.text &&
    attempt.intent === input.intent &&
    attempt.initialCollaborationMode === input.initialCollaborationMode &&
    attempt.forkBeforeMessageId === input.forkBeforeMessageId &&
    attempt.workspaceId === input.workspaceId &&
    sameSendableAttachments(input.attachments, attempt)
  )
}

function chatSourceMetadata(options: UseChatSendOptions): ChatSendParams['_source'] {
  const elevated = options.normalizeElevatedMode(options.elevatedMode.value)
  return {
    ...(elevated ? { elevated } : {}),
    runMode: normalizeSandboxRunMode(options.runMode.value),
  }
}

export interface UseChatSendOptions {
  rpc: RpcClient
  supportsMethod?: (method: string) => boolean
  activeSteerCapability?: Readonly<Ref<ChatSteerCapability | null>>
  inputText: Ref<string>
  messages: Ref<ChatMessage[]>
  sessionKey: Ref<string>
  pendingQueueOwnerContext: Ref<PendingQueueOwnerContext | null>
  busySendMode: Ref<BusySendMode>
  modelRoutingMode: Readonly<Ref<ModelRoutingMode>>
  modelRoutingSettingsBusy: Readonly<Ref<boolean>>
  elevatedMode: Ref<string>
  runMode: Ref<SandboxRunMode>
  pendingAttachments: Ref<Attachment[]>
  composerRevision?: Readonly<Ref<number>>
  pendingSessionIntent: Ref<string | null>
  initialCollaborationMode: Readonly<Ref<CollaborationMode>>
  pendingForkBeforeMessageId: Ref<string | null>
  pendingWorkspaceId?: Ref<string | null>
  sendBlockedReason?: Readonly<Ref<string | null>>
  validateActiveProjectBeforeSend?: () => Promise<string | null>
  acceptPendingWorkspaceBinding?: (workspaceId: string | null) => void
  materializeDraftSession?: (sessionKey: string) => void
  aborted: Ref<boolean>
  // Task id rendered by the live stream; a fresh turn binds it from the
  // chat.send response so a prior task's late events can't leak in (issue #344).
  activeStreamTaskId: Ref<string>
  activeStreamSessionKey: Ref<string>
  autoScroll: Ref<boolean>
  stream: ChatRpcStreamApi
  canStop?: () => boolean
  normalizeElevatedMode: (mode: string) => string
  adoptResponseSession: (
    key: string,
    ownerRequestId: string,
  ) => void
    | { authoritativeIdle: boolean; backgroundOnly?: boolean }
    | Promise<void | { authoritativeIdle: boolean; backgroundOnly?: boolean }>
  scheduleHistorySync: () => void
  schedulePendingDrainAfterTerminal: () => void
  flushDeferredPendingDrain: () => void
  // Event frames can beat the chat.send response. The event handler owns the
  // pending-terminal buffer and consumes only the task id accepted here.
  bindActiveStreamTask?: (taskId: string) => void
  isCompactInFlightForCurrentSession: () => boolean
  hasPendingAttachmentWork: () => boolean
  prepareAttachmentsForSend?: (options?: {
    isCurrent?: () => boolean
    attachments?: Attachment[]
  }) => Promise<boolean>
  enqueuePendingInput: (text: string, owner?: PendingQueueOwner) => boolean
  enqueuePendingPayload?: (
    payload: {
      text: string
      attachments?: Attachment[]
      intent?: string | null
    },
    owner?: PendingQueueOwner,
  ) => boolean
  enqueueHiddenControl?: (
    item: {
      text: string
      displayText: string
      clientRequestId?: string
      sessionKey?: string
      clientMessageId?: string
      visibleCommitted?: boolean
    },
    owner?: PendingQueueOwner,
  ) => boolean
  enqueuePendingSteerRetry?: (item: {
    text: string
    clientRequestId: string
    clientMessageId: string
    expectedTurnId: string
    visibleCommitted: boolean
  }) => boolean
  restoreSteerIntoComposer?: (text: string) => void
  popAllPendingIntoComposer: () => boolean
  hiddenControlStorage?: HiddenControlStorage | null
  metaDiscardStorage?: MetaDiscardStorage | null
  executeSlashCommand: (text: string) => Promise<boolean>
  closeSlashMenu: () => void
  autoResizeTextarea: () => void
  scrollToBottom: () => void
}

export function useChatSend(options: UseChatSendOptions) {
  const { pushToast } = useToasts()
  let activeFreshSendToken: FreshSendToken | null = null
  let activeResponseHandoff: ResponseHandoffGate | null = null
  let activeProjectPreflightToken: symbol | null = null
  let recoveredAttempt: SendAttempt | null = null
  const hiddenDispatchInFlight = new Map<string, Promise<HiddenControlDispatchResult>>()
  const renderedHiddenControls = new Set<string>()

  function metaDiscardStorage(): MetaDiscardStorage | null | undefined {
    return options.metaDiscardStorage
  }

  const recoveredQueuedAttempts = new WeakMap<ChatPendingItem, SendAttempt>()
  const recoveredQueuedSteers = new WeakMap<ChatPendingItem, SteerAttempt>()

  function pendingWorkspaceForIntent(intent: string | null): string | null {
    return intent === 'new_chat'
      ? options.pendingWorkspaceId?.value || null
      : null
  }

  function captureComposerSnapshot(): ComposerSnapshot {
    const intent = options.pendingSessionIntent.value
    const attachmentRefs = [...options.pendingAttachments.value]
    return {
      revision: options.composerRevision?.value ?? null,
      inputText: options.inputText.value,
      attachmentRefs,
      payloadAttachments: attachmentRefs.map(attachment => ({ ...attachment })),
      intent,
      forkBeforeMessageId: options.pendingForkBeforeMessageId.value,
      workspaceId: pendingWorkspaceForIntent(intent),
      initialCollaborationMode: initialModeForIntent(intent),
    }
  }

  function composerMatchesSnapshot(snapshot: ComposerSnapshot): boolean {
    if (
      snapshot.revision !== null
      && options.composerRevision
      && options.composerRevision.value !== snapshot.revision
    ) return false
    return (
      options.inputText.value === snapshot.inputText
      && options.pendingSessionIntent.value === snapshot.intent
      && options.pendingForkBeforeMessageId.value === snapshot.forkBeforeMessageId
      && pendingWorkspaceForIntent(options.pendingSessionIntent.value) === snapshot.workspaceId
      && options.pendingAttachments.value.length === snapshot.attachmentRefs.length
      && options.pendingAttachments.value.every(
        (attachment, index) => attachment === snapshot.attachmentRefs[index],
      )
    )
  }

  function payloadFromSnapshot(snapshot: ComposerSnapshot): ExplicitSendPayload {
    return {
      attachments: snapshot.payloadAttachments,
      intent: snapshot.intent,
      forkBeforeMessageId: snapshot.forkBeforeMessageId,
      workspaceId: snapshot.workspaceId,
      initialCollaborationMode: snapshot.initialCollaborationMode,
    }
  }

  function modelImageSendBlocked(attachments: readonly Attachment[]): boolean {
    if (!hasSendableModelInputImageAttachment(attachments)) return false
    return options.modelRoutingSettingsBusy.value
      || options.modelRoutingMode.value === 'llm_ensemble'
  }

  function activeSteerCapability(): ChatSteerCapability | null {
    return options.activeSteerCapability?.value || null
  }

  function capabilityExpectedTurnId(): string {
    return String(activeSteerCapability()?.expected_turn_id || '').trim()
  }

  function currentExpectedTurnId(): string {
    return String(
      capabilityExpectedTurnId()
      || options.activeStreamTaskId.value,
    ).trim()
  }

  function supportsSameTurnSteer(): boolean {
    const capability = activeSteerCapability()
    const expectedTurnId = capabilityExpectedTurnId()
    const activeTaskId = String(options.activeStreamTaskId.value || '').trim()
    const inputKinds = capability?.input_kinds
    return Boolean(
      options.supportsMethod?.('sessions.steer.v2')
      && capability?.mode === 'same_turn'
      && expectedTurnId
      && activeTaskId === expectedTurnId
      && (!inputKinds?.length || inputKinds.includes('text'))
      && options.modelRoutingMode.value !== 'llm_ensemble',
    )
  }

  function isPlainSteerPayload(
    text: string,
    attachments: readonly Attachment[],
    intent: string | null,
    forkBeforeMessageId: string | null,
  ): boolean {
    return !isControlInput(text)
      && attachments.length === 0
      && !intent
      && !forkBeforeMessageId
  }

  function canSteerPayload(
    text: string,
    attachments: readonly Attachment[],
    intent: string | null,
    forkBeforeMessageId: string | null,
  ): boolean {
    return supportsSameTurnSteer()
      && isPlainSteerPayload(text, attachments, intent, forkBeforeMessageId)
      && !options.isCompactInFlightForCurrentSession()
      && !responseHandoffBlocksCurrentSession()
  }

  async function refreshedActiveProjectBlocksSend(): Promise<boolean> {
    if (activeProjectPreflightToken) return true
    const token = Symbol('active-project-preflight')
    activeProjectPreflightToken = token
    const requestSessionKey = options.sessionKey.value
    try {
      const reason = await options.validateActiveProjectBeforeSend?.()
      if (options.sessionKey.value !== requestSessionKey) return true
      return Boolean(reason)
    } catch {
      return true
    } finally {
      if (activeProjectPreflightToken === token) {
        activeProjectPreflightToken = null
      }
    }
  }

  function acceptPendingWorkspaceBinding(workspaceId: string | null) {
    options.acceptPendingWorkspaceBinding?.(workspaceId)
    if (options.pendingWorkspaceId?.value === workspaceId) {
      options.pendingWorkspaceId.value = null
    }
  }

  function beginFreshStream(requestSessionKey: string): FreshSendToken {
    const token: FreshSendToken = { stoppedByUser: false }
    activeFreshSendToken = token
    options.activeStreamTaskId.value = PENDING_STREAM_TASK_ID
    options.activeStreamSessionKey.value = requestSessionKey
    options.stream.startStreaming()
    options.stream.showThinkingIndicator()
    return token
  }

  function pendingQueueOwner(): PendingQueueOwner | undefined {
    const context = options.pendingQueueOwnerContext.value
    return context?.sessionKey === options.sessionKey.value
      ? { ownerRequestId: context.ownerRequestId }
      : undefined
  }

  function initialModeForIntent(intent: string | null): CollaborationMode | null {
    return intent === 'new_chat' ? options.initialCollaborationMode.value : null
  }

  function consumeAcceptedSessionIntent(attempt: SendAttempt): void {
    if (options.sessionKey.value !== attempt.requestSessionKey) return
    if (attempt.intent === 'new_chat') {
      options.materializeDraftSession?.(attempt.requestSessionKey)
    }
    if (options.pendingSessionIntent.value === attempt.intent) {
      options.pendingSessionIntent.value = null
    }
    if (
      options.pendingWorkspaceId
      && attempt.intent === 'new_chat'
      && options.pendingWorkspaceId.value === attempt.workspaceId
    ) {
      acceptPendingWorkspaceBinding(attempt.workspaceId)
    }
  }

  function beginResponseHandoff(
    requestSessionKey: string,
    ownerRequestId: string,
  ): ResponseHandoffGate {
    const gate: ResponseHandoffGate = {
      requestSessionKey,
      ownerRequestId,
      targetSessionKey: null,
      stoppedByUser: false,
      acceptedTaskId: '',
      terminalResponse: false,
      authoritativeIdle: false,
      backgroundOnly: false,
    }
    activeResponseHandoff = gate
    options.pendingQueueOwnerContext.value = { sessionKey: requestSessionKey, ownerRequestId }
    return gate
  }

  function responseHandoffBlocksCurrentSession(): boolean {
    const gate = activeResponseHandoff
    if (!gate) return false
    const currentSessionKey = options.sessionKey.value
    return (
      currentSessionKey === gate.requestSessionKey
      || currentSessionKey === gate.targetSessionKey
    )
  }

  async function handoffResponseSession(key: string, gate: ResponseHandoffGate) {
    gate.targetSessionKey = key
    if (activeResponseHandoff === gate) {
      options.pendingQueueOwnerContext.value = {
        sessionKey: key,
        ownerRequestId: gate.ownerRequestId,
      }
    }
    const adoption = await options.adoptResponseSession(key, gate.ownerRequestId)
    gate.authoritativeIdle = adoption?.authoritativeIdle === true
    gate.backgroundOnly = adoption?.backgroundOnly === true
    if (gate.stoppedByUser && options.sessionKey.value === key) {
      options.aborted.value = true
      options.activeStreamTaskId.value = STOPPED_STREAM_TASK_ID
      options.activeStreamSessionKey.value = key
      if (options.stream.isStreaming.value) {
        options.stream.endStreaming({ reason: 'aborted' })
      }
      options.popAllPendingIntoComposer()
      return
    }
    const terminalReplayFinished = (
      options.activeStreamTaskId.value === FINISHED_STREAM_TASK_ID
      && gate.authoritativeIdle
    )
    const shouldPreserveAcceptedStream = (
      options.sessionKey.value === key
      && !gate.terminalResponse
      && !terminalReplayFinished
      && !gate.backgroundOnly
      && (!gate.authoritativeIdle || !gate.acceptedTaskId)
    )
    if (shouldPreserveAcceptedStream && !options.stream.isStreaming.value) {
      options.stream.startStreaming()
      options.stream.showThinkingIndicator()
    }
    if (
      shouldPreserveAcceptedStream
      && gate.acceptedTaskId
      && !options.activeStreamTaskId.value
    ) {
      bindAcceptedTask(gate.acceptedTaskId)
    }
    if (
      shouldPreserveAcceptedStream
      || (options.sessionKey.value === key && options.stream.isStreaming.value)
    ) {
      options.activeStreamSessionKey.value = key
    }
  }

  function finishResponseHandoff(gate: ResponseHandoffGate | null) {
    if (!gate || activeResponseHandoff !== gate) return
    const adoptedTargetIsCurrent = Boolean(
      gate.targetSessionKey
      && options.sessionKey.value === gate.targetSessionKey,
    )
    activeResponseHandoff = null
    if (options.pendingQueueOwnerContext.value?.ownerRequestId === gate.ownerRequestId) {
      options.pendingQueueOwnerContext.value = null
    }
    if (adoptedTargetIsCurrent && !gate.stoppedByUser) {
      options.flushDeferredPendingDrain()
      // An idle subscription snapshot can be authoritative without replaying
      // a terminal event. In that case there is no deferred signal to flush,
      // so explicitly release the adopted follow-up after hydration finishes.
      if (
        (gate.acceptedTaskId || options.activeStreamTaskId.value === FINISHED_STREAM_TASK_ID)
        && !gate.terminalResponse
        && gate.authoritativeIdle
        && !options.stream.isStreaming.value
        && (
          !options.activeStreamTaskId.value
          || options.activeStreamTaskId.value === FINISHED_STREAM_TASK_ID
        )
      ) {
        options.schedulePendingDrainAfterTerminal()
      }
    }
  }

  function freshSendStillOwnsStream(
    token: FreshSendToken | null,
    requestSessionKey: string,
  ): boolean {
    return (
      token !== null &&
      activeFreshSendToken === token &&
      options.sessionKey.value === requestSessionKey
    )
  }

  function acceptedTaskId(response: ChatSendResponse | null | undefined): string {
    return response?.task_id || response?.taskId || ''
  }

  function bindAcceptedUserMessage(
    clientMessageId: string,
    response: ChatSendResponse | null | undefined,
  ) {
    const messageId = response?.user_message_id || response?.message_id || ''
    bindUserMessageId(clientMessageId, messageId)
    const turnId = acceptedTaskId(response)
    if (!turnId) return
    const message = options.messages.value.find(item => item.clientId === clientMessageId)
    if (message) {
      message.turnId = turnId
      if (
        message.turnOutcome
        && ['webui_stop', 'webui_escape'].includes(
          String(message.turnOutcome.cancellationSource || ''),
        )
      ) {
        message.turnOutcome = {
          ...message.turnOutcome,
          turnId,
          taskId: turnId,
        }
      }
    }
  }

  function bindUserMessageId(clientMessageId: string, messageId: string) {
    if (!clientMessageId || !messageId) return
    const index = options.messages.value.findIndex(message => message.clientId === clientMessageId)
    if (index < 0) return
    const optimistic = options.messages.value[index]
    if (!optimistic || optimistic.messageId === messageId) return
    options.messages.value[index] = { ...optimistic, messageId }
  }

  function bindAcceptedTask(taskId: string) {
    if (options.bindActiveStreamTask) {
      options.bindActiveStreamTask(taskId)
      return
    }
    options.activeStreamTaskId.value = taskId
  }

  function reportAbortFailure(relevantSessionKeys?: string[]) {
    const message = 'Stop could not reach the server — the run may still be finishing.'
    if (
      relevantSessionKeys
      && !relevantSessionKeys.includes(options.sessionKey.value)
    ) {
      pushToast(message, { tone: 'warn', duration: 8000 })
      return
    }
    options.messages.value.push({
      role: 'system',
      text: message,
      ts: new Date().toISOString(),
    })
  }

  function handleTerminalResponse(
    response: ChatSendResponse,
    freshSendToken: FreshSendToken | null,
    optionsForResponse: { finishFreshStream: boolean },
  ): boolean {
    const status = terminalResponseStatus(response)
    if (!status) return false
    let finalizedFreshStream = false
    if (
      optionsForResponse.finishFreshStream
      && freshSendToken !== null
      && activeFreshSendToken === freshSendToken
    ) {
      activeFreshSendToken = null
      options.activeStreamTaskId.value = FINISHED_STREAM_TASK_ID
      options.activeStreamSessionKey.value = ''
      options.stream.endStreaming(status === 'cancelled' ? { reason: 'aborted' } : undefined)
      finalizedFreshStream = true
    }
    if (status !== 'succeeded') {
      const code = terminalReplayErrorCode(response, status)
      options.messages.value.push({
        role: 'error',
        text: localizedChatErrorMessage(code, terminalReplayMessage(response, status)),
        errorCode: code,
        terminalNotice: true,
        ts: new Date().toISOString(),
      })
    }
    options.scheduleHistorySync()
    if (finalizedFreshStream) {
      if (status === 'cancelled') options.popAllPendingIntoComposer()
      else options.schedulePendingDrainAfterTerminal()
    }
    return true
  }

  function abortStaleAcceptedTask(
    response: ChatSendResponse | null | undefined,
    requestSessionKey: string,
    force = false,
  ) {
    if (!force && options.sessionKey.value !== requestSessionKey) return
    const taskId = acceptedTaskId(response)
    if (!taskId && !force) return
    const acceptedSessionKey = response?.sessionKey || requestSessionKey
    const params: Record<string, string> = {
      sessionKey: acceptedSessionKey,
      source: 'webui_stale_send',
    }
    if (taskId) params.taskId = taskId
    options.rpc.call('chat.abort', params).catch(() => {
      if (force) reportAbortFailure([requestSessionKey, acceptedSessionKey])
    })
  }

  function steerMessage(attempt: SteerAttempt): ChatMessage | undefined {
    return options.messages.value.find(message =>
      message.clientId === attempt.clientMessageId
      || message.steerClientRequestId === attempt.clientRequestId,
    )
  }

  function pushSteerMessage(attempt: SteerAttempt) {
    if (attempt.visibleCommitted || steerMessage(attempt)) return
    options.stream.checkpointForUserMessage?.(attempt.expectedTurnId)
    options.messages.value.push({
      role: 'user',
      text: attempt.text,
      ts: new Date().toISOString(),
      clientId: attempt.clientMessageId,
      turnId: attempt.expectedTurnId,
      inputDisposition: 'steering',
      steerClientRequestId: attempt.clientRequestId,
      steerClientMessageId: attempt.clientMessageId,
    })
    attempt.visibleCommitted = true
    options.autoScroll.value = true
    options.scrollToBottom()
  }

  function removeSteerMessage(attempt: SteerAttempt) {
    const index = options.messages.value.findIndex(message =>
      message.clientId === attempt.clientMessageId
      || message.steerClientRequestId === attempt.clientRequestId,
    )
    if (index >= 0) options.messages.value.splice(index, 1)
  }

  function restoreSteerMessage(attempt: SteerAttempt) {
    const message = steerMessage(attempt)
    if (message?.steerRestored) return
    options.restoreSteerIntoComposer?.(attempt.text)
    if (message) message.steerRestored = true
  }

  function cancelStoppedSteerAttempt(
    attempt: SteerAttempt,
    queuedItem?: ChatPendingItem,
  ): ChatSendOutcome {
    const message = steerMessage(attempt)
    if (message) {
      message.inputDisposition = 'cancelled'
      message.steerStopRequested = false
    }
    restoreSteerMessage(attempt)
    if (queuedItem) recoveredQueuedSteers.delete(queuedItem)
    // The queue item has been resolved locally as not admitted; reporting an
    // accepted delivery outcome removes that transport lease instead of
    // retaining a second copy beside the restored composer text.
    return 'accepted'
  }

  function bindSteerResponse(
    attempt: SteerAttempt,
    response: SessionSteerV2Response,
  ) {
    const message = steerMessage(attempt)
    if (!message) return
    const messageId = String(response.user_message_id || '').trim()
    const turnId = String(
      response.disposition === 'promoted'
        ? response.promoted_turn_id || response.turn_id || attempt.expectedTurnId
        : response.turn_id || attempt.expectedTurnId,
    ).trim()
    const disposition = response.disposition || 'steering'
    const rawRevision = Number(response.revision)
    const revision = Number.isInteger(rawRevision) && rawRevision >= 0
      ? rawRevision
      : undefined
    if (messageId) message.messageId = messageId
    const currentRevision = message.inputDispositionRevision
    const responseIsCurrent = currentRevision === undefined
      || (revision !== undefined && revision >= currentRevision)
    if (turnId && responseIsCurrent) message.turnId = turnId
    // Stop may win locally while the acceptance response is still in flight.
    // The later typed disposition event remains authoritative; never repaint a
    // locally-cancelled adjustment as waiting in the meantime.
    if (
      responseIsCurrent
      && (
        !message.inputDisposition
        || message.inputDisposition === 'steering'
      )
    ) {
      message.inputDisposition = disposition
      if (revision !== undefined) message.inputDispositionRevision = revision
    }
    if (
      responseIsCurrent
      && disposition === 'promoted'
      && message.inputDisposition === 'promoted'
    ) {
      message.promotedFromTurnId = String(
        response.promoted_from_turn_id || attempt.expectedTurnId,
      ).trim()
      options.messages.value = rehomePromotedSteerRows(options.messages.value)
    }
    if (
      responseIsCurrent
      && ['applied', 'promoted', 'cancelled', 'rejected'].includes(disposition)
    ) {
      message.steerStopRequested = false
    }
    if (responseIsCurrent && disposition === 'cancelled') {
      restoreSteerMessage(attempt)
    }
  }

  function enqueueSafeSteerFallback(
    attempt: SteerAttempt,
    queuedItem?: ChatPendingItem,
  ): ChatSendOutcome {
    removeSteerMessage(attempt)
    if (queuedItem) {
      recoveredQueuedSteers.delete(queuedItem)
      queuedItem.steerClientRequestId = undefined
      queuedItem.steerClientMessageId = undefined
      queuedItem.steerExpectedTurnId = undefined
      queuedItem.steerVisibleCommitted = undefined
      return 'deferred'
    }
    const queued = options.enqueuePendingPayload?.({
      text: attempt.text,
      attachments: [],
      intent: null,
    }, pendingQueueOwner()) ?? false
    if (!queued) restoreSteerMessage(attempt)
    return queued ? 'accepted' : 'not_sent'
  }

  function rememberSteerRetry(
    attempt: SteerAttempt,
    queuedItem?: ChatPendingItem,
  ): ChatSendOutcome {
    if (queuedItem) {
      recoveredQueuedSteers.set(queuedItem, attempt)
      queuedItem.steerClientRequestId = attempt.clientRequestId
      queuedItem.steerClientMessageId = attempt.clientMessageId
      queuedItem.steerExpectedTurnId = attempt.expectedTurnId
      queuedItem.steerVisibleCommitted = attempt.visibleCommitted
      return 'retryable_failure'
    }
    const queued = options.enqueuePendingSteerRetry?.({
      text: attempt.text,
      clientRequestId: attempt.clientRequestId,
      clientMessageId: attempt.clientMessageId,
      expectedTurnId: attempt.expectedTurnId,
      visibleCommitted: attempt.visibleCommitted,
    }) ?? false
    if (!queued) {
      const message = steerMessage(attempt)
      if (message) message.inputDisposition = 'rejected'
    }
    return queued ? 'accepted' : 'retryable_failure'
  }

  async function dispatchSteerV2(
    text: string,
    optionsForSteer: {
      composerSnapshot?: ComposerSnapshot
      queuedItem?: ChatPendingItem
    } = {},
  ): Promise<ChatSendOutcome> {
    const requestSessionKey = options.sessionKey.value
    const queuedItem = optionsForSteer.queuedItem
    const recovered = queuedItem
      ? recoveredQueuedSteers.get(queuedItem)
        || (
          queuedItem.steerClientRequestId
          && queuedItem.steerClientMessageId
          && queuedItem.steerExpectedTurnId
            ? {
                clientRequestId: queuedItem.steerClientRequestId,
                clientMessageId: queuedItem.steerClientMessageId,
                expectedTurnId: queuedItem.steerExpectedTurnId,
                text: queuedItem.text,
                visibleCommitted: queuedItem.steerVisibleCommitted === true,
              }
            : null
        )
      : null
    if (!requestSessionKey || !text.trim()) return 'not_sent'
    if (!options.supportsMethod?.('sessions.steer.v2')) {
      return recovered ? 'retryable_failure' : 'not_sent'
    }
    if (
      !recovered
      && !canSteerPayload(
        text,
        queuedItem ? queuedItem.attachments : options.pendingAttachments.value,
        queuedItem ? queuedItem.intent : options.pendingSessionIntent.value,
        queuedItem ? null : options.pendingForkBeforeMessageId.value,
      )
    ) return 'not_sent'
    if (options.sendBlockedReason?.value || options.hasPendingAttachmentWork()) {
      return recovered ? 'retryable_failure' : 'not_sent'
    }
    if (
      optionsForSteer.composerSnapshot
      && !composerMatchesSnapshot(optionsForSteer.composerSnapshot)
    ) return 'not_sent'

    const expectedTurnId = recovered?.expectedTurnId || capabilityExpectedTurnId()
    if (!expectedTurnId) return 'not_sent'
    const attempt: SteerAttempt = recovered || {
      clientRequestId: createClientRequestId(),
      clientMessageId: createClientMessageId(),
      expectedTurnId,
      text: text.trim(),
      visibleCommitted: false,
    }
    pushSteerMessage(attempt)
    if (queuedItem) {
      queuedItem.steerClientRequestId = attempt.clientRequestId
      queuedItem.steerClientMessageId = attempt.clientMessageId
      queuedItem.steerExpectedTurnId = attempt.expectedTurnId
      queuedItem.steerVisibleCommitted = true
    } else {
      options.inputText.value = ''
      options.pendingSessionIntent.value = null
      options.pendingForkBeforeMessageId.value = null
      options.autoResizeTextarea()
    }

    const params: SessionSteerV2Params = {
      key: requestSessionKey,
      message: attempt.text,
      expected_turn_id: attempt.expectedTurnId,
      client_request_id: attempt.clientRequestId,
      client_message_id: attempt.clientMessageId,
      surface_id: 'webui',
      _source: chatSourceMetadata(options),
    }
    try {
      const response = await options.rpc.call<SessionSteerV2Response>(
        'sessions.steer.v2',
        params as unknown as Record<string, unknown>,
      )
      if (options.sessionKey.value !== requestSessionKey) {
        return response.accepted === false ? 'not_sent' : 'accepted'
      }
      if (response.accepted === false) {
        if (steerMessage(attempt)?.steerStopRequested) {
          return cancelStoppedSteerAttempt(attempt, queuedItem)
        }
        if (response.fallback_safe === true) {
          return enqueueSafeSteerFallback(attempt, queuedItem)
        }
        const message = steerMessage(attempt)
        if (message) message.inputDisposition = response.disposition || 'rejected'
        restoreSteerMessage(attempt)
        return 'not_sent'
      }
      bindSteerResponse(attempt, response)
      if (queuedItem) recoveredQueuedSteers.delete(queuedItem)
      options.scheduleHistorySync()
      return 'accepted'
    } catch (error: unknown) {
      if ((error as RpcClientError | null | undefined)?.accepted === true) {
        const message = steerMessage(attempt)
        const acceptedMessageId = String(
          rpcErrorDetail(error, 'user_message_id')
          || rpcErrorDetail(error, 'message_id')
          || '',
        )
        if (message && acceptedMessageId) message.messageId = acceptedMessageId
        options.scheduleHistorySync()
        return 'accepted'
      }
      if (steerFallbackSafe(error)) {
        if (steerMessage(attempt)?.steerStopRequested) {
          return cancelStoppedSteerAttempt(attempt, queuedItem)
        }
        return enqueueSafeSteerFallback(attempt, queuedItem)
      }
      if (
        hasUnknownAcceptance(error)
        || rpcErrorDetail(error, 'retryable') === true
      ) {
        return rememberSteerRetry(attempt, queuedItem)
      }
      const message = steerMessage(attempt)
      if (message) message.inputDisposition = 'rejected'
      restoreSteerMessage(attempt)
      return 'not_sent'
    }
  }

  async function onSend(invocation: {
    bypassSlashCommand?: boolean
    composerText?: string
    textOverride?: string
    cancelIfComposerChanged?: boolean
  } = {}) {
    const requestSessionKey = options.sessionKey.value
    const composerSnapshot = captureComposerSnapshot()
    const bypassSlashCommand = invocation.bypassSlashCommand === true
    const composerText = invocation.composerText ?? options.inputText.value
    let text = (invocation.textOverride ?? options.inputText.value).trim()
    let sendableAttachments = options.pendingAttachments.value.filter(isSendableAttachment)
    let hasPayload = text || sendableAttachments.length > 0
    let isLiteralSlash = false
    const handoffInFlight = responseHandoffBlocksCurrentSession()

    if (options.hasPendingAttachmentWork()) {
      pushToast(i18n.global.t('chat.toast.waitAttachments'), { tone: 'info' })
      return
    }

    if (!bypassSlashCommand && text.startsWith('//')) {
      isLiteralSlash = true
      text = text.slice(1)
      sendableAttachments = options.pendingAttachments.value.filter(isSendableAttachment)
      hasPayload = text || sendableAttachments.length > 0
    }

    if (hasPayload) {
      // Transport readiness is a fail-closed precondition. Keep the composer
      // and attachment refs untouched so manual, keyboard, and automatic sends
      // all preserve the exact draft until live subscription recovery succeeds.
      if (options.sendBlockedReason?.value) return
      if (options.validateActiveProjectBeforeSend) {
        if (await refreshedActiveProjectBlocksSend()) return
      }
      if (options.sessionKey.value !== requestSessionKey) return
      if (options.sendBlockedReason?.value) return
      if (
        invocation.cancelIfComposerChanged
        && !composerMatchesSnapshot(composerSnapshot)
      ) return
    }

    // An unknown acceptance must be resolved by replaying the exact original
    // request before any edited draft or mode change can become a new turn.
    // Otherwise a committed new_chat can be stranded behind a second request
    // id that conflicts with the already-created session.
    if (
      !handoffInFlight
      && recoveredAttempt?.requiresIdempotentReplay
      && recoveredAttempt.requestSessionKey === options.sessionKey.value
    ) {
      await dispatchSend(recoveredAttempt.text, {
        composerText,
        queueMode: recoveredAttempt.queueMode,
        composerSnapshot,
        cancelIfComposerChanged: invocation.cancelIfComposerChanged,
      })
      return
    }

    // Retry an explicitly rejected prior send with its exact original queue
    // semantics when the visible draft is unchanged.
    if (
      !handoffInFlight &&
      recoveredAttempt &&
      matchesRecoveredDraft(recoveredAttempt, {
        requestSessionKey: options.sessionKey.value,
        text,
        attachments: sendableAttachments,
        intent: composerSnapshot.intent,
        initialCollaborationMode: composerSnapshot.initialCollaborationMode,
        forkBeforeMessageId: composerSnapshot.forkBeforeMessageId,
        workspaceId: composerSnapshot.workspaceId,
      })
    ) {
      await dispatchSend(text, {
        composerText,
        queueMode: recoveredAttempt.queueMode,
        payload: payloadFromSnapshot(composerSnapshot),
        composerSnapshot,
        cancelIfComposerChanged: invocation.cancelIfComposerChanged,
      })
      return
    }

    const compactInFlight = options.isCompactInFlightForCurrentSession()
    if (options.stream.isStreaming.value || compactInFlight || handoffInFlight) {
      if (!bypassSlashCommand && !isLiteralSlash && isControlInput(text)) {
        const queued = options.enqueuePendingInput(text, pendingQueueOwner())
        if (!queued) pushToast(i18n.global.t('chat.toast.queueFull'), { tone: 'info' })
        return
      }
      if (!hasPayload) return
      if (
        options.busySendMode.value === 'steer'
        && canSteerPayload(
          text,
          composerSnapshot.payloadAttachments,
          composerSnapshot.intent,
          composerSnapshot.forkBeforeMessageId,
        )
      ) {
        await dispatchSteerV2(text, { composerSnapshot })
        return
      }
      // Surface a full queue instead of silently dropping the send: the draft is
      // preserved (enqueue returns false before clearing the composer).
      const composerChanged = !composerMatchesSnapshot(composerSnapshot)
      if (invocation.cancelIfComposerChanged && composerChanged) return
      const queued = composerChanged || invocation.textOverride !== undefined
        ? options.enqueuePendingPayload?.({
            text,
            attachments: composerSnapshot.payloadAttachments,
            intent: composerSnapshot.intent,
          }, pendingQueueOwner()) ?? false
        : options.enqueuePendingInput(text, pendingQueueOwner())
      if (!queued) {
        pushToast(i18n.global.t('chat.toast.queueFull'), { tone: 'info' })
      }
      return
    }

    if (!bypassSlashCommand && !isLiteralSlash && text.startsWith('/')) {
      if (!composerMatchesSnapshot(composerSnapshot)) return
      const handled = await options.executeSlashCommand(text)
      if (handled) return
    }

    if (!hasPayload || !options.sessionKey.value) return

    await dispatchSend(text, {
      composerText,
      payload: payloadFromSnapshot(composerSnapshot),
      composerSnapshot,
      cancelIfComposerChanged: invocation.cancelIfComposerChanged,
    })
  }

  async function dispatchComposerPrompt(prompt: string, composerText: string) {
    await onSend({
      bypassSlashCommand: true,
      composerText,
      textOverride: prompt,
    })
  }

  /**
   * Send one queued item without staging it in the visible composer.
   *
   * The queued snapshot owns its attachment refresh and retry identity. This
   * lets an operator keep typing while the steer RPC is in flight, and a lost
   * response can be retried with the same idempotency key without replacing
   * that unrelated draft.
   */
  async function sendQueuedItem(
    item: ChatPendingItem,
    delivery: 'followup' | 'steer',
    expectedSessionKey?: string,
  ): Promise<ChatSendOutcome> {
    const text = item.text.trim()
    const ownerSessionKey = expectedSessionKey
      || item.ownerSessionKey
      || options.sessionKey.value
    const retryAttempt = recoveredQueuedAttempts.get(item) ?? null
    const steerRetryAttempt = recoveredQueuedSteers.get(item)
      || (
        item.steerClientRequestId
        && item.steerClientMessageId
        && item.steerExpectedTurnId
          ? item
          : null
      )
    const preserveRetryState = (outcome: ChatSendOutcome): ChatSendOutcome => (
      (retryAttempt || steerRetryAttempt)
      && (outcome === 'deferred' || outcome === 'not_sent')
        ? 'retryable_failure'
        : outcome
    )
    const blockedOutcome = () => preserveRetryState(
      delivery === 'followup' ? 'deferred' : 'not_sent',
    )
    if (!ownerSessionKey || options.sessionKey.value !== ownerSessionKey) {
      return preserveRetryState('not_sent')
    }
    if (options.sendBlockedReason?.value) {
      return blockedOutcome()
    }
    if (options.validateActiveProjectBeforeSend) {
      if (await refreshedActiveProjectBlocksSend()) return blockedOutcome()
      if (options.sessionKey.value !== ownerSessionKey) {
        return preserveRetryState('not_sent')
      }
      if (options.sendBlockedReason?.value) return blockedOutcome()
    }
    if (options.hasPendingAttachmentWork()) {
      if (delivery === 'steer') {
        pushToast(i18n.global.t('chat.toast.waitAttachments'), { tone: 'info' })
      }
      return preserveRetryState(delivery === 'followup' ? 'deferred' : 'not_sent')
    }
    if (item.attachments.some(attachment => !isSendableAttachment(attachment))) {
      return preserveRetryState('not_sent')
    }
    if (
      delivery === 'followup'
      && !item.hiddenControl
      && item.attachments.length === 0
      && item.text.trim().startsWith('/')
      && !item.text.trim().startsWith('//')
    ) {
      if (options.stream.isStreaming.value || options.isCompactInFlightForCurrentSession()) {
        return preserveRetryState('deferred')
      }
      return await options.executeSlashCommand(item.text.trim())
        ? 'accepted'
        : preserveRetryState('not_sent')
    }
    if (hasSendableModelInputImageAttachment(item.attachments)) {
      if (options.modelRoutingSettingsBusy.value) {
        return preserveRetryState(delivery === 'followup' ? 'deferred' : 'not_sent')
      }
      if (options.modelRoutingMode.value === 'llm_ensemble') {
        return preserveRetryState('not_sent')
      }
    }
    if (
      options.isCompactInFlightForCurrentSession()
      || responseHandoffBlocksCurrentSession()
      || (delivery === 'followup' && options.stream.isStreaming.value)
    ) {
      return preserveRetryState(delivery === 'followup' ? 'deferred' : 'not_sent')
    }

    if (delivery === 'steer') {
      return dispatchSteerV2(text, { queuedItem: item })
    }
    const outcome = await dispatchSend(text, {
      composerText: item.text,
      payload: {
        attachments: item.attachments,
        intent: item.intent,
        // A queued follow-up has no fork target. In particular, never inherit
        // the fork target of the unrelated draft currently in the composer.
        forkBeforeMessageId: null,
      },
      preserveComposer: true,
      retryAttempt,
      rememberRetryableAttempt: attempt => {
        recoveredQueuedAttempts.set(item, attempt)
      },
    })
    if (outcome === 'accepted') {
      recoveredQueuedAttempts.delete(item)
    }
    return preserveRetryState(outcome)
  }

  function sendQueuedSteer(
    item: ChatPendingItem,
    expectedSessionKey?: string,
  ): Promise<ChatSendOutcome> {
    return sendQueuedItem(item, 'steer', expectedSessionKey)
  }

  function sendQueuedFollowup(
    item: ChatPendingItem,
    expectedSessionKey?: string,
  ): Promise<ChatSendOutcome> {
    return sendQueuedItem(item, 'followup', expectedSessionKey)
  }

  async function dispatchSend(
    text: string,
    sendOpts: DispatchSendOptions = {},
  ): Promise<ChatSendOutcome> {
    const requestSessionKey = options.sessionKey.value
    if (!requestSessionKey) return 'not_sent'
    if (options.sendBlockedReason?.value) return 'not_sent'
    let preserveComposer = sendOpts.preserveComposer === true
    const sourceAttachments = sendOpts.payload?.attachments ?? options.pendingAttachments.value
    const intent = sendOpts.payload
      ? sendOpts.payload.intent
      : options.pendingSessionIntent.value
    const forkBeforeMessageId = sendOpts.payload
      ? sendOpts.payload.forkBeforeMessageId
      : options.pendingForkBeforeMessageId.value
    // Only the first new-task attempt owns the pending workspace. Follow-up
    // queue/steer sends may run before it is accepted, but must neither inherit
    // nor clear that project binding.
    const workspaceId = sendOpts.payload && 'workspaceId' in sendOpts.payload
      ? sendOpts.payload.workspaceId ?? null
      : pendingWorkspaceForIntent(intent)
    const initialCollaborationMode = (
      sendOpts.payload
      && 'initialCollaborationMode' in sendOpts.payload
    )
      ? sendOpts.payload.initialCollaborationMode ?? null
      : initialModeForIntent(intent)
    const initialSendableAttachments = sourceAttachments.filter(isSendableAttachment)
    // This is deliberately before optimistic rendering, composer clearing,
    // stream state, and chat.send. A blocked draft remains exactly editable.
    if (modelImageSendBlocked(initialSendableAttachments)) return 'not_sent'
    const retryCandidate = sendOpts.retryAttempt ?? (preserveComposer ? null : recoveredAttempt)
    const requiresRecoveryReplay = Boolean(
      retryCandidate?.requiresIdempotentReplay
      && retryCandidate.requestSessionKey === requestSessionKey
      && retryCandidate.queueMode === sendOpts.queueMode,
    )
    const isRecoveredRetry = Boolean(
      requiresRecoveryReplay
      || (
        retryCandidate
        && matchesRecoveredDraft(retryCandidate, {
          requestSessionKey,
          text,
          attachments: initialSendableAttachments,
          intent,
          initialCollaborationMode,
          forkBeforeMessageId,
          workspaceId,
        })
        && retryCandidate.queueMode === sendOpts.queueMode
      ),
    )
    const retryAttempt = isRecoveredRetry ? retryCandidate : null
    const sendAttachmentIds = new Set(
      (retryAttempt?.attachments || initialSendableAttachments)
        .map(attachment => attachment.local_id),
    )
    // A recovered attempt must keep the exact serialized attachment tokens and
    // metadata that were fingerprinted with its idempotency key.
    if (!retryAttempt && options.prepareAttachmentsForSend) {
      const ready = await options.prepareAttachmentsForSend({
        isCurrent: () => options.sessionKey.value === requestSessionKey,
        ...(sendOpts.payload ? { attachments: sourceAttachments } : {}),
      })
      if (!ready) return 'not_sent'
      if (options.sessionKey.value !== requestSessionKey) return 'not_sent'
    }
    const composerChanged = sendOpts.composerSnapshot
      ? !composerMatchesSnapshot(sendOpts.composerSnapshot)
      : false
    if (sendOpts.cancelIfComposerChanged && composerChanged) return 'not_sent'
    if (composerChanged) preserveComposer = true
    const currentSourceAttachments = sendOpts.payload?.attachments
      ?? options.pendingAttachments.value
    if (
      preserveComposer
      && sendOpts.payload
      && currentSourceAttachments.some(attachment => !isSendableAttachment(attachment))
    ) {
      return 'not_sent'
    }
    const attachmentsToSend = retryAttempt?.attachments || currentSourceAttachments.filter(
      (attachment): attachment is SendableAttachment =>
        sendAttachmentIds.has(attachment.local_id) && isSendableAttachment(attachment),
    )
    // Routing can change while an expiring staged upload is refreshed. Recheck
    // the authoritative live state before any visible or RPC mutation.
    if (options.sendBlockedReason?.value) return 'not_sent'
    if (modelImageSendBlocked(attachmentsToSend)) return 'not_sent'
    const attachmentsToKeep = currentSourceAttachments.filter(
      attachment => !sendAttachmentIds.has(attachment.local_id) || !isSendableAttachment(attachment),
    )
    if (!text && attachmentsToSend.length === 0) return 'not_sent'

    options.aborted.value = false
    if (!preserveComposer) options.closeSlashMenu()
    recordSessionNavigationDiag('send.start', {
      requestSession: requestSessionKey,
      current: requestSessionKey,
    })

    const userText = text
    let attempt = retryAttempt
    if (!attempt) {
      const clientMessageId = createClientMessageId()
      const params: ChatSendParams = {
        clientRequestId: createClientRequestId(),
        clientMessageId,
        message: text || 'Describe these attachments',
        // The Vue client never uses the legacy cancel-style steer path. Make
        // ordinary sends explicit so a persisted session queue_mode="steer"
        // from an older client cannot silently turn them into interrupts.
        queueMode: sendOpts?.queueMode ?? 'followup',
        sessionKey: requestSessionKey,
      }
      params._source = chatSourceMetadata(options)
      if (intent) params.intent = intent
      if (intent === 'new_chat' && workspaceId) params.workspaceId = workspaceId
      if (initialCollaborationMode === 'plan') {
        params.collaborationMode = initialCollaborationMode
      }
      if (forkBeforeMessageId) params.forkBeforeMessageId = forkBeforeMessageId
      if (attachmentsToSend.length > 0) {
        params.displayText = userText
        params.attachments = attachmentsToSend.map(serializeSendableAttachment)
      }
      attempt = {
        clientRequestId: params.clientRequestId!,
        clientMessageId,
        composerText: sendOpts?.composerText ?? text,
        requestSessionKey,
        queueMode: sendOpts?.queueMode,
        text,
        attachments: attachmentsToSend.map(attachment => ({ ...attachment })),
        intent,
        initialCollaborationMode,
        forkBeforeMessageId,
        workspaceId,
        params,
      }
      const now = new Date().toISOString()
      const displayAttachments = attachmentsToSend.map(serializeDisplayAttachment)
      options.messages.value.push({
        role: 'user',
        text: userText,
        ts: now,
        clientId: clientMessageId,
        ...(displayAttachments.length > 0 ? { attachments: displayAttachments } : {}),
      })
      options.autoScroll.value = true
      options.scrollToBottom()
    }
    if (!preserveComposer) {
      recoveredAttempt = null
      const composerTextBeforeSend = options.inputText.value
      const preserveEditedComposer = Boolean(
        retryAttempt?.requiresIdempotentReplay
        && composerTextBeforeSend
        && composerTextBeforeSend !== retryAttempt.composerText
      )
      options.inputText.value = preserveEditedComposer ? composerTextBeforeSend : ''
      options.autoResizeTextarea()
      options.pendingAttachments.value = attachmentsToKeep
      if (options.pendingForkBeforeMessageId.value === forkBeforeMessageId) {
        options.pendingForkBeforeMessageId.value = null
      }
    } else if (sendOpts.composerSnapshot) {
      const originalAttachmentRefs = new Set(sendOpts.composerSnapshot.attachmentRefs)
      options.pendingAttachments.value = options.pendingAttachments.value.filter(
        attachment => !originalAttachmentRefs.has(attachment),
      )
    }
    // A steer send rides an already-active stream; restarting it would wipe
    // the partial output of the run being steered.
    const wasStreaming = options.stream.isStreaming.value
    const freshSendToken = wasStreaming
      ? null
      : beginFreshStream(requestSessionKey)
    let responseHandoff = (
      attempt.forkBeforeMessageId
        ? beginResponseHandoff(requestSessionKey, attempt.clientRequestId)
        : null
    )

    try {
      const res = await options.rpc.call<ChatSendResponse>('chat.send', attempt.params)
      if (recoveredAttempt?.clientRequestId === attempt.clientRequestId) {
        recoveredAttempt = null
      }
      // A draft becomes a routable session only after the gateway has durably
      // accepted its first turn. Keeping the intent until this point avoids
      // remounting onto an empty history when acceptance fails or is unknown.
      consumeAcceptedSessionIntent(attempt)
      const taskId = acceptedTaskId(res)
      const terminalStatus = terminalResponseStatus(res)
      if (responseHandoff) {
        responseHandoff.acceptedTaskId = taskId
        responseHandoff.terminalResponse = Boolean(terminalStatus)
      }
      const stoppedByUser = freshSendToken?.stoppedByUser === true
        || responseHandoff?.stoppedByUser === true
      const lostFreshStream = !wasStreaming
        && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)
      if (stoppedByUser || lostFreshStream) {
        const acceptedSessionKey = res?.sessionKey || requestSessionKey
        // A same-session accepted row remains part of the visible parent even
        // after Stop or a newer send. A child identity must never be written
        // onto that parent row; the child history owns it after handoff.
        if (
          options.sessionKey.value === requestSessionKey
          && acceptedSessionKey === requestSessionKey
        ) {
          bindAcceptedUserMessage(attempt.clientMessageId, res)
        }
        abortStaleAcceptedTask(res, requestSessionKey, stoppedByUser)
        if (
          stoppedByUser
          && options.sessionKey.value === requestSessionKey
          && acceptedSessionKey !== requestSessionKey
        ) {
          responseHandoff ||= beginResponseHandoff(
            requestSessionKey,
            attempt.clientRequestId,
          )
          responseHandoff.stoppedByUser = true
          responseHandoff.acceptedTaskId = taskId
          responseHandoff.terminalResponse = Boolean(terminalStatus)
          await handoffResponseSession(acceptedSessionKey, responseHandoff)
        }
        return 'accepted'
      }
      if ((res?.sessionKey || requestSessionKey) === requestSessionKey) {
        bindAcceptedUserMessage(attempt.clientMessageId, res)
      }
      // Bind the live stream to this turn's task so a prior task's late events
      // can't bleed into it (issue #344). Only a fresh turn takes over rendering
      // — a steer/queue send rides the in-flight stream and must not rebind —
      // and only while this session is still the one on screen.
      const responseIsCurrent = options.sessionKey.value === requestSessionKey
      if (!terminalStatus && !wasStreaming && responseIsCurrent) {
        options.activeStreamSessionKey.value = res?.sessionKey || requestSessionKey
        if (taskId) bindAcceptedTask(taskId)
      }
      const decision = decideSendResponseSession({
        requestSessionKey,
        currentSessionKey: options.sessionKey.value,
        responseSessionKey: res?.sessionKey,
      })
      const terminalSessionKey = decision.action === 'persist'
        ? decision.responseSessionKey
        : requestSessionKey
      if (decision.action === 'persist') {
        recordSessionNavigationDiag('send.response.persist', {
          requestSession: requestSessionKey,
          responseSession: decision.responseSessionKey,
          current: options.sessionKey.value,
        })
        responseHandoff ||= beginResponseHandoff(requestSessionKey, attempt.clientRequestId)
        responseHandoff.acceptedTaskId = taskId
        responseHandoff.terminalResponse = Boolean(terminalStatus)
        await handoffResponseSession(decision.responseSessionKey, responseHandoff)
      } else if (decision.reason === 'current_session_changed') {
        recordSessionNavigationDiag('send.response.stale', {
          requestSession: requestSessionKey,
          responseSession: res?.sessionKey,
          current: options.sessionKey.value,
          reason: decision.reason,
        })
      }
      if (
        terminalStatus
        && responseIsCurrent
        && options.sessionKey.value === terminalSessionKey
      ) {
        handleTerminalResponse(res, freshSendToken, {
          finishFreshStream: !wasStreaming,
        })
        // A terminal task response (including first-attempt activation failure)
        // may have no future live event. Fresh turns close their spinner;
        // steer responses only surface the result without ending the older run.
      }
      return 'accepted'
    } catch (err: unknown) {
      const acceptedError = acceptedErrorInfo(err)
      if (
        acceptedError
        && recoveredAttempt?.clientRequestId === attempt.clientRequestId
      ) {
        recoveredAttempt = null
      }
      if (acceptedError) consumeAcceptedSessionIntent(attempt)
      const acceptedSessionKey = acceptedError?.sessionKey || requestSessionKey
      const rememberRetryableAttempt = (restoreComposer: boolean) => {
        if (!shouldRestoreSendAttempt(err)) return
        if (preserveComposer) {
          const remembered = {
            ...attempt,
            requiresIdempotentReplay: hasUnknownAcceptance(err),
          }
          if (sendOpts.rememberRetryableAttempt) {
            sendOpts.rememberRetryableAttempt(remembered)
          } else {
            recoveredAttempt = remembered
          }
        } else if (restoreComposer) {
          restoreSendAttempt(attempt, {
            requiresIdempotentReplay: hasUnknownAcceptance(err),
          })
        }
      }
      const stoppedByUser = freshSendToken?.stoppedByUser === true
        || responseHandoff?.stoppedByUser === true
      if (
        acceptedError
        && stoppedByUser
        && !acceptedError.terminalWithoutTask
        && acceptedSessionKey !== requestSessionKey
      ) {
        abortStaleAcceptedTask(
          { sessionKey: acceptedSessionKey },
          requestSessionKey,
          true,
        )
      }
      if (
        acceptedError
        && options.sessionKey.value === requestSessionKey
        && acceptedSessionKey !== requestSessionKey
      ) {
        if (!wasStreaming && activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
          options.activeStreamTaskId.value = ''
          options.activeStreamSessionKey.value = ''
          options.stream.endStreaming()
        }
        responseHandoff ||= beginResponseHandoff(requestSessionKey, attempt.clientRequestId)
        responseHandoff.stoppedByUser = stoppedByUser
        responseHandoff.terminalResponse = acceptedError.terminalWithoutTask
        await handoffResponseSession(acceptedSessionKey, responseHandoff)
        options.scheduleHistorySync()
        if (acceptedError.terminalWithoutTask && !stoppedByUser) {
          options.schedulePendingDrainAfterTerminal()
        }
        options.messages.value.push({
          role: 'error',
          text: sendFailureMessage(err),
          errorCode: errorCode(err),
          ts: new Date().toISOString(),
        })
        return 'accepted'
      }
      if (acceptedError && options.sessionKey.value === requestSessionKey) {
        bindUserMessageId(attempt.clientMessageId, acceptedError.messageId)
        options.scheduleHistorySync()
      }
      if (options.sessionKey.value !== requestSessionKey) {
        rememberRetryableAttempt(false)
        recordSessionNavigationDiag('send.error.stale', {
          requestSession: requestSessionKey,
          current: options.sessionKey.value,
          reason: errorMessage(err),
        })
        return acceptedError ? 'accepted' : 'retryable_failure'
      }
      if (!wasStreaming && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)) {
        rememberRetryableAttempt(false)
        return acceptedError ? 'accepted' : 'retryable_failure'
      }
      if (!wasStreaming) {
        if (activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
        }
        options.activeStreamTaskId.value = ''
        options.activeStreamSessionKey.value = ''
        options.stream.endStreaming()
      }
      rememberRetryableAttempt(true)
      options.messages.value.push({
        role: 'error',
        text: sendFailureMessage(err),
        errorCode: errorCode(err),
        ts: new Date().toISOString(),
      })
      return acceptedError ? 'accepted' : 'retryable_failure'
    } finally {
      finishResponseHandoff(responseHandoff)
    }
  }

  function restoreSendAttempt(
    attempt: SendAttempt,
    recovery: { requiresIdempotentReplay: boolean },
  ) {
    const currentText = options.inputText.value
    if (!currentText) {
      options.inputText.value = attempt.composerText
    } else if (
      !recovery.requiresIdempotentReplay
      && currentText !== attempt.composerText
    ) {
      options.inputText.value = [attempt.composerText, currentText].filter(Boolean).join('\n')
    }
    restoreSendableAttachments(attempt.attachments)
    if (!options.pendingSessionIntent.value) options.pendingSessionIntent.value = attempt.intent
    if (!options.pendingForkBeforeMessageId.value) {
      options.pendingForkBeforeMessageId.value = attempt.forkBeforeMessageId
    }
    if (options.pendingWorkspaceId && !options.pendingWorkspaceId.value) {
      options.pendingWorkspaceId.value = attempt.workspaceId
    }
    recoveredAttempt = {
      ...attempt,
      requiresIdempotentReplay: recovery.requiresIdempotentReplay,
    }
    options.autoResizeTextarea()
  }

  function restoreSendableAttachments(attachments: SendableAttachment[]) {
    if (attachments.length === 0) return
    const currentLocalIds = new Set(options.pendingAttachments.value.map(attachment => attachment.local_id))
    const missing = attachments.filter(attachment => !currentLocalIds.has(attachment.local_id))
    if (missing.length > 0) {
      options.pendingAttachments.value = [...missing, ...options.pendingAttachments.value]
    }
  }

  function onStop() {
    const handoffCanStop = responseHandoffBlocksCurrentSession()
    if (!(handoffCanStop || (options.canStop?.() ?? options.stream.isStreaming.value))) return
    options.aborted.value = true
    const handoff = handoffCanStop ? activeResponseHandoff : null
    if (handoff) handoff.stoppedByUser = true
    const abortSessionKey = handoff?.targetSessionKey
      || options.activeStreamSessionKey.value
      || options.sessionKey.value
    if (activeFreshSendToken !== null) activeFreshSendToken.stoppedByUser = true
    activeFreshSendToken = null
    const rawStoppedTurnId = currentExpectedTurnId()
    const stoppedTurnId = rawStoppedTurnId
      && ![
        PENDING_STREAM_TASK_ID,
        FINISHED_STREAM_TASK_ID,
        STOPPED_STREAM_TASK_ID,
      ].includes(rawStoppedTurnId)
      ? rawStoppedTurnId
      : ''
    const latestUserMessage = [...options.messages.value]
      .reverse()
      .find(message => message.role === 'user')
    const outcomeTurnId = stoppedTurnId
      || latestUserMessage?.turnId
      || latestUserMessage?.clientId
      || latestUserMessage?.messageId
      || ''
    const stoppedAt = Date.now()
    const stoppedOutcome = outcomeTurnId
      ? {
          turnId: outcomeTurnId,
          ...(stoppedTurnId ? { taskId: stoppedTurnId } : {}),
          status: 'cancelled',
          kind: 'cancelled',
          cancellationSource: 'webui_stop',
          finishedAt: stoppedAt,
        }
      : undefined
    for (const message of options.messages.value) {
      if (
        message.role === 'user'
        && message.inputDisposition === 'steering'
        && (
          (stoppedTurnId && message.turnId === stoppedTurnId)
          || (!stoppedTurnId && message === latestUserMessage)
        )
      ) {
        // Stop and steer share the server admission gate. Do not guess which
        // side won: an already-applied/promoted steer must not be restored and
        // sent twice. The authoritative disposition event performs any
        // cancelled-input restoration.
        message.steerStopRequested = true
      }
      if (
        stoppedOutcome
        && (
          (stoppedTurnId && message.turnId === stoppedTurnId)
          || (!stoppedTurnId && message === latestUserMessage)
        )
      ) {
        message.turnOutcome = stoppedOutcome
      }
    }
    options.activeStreamTaskId.value = STOPPED_STREAM_TASK_ID
    // Be honest if the abort can't reach the gateway (e.g. the socket dropped):
    // we still tear the local stream down for responsiveness, but the user must
    // know the server-side run may keep going rather than trust a false "stopped".
    const abortParams: Record<string, string> = { sessionKey: abortSessionKey, source: 'webui_stop' }
    options.rpc.call('chat.abort', abortParams)
      .then(() => options.scheduleHistorySync())
      .catch(() => {
        reportAbortFailure([abortSessionKey])
      })
    const messageCountBeforeStop = options.messages.value.length
    options.stream.endStreaming({ reason: 'aborted' })
    const stoppedAssistant = options.messages.value[messageCountBeforeStop]
    if (stoppedOutcome && stoppedAssistant?.role === 'assistant') {
      if (stoppedTurnId) stoppedAssistant.turnId = stoppedTurnId
      stoppedAssistant.turnOutcome = stoppedOutcome
    }
    options.popAllPendingIntoComposer()
  }

  /**
   * Hidden control send: dispatches chat.send with provider text that carries
   * the meta_preflight markers, optionally with a visible displayText bubble.
   * Unlike dispatchSend it does NOT push the provider text as a user bubble,
   * does NOT consume composer text/attachments/intent, and does NOT clear the
   * composer — the operator's draft is preserved. When the turn is streaming or
   * compaction is in flight, it is queued (carrying provider + display text and
   * a hiddenControl flag) so the drain restores both.
   */
  function hiddenDispatchResult(
    status: HiddenControlDispatchResult['status'],
    reason: HiddenControlDispatchResult['reason'],
    clientRequestId: string,
    sessionKey: string,
  ): HiddenControlDispatchResult {
    return { status, reason, clientRequestId, sessionKey }
  }

  function dispatchHiddenSend(
    providerText: string,
    displayText: string,
    clientRequestId?: string,
    targetSessionKey?: string,
  ): Promise<HiddenControlDispatchResult> {
    const requestSessionKey = String(targetSessionKey || options.sessionKey.value).trim()
    const stableClientRequestId = String(clientRequestId || '').trim() || createClientRequestId()
    if (!requestSessionKey || !providerText) {
      return Promise.resolve(hiddenDispatchResult(
        'rejected',
        'invalid_request',
        stableClientRequestId,
        requestSessionKey,
      ))
    }

    const hiddenDispatchKey = `${requestSessionKey}\u0000${stableClientRequestId}`
    const existing = hiddenDispatchInFlight.get(hiddenDispatchKey)
    if (existing) return existing

    // Persist before either local queueing or RPC. The payload contains only
    // the already-visible control turn (never provider credentials), while its
    // stable request id lets Gateway ingress collapse response-loss retries.
    const persistResult = persistHiddenControlResult({
      sessionKey: requestSessionKey,
      clientRequestId: stableClientRequestId,
      providerText,
      displayText,
    }, options.hiddenControlStorage)
    if (persistResult === 'conflict' || persistResult === 'failed' || persistResult === 'invalid') {
      return Promise.resolve(hiddenDispatchResult(
        'rejected',
        persistResult === 'conflict' ? 'outbox_conflict' : 'outbox_persist_failed',
        stableClientRequestId,
        requestSessionKey,
      ))
    }
    if (requestSessionKey !== options.sessionKey.value) {
      // A delayed meta.run response belongs to its originating chat. Persist
      // the exact staged control, but never mutate/send through whichever chat
      // is currently rendered. Returning to the origin calls
      // restoreHiddenControls and resumes with the same idempotency key.
      if (persistResult !== 'persisted' && persistResult !== 'matched') {
        return Promise.resolve(hiddenDispatchResult(
          'rejected',
          'outbox_persist_failed',
          stableClientRequestId,
          requestSessionKey,
        ))
      }
      return Promise.resolve(hiddenDispatchResult(
        'queued',
        'queued',
        stableClientRequestId,
        requestSessionKey,
      ))
    }

    const operation = performHiddenSend(
      providerText,
      displayText,
      stableClientRequestId,
      requestSessionKey,
    )
    hiddenDispatchInFlight.set(hiddenDispatchKey, operation)
    void operation.then(() => {
      if (hiddenDispatchInFlight.get(hiddenDispatchKey) === operation) {
        hiddenDispatchInFlight.delete(hiddenDispatchKey)
      }
    }, () => {
      if (hiddenDispatchInFlight.get(hiddenDispatchKey) === operation) {
        hiddenDispatchInFlight.delete(hiddenDispatchKey)
      }
    })
    return operation
  }

  async function performHiddenSend(
    providerText: string,
    displayText: string,
    stableClientRequestId: string,
    requestSessionKey: string,
  ): Promise<HiddenControlDispatchResult> {
    const compactInFlight = options.isCompactInFlightForCurrentSession()
    const handoffInFlight = responseHandoffBlocksCurrentSession()
    const projectBlocked = options.validateActiveProjectBeforeSend
      ? await refreshedActiveProjectBlocksSend()
      : false
    if (
      projectBlocked
      || options.sendBlockedReason?.value
      || options.stream.isStreaming.value
      || compactInFlight
      || handoffInFlight
    ) {
      const queuedItem = {
        text: providerText,
        displayText,
        clientRequestId: stableClientRequestId,
        sessionKey: requestSessionKey,
      }
      const owner = pendingQueueOwner()
      const queued = owner
        ? options.enqueueHiddenControl?.(queuedItem, owner)
        : options.enqueueHiddenControl?.(queuedItem)
      return hiddenDispatchResult(
        queued ? 'queued' : 'rejected',
        queued ? 'queued' : 'queue_full',
        stableClientRequestId,
        requestSessionKey,
      )
    }

    options.aborted.value = false
    recordSessionNavigationDiag('hiddenSend.start', {
      requestSession: requestSessionKey,
      current: requestSessionKey,
    })
    // Show the visible confirmation as a user bubble (NOT the marker text).
    const now = new Date().toISOString()
    const clientMessageId = `hidden-control:${stableClientRequestId}`
    const renderedKey = `${requestSessionKey}\u0000${stableClientRequestId}`
    if (displayText && !renderedHiddenControls.has(renderedKey)) {
      renderedHiddenControls.add(renderedKey)
      options.messages.value.push({
        role: 'user',
        text: displayText,
        ts: now,
        clientId: clientMessageId,
      })
      options.autoScroll.value = true
      options.scrollToBottom()
    }

    const params: ChatSendParams = {
      clientRequestId: stableClientRequestId,
      clientMessageId,
      message: providerText,
      sessionKey: requestSessionKey,
    }
    const hiddenSessionIntent = requestSessionKey === options.sessionKey.value
      ? options.pendingSessionIntent.value
      : null
    if (hiddenSessionIntent) params.intent = hiddenSessionIntent
    if (displayText && displayText !== providerText) params.displayText = displayText
    params._source = chatSourceMetadata(options)

    const wasStreaming = options.stream.isStreaming.value
    const freshSendToken = wasStreaming
      ? null
      : beginFreshStream(requestSessionKey)
    let responseHandoff: ResponseHandoffGate | null = null

    try {
      const res = await options.rpc.call<ChatSendResponse>('chat.send', params)
      if (
        hiddenSessionIntent
        && requestSessionKey === options.sessionKey.value
        && options.pendingSessionIntent.value === hiddenSessionIntent
      ) {
        options.pendingSessionIntent.value = null
      }
      // A resolved chat.send response proves durable ingress acceptance. Clear
      // the browser outbox before any local session handoff work, which can
      // fail independently without making an exact-id resend necessary.
      removeHiddenControl(
        requestSessionKey,
        stableClientRequestId,
        options.hiddenControlStorage,
      )
      const taskId = acceptedTaskId(res)
      const terminalStatus = terminalResponseStatus(res)
      const stoppedByUser = freshSendToken?.stoppedByUser === true
      const lostFreshStream = !wasStreaming
        && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)
      if (stoppedByUser || lostFreshStream) {
        const acceptedSessionKey = res?.sessionKey || requestSessionKey
        if (
          options.sessionKey.value === requestSessionKey
          && acceptedSessionKey === requestSessionKey
        ) {
          bindAcceptedUserMessage(clientMessageId, res)
        }
        abortStaleAcceptedTask(res, requestSessionKey, stoppedByUser)
        if (
          stoppedByUser
          && options.sessionKey.value === requestSessionKey
          && acceptedSessionKey !== requestSessionKey
        ) {
          responseHandoff = beginResponseHandoff(requestSessionKey, params.clientRequestId!)
          responseHandoff.stoppedByUser = true
          responseHandoff.acceptedTaskId = taskId
          responseHandoff.terminalResponse = Boolean(terminalStatus)
          await handoffResponseSession(acceptedSessionKey, responseHandoff)
        }
        return hiddenDispatchResult(
          'accepted',
          'accepted',
          stableClientRequestId,
          requestSessionKey,
        )
      }
      if ((res?.sessionKey || requestSessionKey) === requestSessionKey) {
        bindAcceptedUserMessage(clientMessageId, res)
      }
      // Bind the live stream to this turn's task so a prior task's late events
      // can't bleed into it (issue #344). Only a fresh turn takes over rendering
      // — a steer/queue send rides the in-flight stream and must not rebind —
      // and only while this session is still the one on screen.
      const responseIsCurrent = options.sessionKey.value === requestSessionKey
      if (!terminalStatus && !wasStreaming && responseIsCurrent) {
        options.activeStreamSessionKey.value = res?.sessionKey || requestSessionKey
        if (taskId) bindAcceptedTask(taskId)
      }
      const decision = decideSendResponseSession({
        requestSessionKey,
        currentSessionKey: options.sessionKey.value,
        responseSessionKey: res?.sessionKey,
      })
      const terminalSessionKey = decision.action === 'persist'
        ? decision.responseSessionKey
        : requestSessionKey
      if (decision.action === 'persist') {
        recordSessionNavigationDiag('hiddenSend.response.persist', {
          requestSession: requestSessionKey,
          responseSession: decision.responseSessionKey,
          current: options.sessionKey.value,
        })
        responseHandoff = beginResponseHandoff(requestSessionKey, params.clientRequestId!)
        responseHandoff.acceptedTaskId = taskId
        responseHandoff.terminalResponse = Boolean(terminalStatus)
        await handoffResponseSession(decision.responseSessionKey, responseHandoff)
      } else if (decision.reason === 'current_session_changed') {
        recordSessionNavigationDiag('hiddenSend.response.stale', {
          requestSession: requestSessionKey,
          responseSession: res?.sessionKey,
          current: options.sessionKey.value,
          reason: decision.reason,
        })
      }
      if (
        terminalStatus
        && responseIsCurrent
        && options.sessionKey.value === terminalSessionKey
      ) {
        handleTerminalResponse(res, freshSendToken, { finishFreshStream: !wasStreaming })
        // See dispatchSend: a terminal response has no future lifecycle event.
      }
      return hiddenDispatchResult(
        'accepted',
        'accepted',
        stableClientRequestId,
        requestSessionKey,
      )
    } catch (err: unknown) {
      const rpcError = err as RpcClientError | null | undefined
      const acceptedError = acceptedErrorInfo(err)
      const accepted = rpcError?.accepted
      if (accepted === true) {
        if (
          hiddenSessionIntent
          && requestSessionKey === options.sessionKey.value
          && options.pendingSessionIntent.value === hiddenSessionIntent
        ) {
          options.pendingSessionIntent.value = null
        }
        removeHiddenControl(
          requestSessionKey,
          stableClientRequestId,
          options.hiddenControlStorage,
        )
      }
      const acceptedSessionKey = acceptedError?.sessionKey || requestSessionKey
      const stoppedByUser = freshSendToken?.stoppedByUser === true
      if (
        acceptedError
        && stoppedByUser
        && !acceptedError.terminalWithoutTask
        && acceptedSessionKey !== requestSessionKey
      ) {
        abortStaleAcceptedTask(
          { sessionKey: acceptedSessionKey },
          requestSessionKey,
          true,
        )
      }
      if (
        acceptedError
        && options.sessionKey.value === requestSessionKey
        && acceptedSessionKey !== requestSessionKey
      ) {
        if (!wasStreaming && activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
          options.activeStreamTaskId.value = ''
          options.activeStreamSessionKey.value = ''
          options.stream.endStreaming()
        }
        responseHandoff = beginResponseHandoff(requestSessionKey, params.clientRequestId!)
        responseHandoff.stoppedByUser = stoppedByUser
        responseHandoff.terminalResponse = acceptedError.terminalWithoutTask
        await handoffResponseSession(acceptedSessionKey, responseHandoff)
        options.scheduleHistorySync()
        if (acceptedError.terminalWithoutTask && !stoppedByUser) {
          options.schedulePendingDrainAfterTerminal()
        }
        options.messages.value.push({
          role: 'error',
          text: sendFailureMessage(err),
          errorCode: errorCode(err),
          ts: new Date().toISOString(),
        })
        return hiddenDispatchResult(
          'accepted',
          'accepted',
          stableClientRequestId,
          requestSessionKey,
        )
      }
      if (acceptedError && options.sessionKey.value === requestSessionKey) {
        bindUserMessageId(clientMessageId, acceptedError.messageId)
        options.scheduleHistorySync()
        return hiddenDispatchResult(
          'accepted',
          'accepted',
          stableClientRequestId,
          requestSessionKey,
        )
      }
      if (acceptedError) {
        return hiddenDispatchResult(
          'accepted',
          'accepted',
          stableClientRequestId,
          requestSessionKey,
        )
      }
      if (accepted === false && rpcError?.retryable === false) {
        removeHiddenControl(
          requestSessionKey,
          stableClientRequestId,
          options.hiddenControlStorage,
        )
      }
      if (options.sessionKey.value !== requestSessionKey) {
        recordSessionNavigationDiag('hiddenSend.error.stale', {
          requestSession: requestSessionKey,
          current: options.sessionKey.value,
          reason: errorMessage(err),
        })
        return hiddenDispatchResult(
          accepted === false ? 'rejected' : 'unknown',
          accepted === false ? 'send_rejected' : 'response_unknown',
          stableClientRequestId,
          requestSessionKey,
        )
      }
      if (!wasStreaming && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)) {
        return hiddenDispatchResult(
          accepted === false ? 'rejected' : 'unknown',
          accepted === false ? 'send_rejected' : 'response_unknown',
          stableClientRequestId,
          requestSessionKey,
        )
      }
      if (!wasStreaming) {
        if (activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
        }
        options.activeStreamTaskId.value = ''
        options.activeStreamSessionKey.value = ''
        options.stream.endStreaming()
      }
      options.messages.value.push({
        role: 'error',
        text: sendFailureMessage(err),
        errorCode: errorCode(err),
        ts: new Date().toISOString(),
      })
      return hiddenDispatchResult(
        accepted === false ? 'rejected' : 'unknown',
        accepted === false ? 'send_rejected' : 'response_unknown',
        stableClientRequestId,
        requestSessionKey,
      )
    } finally {
      finishResponseHandoff(responseHandoff)
    }
    return hiddenDispatchResult('accepted', 'accepted', stableClientRequestId, requestSessionKey)
  }

  async function dispatchQueuedHiddenSend(
    item: ChatPendingItem,
    ownerSessionKey: string,
  ): Promise<ChatSendOutcome> {
    const stableClientRequestId = item.clientRequestId
      || item.hiddenClientRequestId
      || createClientRequestId()
    item.clientRequestId = stableClientRequestId
    item.hiddenClientRequestId = stableClientRequestId
    item.hiddenClientMessageId ||= `hidden-control:${stableClientRequestId}`
    const result = await dispatchHiddenSend(
      item.text,
      item.displayTextOverride || '',
      stableClientRequestId,
      ownerSessionKey,
    )
    if (item.displayTextOverride) item.hiddenVisibleCommitted = true
    if (result.status === 'accepted') return 'accepted'
    if (result.status === 'queued') return 'deferred'
    if (result.status === 'unknown') return 'retryable_failure'
    return 'not_sent'
  }

  async function restoreHiddenControls(
    targetSessionKey = options.sessionKey.value,
    skipClientRequestIds: readonly string[] = [],
    isCurrent: () => boolean = () => true,
  ): Promise<void> {
    if (!targetSessionKey || !isCurrent() || options.sessionKey.value !== targetSessionKey) return
    const skipped = new Set(skipClientRequestIds)
    for (const item of listHiddenControls(targetSessionKey, options.hiddenControlStorage)) {
      if (!isCurrent() || options.sessionKey.value !== targetSessionKey) return
      if (skipped.has(item.clientRequestId)) continue
      const result = await dispatchHiddenSend(
        item.providerText,
        item.displayText,
        item.clientRequestId,
      )
      if (!isCurrent()) return
      // One queued item owns the next drain slot. Continuing would only fill a
      // bounded in-memory queue during a long active turn; the remaining
      // durable outbox entries will be retried on the next restore/reconnect.
      if (result.status === 'queued') return
    }
  }

  async function retryPendingMetaDiscard(
    sessionKey: string,
    clientRequestId: string,
  ): Promise<boolean> {
    try {
      const result = await options.rpc.call<{ discarded?: boolean; accepted?: boolean }>('meta.drafts.discard', {
        sessionKey,
        clientRequestId,
      })
      if (result?.discarded !== true && result?.accepted !== true) return false
      removePendingMetaDiscard(sessionKey, clientRequestId, metaDiscardStorage())
      return true
    } catch {
      return false
    }
  }

  function discardHiddenControl(sessionKey: string, clientRequestId: string): boolean {
    // Persist the user's cancellation before removing the sendable browser
    // copy. If the RPC or its response is lost, reload retries only this
    // discard and never treats the server draft as launchable work.
    if (!persistPendingMetaDiscard({ sessionKey, clientRequestId }, metaDiscardStorage())) {
      return false
    }
    forgetHiddenControl(sessionKey, clientRequestId)
    void retryPendingMetaDiscard(sessionKey, clientRequestId)
    return true
  }

  function forgetHiddenControl(sessionKey: string, clientRequestId: string): void {
    removeHiddenControl(sessionKey, clientRequestId, options.hiddenControlStorage)
  }

  async function flushPendingMetaDiscards(
    sessionKey?: string,
    skipClientRequestIds: readonly string[] = [],
  ): Promise<string[]> {
    const remaining: string[] = []
    const skipped = new Set(skipClientRequestIds)
    for (const pending of listPendingMetaDiscards(sessionKey, metaDiscardStorage())) {
      if (skipped.has(pending.clientRequestId)) {
        remaining.push(pending.clientRequestId)
        continue
      }
      const discarded = await retryPendingMetaDiscard(
        pending.sessionKey,
        pending.clientRequestId,
      )
      if (!discarded) remaining.push(pending.clientRequestId)
    }
    return remaining
  }

  /**
   * Build and dispatch the hidden meta-preflight confirmation. The
   * server-authored confirmed.message is preferred (it carries the base64url
   * meta_preflight_fields marker); the JS fallback embeds the two required
   * HTML-comment markers keyed by the Python preflight protocol parser.
   */
  function sendHiddenMetaPreflightConfirmation(
    confirmed: { message?: string } | null,
    detail: { runId: string; metaSkillName: string; interpretedRequest: string; language: string },
  ) {
    const interpreted = (detail.interpretedRequest || '').trim()
    const fallback =
      `${interpreted}\n\n<!-- opensquilla:meta_preflight_confirmed=1 -->` +
      (detail.runId ? `\n<!-- opensquilla:meta_preflight_run_id=${detail.runId} -->` : '')
    const providerText = confirmed?.message || fallback
    const zhFallback = detail.language === 'zh' ? '已确认，开始运行。' : 'Confirmed — starting the run.'
    const visibleText = interpreted || zhFallback
    void dispatchHiddenSend(providerText, visibleText)
  }

  return {
    onSend,
    onStop,
    sendQueuedSteer,
    sendQueuedFollowup,
    supportsSameTurnSteer,
    dispatchComposerPrompt,
    dispatchHiddenSend,
    dispatchQueuedHiddenSend,
    discardHiddenControl,
    forgetHiddenControl,
    flushPendingMetaDiscards,
    restoreHiddenControls,
    sendHiddenMetaPreflightConfirmation,
  }
}
