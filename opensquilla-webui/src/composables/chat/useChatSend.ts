import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'
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
import type {
  GatewayModelRoutingMode,
  ImageInputAdmission,
  ModelRoutingMode,
} from '@/types/modelRouting'
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
import type { ChatTaskOwnershipApi } from '@/composables/chat/useChatTaskOwnership'
import type {
  BusySendMode,
  PendingCancelOptions,
  PendingQueueOwner,
  PendingQueueOwnerContext,
  PendingSteerPayload,
} from '@/composables/chat/useChatPendingQueue'
import type { ChatSteerDeliveryApi } from '@/composables/chat/useChatSteerDelivery'
import type { SlashCommandClassification } from '@/composables/chat/useChatSlashCommands'
import { recordSessionNavigationDiag } from '@/utils/chat/sessionNavigationDiag'
import { canonicalSessionKey } from '@/utils/chat/sessionKeys'
import {
  hasModelInputImageAttachment,
  hasSendableModelInputImageAttachment,
  isSendableAttachment,
  serializeDisplayAttachment,
  serializeSendableAttachment,
  type SendableAttachment,
} from '@/utils/chat/attachments'
import { localizedChatErrorMessage } from '@/utils/chat/errors'
import { isControlInput } from '@/utils/chat/inputSemantics'
import {
  createClientMessageId,
  createClientRequestId,
  stableClientUuid,
} from '@/utils/chat/messageIdentity'
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
import type {
  PendingInputWal,
  ResponseHandoffWalRecord,
} from '@/utils/chat/pendingInputWal'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
  STOPPED_STREAM_TASK_ID,
  taskTerminalMessage,
} from '@/utils/chat/streamEvents'

type RpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
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
  initialRoutingMode: GatewayModelRoutingMode | null
  forkBeforeMessageId: string | null
  workspaceId: string | null
  restoreComposerOnHandoffFailure?: boolean
  handoffWalOwnerId?: string
  handoffWalRevision?: number
  replayCoordinationKey?: string
  params: ChatSendParams
  requiresIdempotentReplay?: boolean
  // A Stop issued before durable acceptance is known belongs to this exact
  // idempotent request, not to whichever session happens to be visible later.
  stopRequested?: boolean
  acceptanceRpc?: {
    method: 'chat.send' | 'sessions.pending_inputs.dispatch'
    params: Record<string, unknown>
  }
  acceptanceResolved?: boolean
  acceptanceInFlight?: boolean
  acceptedTaskId?: string
  acceptedSessionKey?: string
  stopAbortPromise?: Promise<boolean> | null
  autoRecoverAcceptance?: boolean
  hiddenControl?: boolean
  stopOwner?: symbol
}

export type ChatSendOutcome = 'accepted' | 'deferred' | 'not_sent' | 'retryable_failure'

interface ExplicitSendPayload {
  attachments: Attachment[]
  intent: string | null
  forkBeforeMessageId: string | null
  workspaceId?: string | null
  initialCollaborationMode?: CollaborationMode | null
  initialRoutingMode?: GatewayModelRoutingMode | null
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
  initialRoutingMode: GatewayModelRoutingMode | null
  queueOwnerRequestId: string | null
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
  durablePendingItem?: ChatPendingItem
  /** Delay branch truncation and optimistic rendering until ingress accepts. */
  acceptedVisibleReplay?: { forkBeforeMessageId: string }
  /** Protocol replays keep a rejected attempt on their own surface. */
  suppressRejectedFailureMessage?: boolean
  /** Preserve an explicit empty attachment list on the chat.send wire. */
  includeEmptyAttachments?: boolean
  /** Revalidate protocol-owned sends after every awaited pre-dispatch step. */
  preDispatchGuard?: (stage: 'preflight' | 'before_rpc') => boolean
  /** Require a non-replayable WAL preparation that is armed immediately before RPC. */
  requirePreparedHandoff?: boolean
  /** Stable cross-tab identity for one protocol-owned replay. */
  replayCoordination?: {
    key: string
    clientRequestId: string
    clientMessageId: string
  }
}

export interface UsageBarrierReplayPayload {
  text: string
  forkBeforeMessageId: string
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
  durableRecord: ResponseHandoffWalRecord | null
}

interface FreshSendToken {
  stoppedByUser: boolean
}

interface AcceptanceTransaction {
  id: symbol
  requestSessionKey: string
  stoppedByUser: boolean
  freshSendToken: FreshSendToken | null
  attempt: SendAttempt | null
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
    initialRoutingMode: GatewayModelRoutingMode | null
    forkBeforeMessageId: string | null
    workspaceId: string | null
  },
): boolean {
  return (
    attempt.requestSessionKey === input.requestSessionKey &&
    attempt.text === input.text &&
    attempt.intent === input.intent &&
    attempt.initialCollaborationMode === input.initialCollaborationMode &&
    attempt.initialRoutingMode === input.initialRoutingMode &&
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
  hasPendingQueueWork?: () => boolean
  pendingInputWal?: PendingInputWal | null
  busySendMode: Ref<BusySendMode>
  modelRoutingMode: Readonly<Ref<ModelRoutingMode>>
  modelRoutingSettingsBusy: Readonly<Ref<boolean>>
  imageInputAdmission?: Readonly<Ref<ImageInputAdmission>>
  elevatedMode: Ref<string>
  runMode: Ref<SandboxRunMode>
  pendingAttachments: Ref<Attachment[]>
  composerRevision?: Readonly<Ref<number>>
  pendingSessionIntent: Ref<string | null>
  initialCollaborationMode: Readonly<Ref<CollaborationMode>>
  initialRoutingMode: Readonly<Ref<GatewayModelRoutingMode | null>>
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
  taskOwnership?: ChatTaskOwnershipApi
  acceptanceStopPending?: Ref<boolean>
  acceptanceRecoveryPending?: Ref<boolean>
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
  recoverPendingQueueHandoff?: (
    sourceSessionKey: string,
    targetSessionKey: string,
    ownerRequestId: string,
  ) => Promise<void>
  failPendingQueueHandoff?: (ownerRequestId: string) => Promise<void> | void
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
  enqueuePendingInput: (
    text: string,
    owner?: PendingQueueOwner,
    enqueueOptions?: { confirmedPlainText?: boolean },
  ) => boolean | Promise<boolean>
  enqueuePendingPayload?: (
    payload: {
      text: string
      attachments?: Attachment[]
      intent?: string | null
      confirmedPlainText?: boolean
    },
    owner?: PendingQueueOwner,
  ) => boolean | Promise<boolean>
  cancelDurablePendingItem?: (
    item: ChatPendingItem,
    options?: PendingCancelOptions,
  ) => Promise<boolean>
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
  enqueuePendingSteerAttempt?: (
    payload: PendingSteerPayload,
    owner?: PendingQueueOwner,
  ) => ChatPendingItem | null
  steerDelivery: ChatSteerDeliveryApi
  restoreSteerIntoComposer?: (text: string) => void
  popAllPendingIntoComposer: () => boolean
  reconcileTaskOwnership?: () => void | Promise<unknown>
  hiddenControlStorage?: HiddenControlStorage | null
  metaDiscardStorage?: MetaDiscardStorage | null
  classifySlashCommand: (text: string) => Promise<SlashCommandClassification>
  executeSlashCommand: (
    text: string,
    knownClassification?: SlashCommandClassification,
  ) => Promise<boolean>
  closeSlashMenu: () => void
  autoResizeTextarea: () => void
  scrollToBottom: () => void
}

export function useChatSend(options: UseChatSendOptions) {
  const { pushToast } = useToasts()
  const acceptanceStopPending = options.acceptanceStopPending || ref(false)
  let activeFreshSendToken: FreshSendToken | null = null
  let activeAcceptanceTransaction: AcceptanceTransaction | null = null
  let acceptanceStopOwner: symbol | null = null
  let activeResponseHandoff: ResponseHandoffGate | null = null
  let activeProjectPreflightToken: symbol | null = null
  let recoveredAttempt: SendAttempt | null = null
  let usageBarrierReplayAttempt: SendAttempt | null = null
  let usageBarrierReplayInFlight = false
  let handoffRecoveryPromise: Promise<void> | null = null
  const acceptanceRecoveryWorkers = new Map<string, Promise<void>>()
  const stoppedAcceptanceAttempts = new Map<string, SendAttempt>()
  const hiddenDispatchInFlight = new Map<string, Promise<HiddenControlDispatchResult>>()
  const renderedHiddenControls = new Set<string>()
  const acceptanceRecoveryVersion = ref(0)

  function noteAcceptanceRecoveryChanged() {
    acceptanceRecoveryVersion.value += 1
  }

  const acceptanceRecoveryPendingForCurrentSession: ComputedRef<boolean> = computed(() => {
    // Depend on an explicit version because the attempt registry is purposely
    // non-reactive and must remain request-owned across route switches.
    acceptanceRecoveryVersion.value
    const key = options.sessionKey.value
    if (!key) return false
    for (const attempt of stoppedAcceptanceAttempts.values()) {
      if (attempt.requestSessionKey === key && attempt.stopRequested) return true
    }
    for (const workerKey of acceptanceRecoveryWorkers.keys()) {
      if (workerKey.startsWith(`${key}\u0000`)) return true
    }
    return false
  })
  watch(acceptanceRecoveryPendingForCurrentSession, (pending) => {
    if (options.acceptanceRecoveryPending) {
      options.acceptanceRecoveryPending.value = pending
    }
  }, { immediate: true })

  function metaDiscardStorage(): MetaDiscardStorage | null | undefined {
    return options.metaDiscardStorage
  }

  const recoveredQueuedAttempts = new WeakMap<ChatPendingItem, SendAttempt>()

  function pendingWorkspaceForIntent(intent: string | null): string | null {
    return intent === 'new_chat'
      ? options.pendingWorkspaceId?.value || null
      : null
  }

  function captureComposerSnapshot(): ComposerSnapshot {
    const intent = options.pendingSessionIntent.value
    const attachmentRefs = [...options.pendingAttachments.value]
    const queueOwnerContext = options.pendingQueueOwnerContext.value
    return {
      revision: options.composerRevision?.value ?? null,
      inputText: options.inputText.value,
      attachmentRefs,
      payloadAttachments: attachmentRefs.map(attachment => ({ ...attachment })),
      intent,
      forkBeforeMessageId: options.pendingForkBeforeMessageId.value,
      workspaceId: pendingWorkspaceForIntent(intent),
      initialCollaborationMode: initialModeForIntent(intent),
      initialRoutingMode: initialRoutingModeForIntent(intent),
      queueOwnerRequestId: queueOwnerContext?.sessionKey === options.sessionKey.value
        ? queueOwnerContext.ownerRequestId
        : null,
    }
  }

  function queueOwnerMatchesSnapshot(snapshot: ComposerSnapshot): boolean {
    const context = options.pendingQueueOwnerContext.value
    const currentOwnerRequestId = context?.sessionKey === options.sessionKey.value
      ? context.ownerRequestId
      : null
    return currentOwnerRequestId === snapshot.queueOwnerRequestId
  }

  function queueOwnerFromSnapshot(snapshot: ComposerSnapshot): PendingQueueOwner | undefined {
    return snapshot.queueOwnerRequestId
      ? { ownerRequestId: snapshot.queueOwnerRequestId }
      : undefined
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
      initialRoutingMode: snapshot.initialRoutingMode,
    }
  }

  function modelImageSendBlocked(attachments: readonly Attachment[]): boolean {
    if (!hasModelInputImageAttachment(attachments)) return false
    return options.modelRoutingSettingsBusy.value
      || options.imageInputAdmission?.value === 'blocked'
      || (
        options.imageInputAdmission === undefined
        && options.modelRoutingMode.value === 'llm_ensemble'
      )
  }

  function activeSteerCapability(): ChatSteerCapability | null {
    return options.activeSteerCapability?.value || null
  }

  function capabilityExpectedTurnId(): string {
    return String(activeSteerCapability()?.expected_turn_id || '').trim()
  }

  function currentExpectedTurnId(): string {
    return String(
      options.taskOwnership?.runningTaskId.value
      || capabilityExpectedTurnId()
      || options.activeStreamTaskId.value,
    ).trim()
  }

  function taskAcceptanceStatus(response: ChatSendResponse | null | undefined): string {
    return String(response?.task_status || response?.taskStatus || '').trim().toLowerCase()
  }

  function hasAuthoritativeWork(): boolean {
    return options.taskOwnership?.hasAuthoritativeWork.value === true
      || acceptanceStopPending.value
      || acceptanceRecoveryPendingForCurrentSession.value
  }

  function noteAcceptedTask(
    response: ChatSendResponse | null | undefined,
    requestSessionKey: string,
  ): {
    taskId: string
    claimRender: boolean
    renderTaskId: string
  } {
    const taskId = acceptedTaskId(response)
    if (!taskId || !options.taskOwnership) {
      return { taskId, claimRender: Boolean(taskId), renderTaskId: taskId }
    }
    const acceptedSessionKey = response?.sessionKey || requestSessionKey
    if (
      options.sessionKey.value !== requestSessionKey
      && options.sessionKey.value !== acceptedSessionKey
    ) {
      return { taskId, claimRender: false, renderTaskId: '' }
    }
    const ownership = options.taskOwnership.noteAccepted(taskId, taskAcceptanceStatus(response))
    return {
      taskId,
      claimRender: ownership.claimRender,
      renderTaskId: ownership.renderTaskId,
    }
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

  function acceptanceAttemptKey(attempt: Pick<SendAttempt, 'requestSessionKey' | 'clientRequestId'>) {
    return `${attempt.requestSessionKey}\u0000${attempt.clientRequestId}`
  }

  function beginFreshStream(
    requestSessionKey: string,
    attempt: SendAttempt | null = null,
  ): FreshSendToken {
    // An unknown-acceptance retry keeps the original Stop intent. The stable
    // request id will hit the ingress receipt; once its task id is known the
    // normal stopped-response path issues the exact scoped abort.
    const token: FreshSendToken = {
      stoppedByUser: attempt?.stopRequested === true || acceptanceStopPending.value,
    }
    activeFreshSendToken = token
    options.activeStreamTaskId.value = PENDING_STREAM_TASK_ID
    options.activeStreamSessionKey.value = requestSessionKey
    options.stream.startStreaming()
    options.stream.showThinkingIndicator()
    return token
  }

  function beginAcceptanceTransaction(
    requestSessionKey: string,
    freshSendToken: FreshSendToken | null,
    attempt: SendAttempt | null = null,
  ): AcceptanceTransaction {
    const transaction = {
      id: Symbol('chat-acceptance'),
      requestSessionKey,
      stoppedByUser: attempt?.stopRequested === true || acceptanceStopPending.value,
      freshSendToken,
      attempt,
    }
    if (transaction.stoppedByUser) {
      acceptanceStopOwner = transaction.id
      acceptanceStopPending.value = true
      if (attempt?.stopRequested) {
        attempt.stopOwner = transaction.id
        stoppedAcceptanceAttempts.set(acceptanceAttemptKey(attempt), attempt)
      }
      if (freshSendToken) freshSendToken.stoppedByUser = true
      noteAcceptanceRecoveryChanged()
    }
    activeAcceptanceTransaction = transaction
    return transaction
  }

  function finishAcceptanceTransaction(transaction: AcceptanceTransaction) {
    if (activeAcceptanceTransaction === transaction) activeAcceptanceTransaction = null
  }

  function clearAcceptanceStop(transaction: AcceptanceTransaction | null) {
    if (!transaction || acceptanceStopOwner !== transaction.id) return
    acceptanceStopOwner = null
    acceptanceStopPending.value = false
  }

  function clearAttemptStop(attempt: SendAttempt) {
    attempt.stopRequested = false
    stoppedAcceptanceAttempts.delete(acceptanceAttemptKey(attempt))
    noteAcceptanceRecoveryChanged()
    // A background recovery must not clear a newer visible request's Stop.
    if (
      activeAcceptanceTransaction?.attempt === attempt
      || (attempt.stopOwner != null && acceptanceStopOwner === attempt.stopOwner)
      || (
        options.sessionKey.value === attempt.requestSessionKey
        && recoveredAttempt?.clientRequestId === attempt.clientRequestId
      )
    ) {
      acceptanceStopOwner = null
      acceptanceStopPending.value = false
    }
    attempt.stopOwner = undefined
  }

  const acceptanceRecoveryDelaysMs = [250, 1_000, 4_000, 15_000] as const

  async function abortRecoveredAcceptedTask(attempt: SendAttempt): Promise<boolean> {
    const taskId = attempt.acceptedTaskId || ''
    if (!attempt.stopRequested || !taskId) return !attempt.stopRequested
    if (attempt.stopAbortPromise) return attempt.stopAbortPromise
    const operation = (async () => {
      const isCurrentRequest = options.sessionKey.value === attempt.requestSessionKey
      if (isCurrentRequest) options.taskOwnership?.requestStop(taskId)
      try {
        const abort = await options.rpc.call<{ aborted?: boolean, reason?: string }>('chat.abort', {
          sessionKey: attempt.acceptedSessionKey || attempt.requestSessionKey,
          taskId,
          source: 'webui_stop',
          scope: 'task',
        })
        if (abort?.aborted !== true) {
          if (isCurrentRequest) {
            await options.reconcileTaskOwnership?.()
            options.scheduleHistorySync()
          }
          // An exact task_not_active answer proves there is nothing left for
          // this Stop worker to cancel. Reconcile/history owns the real
          // terminal disposition; do not synthesize a local cancellation or
          // retry forever.
          if (['task_not_active', 'task_mismatch'].includes(
            String(abort?.reason || '').toLowerCase(),
          )) {
            clearAttemptStop(attempt)
            if (recoveredAttempt?.clientRequestId === attempt.clientRequestId) {
              recoveredAttempt = null
            }
            return true
          }
          return false
        }
        clearAttemptStop(attempt)
        if (recoveredAttempt?.clientRequestId === attempt.clientRequestId) {
          recoveredAttempt = null
        }
        if (isCurrentRequest) options.scheduleHistorySync()
        return true
      } catch {
        if (isCurrentRequest) await options.reconcileTaskOwnership?.()
        return false
      }
    })().finally(() => {
      if (attempt.stopAbortPromise === operation) attempt.stopAbortPromise = null
    })
    attempt.stopAbortPromise = operation
    return operation
  }

  async function settleRecoveredAcceptance(
    attempt: SendAttempt,
    response: ChatSendResponse,
  ): Promise<boolean> {
    attempt.acceptanceResolved = true
    attempt.acceptedTaskId = acceptedTaskId(response)
    attempt.acceptedSessionKey = response.sessionKey || attempt.requestSessionKey
    const ownsRecoveredAttempt = recoveredAttempt?.clientRequestId === attempt.clientRequestId
    if (attempt.hiddenControl) {
      removeHiddenControl(
        attempt.requestSessionKey,
        attempt.clientRequestId,
        options.hiddenControlStorage,
      )
    }

    const isCurrentRequest = options.sessionKey.value === attempt.requestSessionKey
    const accepted = noteAcceptedTask(response, attempt.requestSessionKey)
    const terminalStatus = terminalResponseStatus(response)
    if (isCurrentRequest) {
      consumeAcceptedSessionIntent(attempt)
      bindAcceptedUserMessage(attempt.clientMessageId, response)
      options.scheduleHistorySync()
    }

    if (!attempt.stopRequested) {
      if (ownsRecoveredAttempt) recoveredAttempt = null
      return true
    }
    if (terminalStatus) {
      if (isCurrentRequest) {
        handleTerminalResponse(response, null, { finishFreshStream: false })
      }
      clearAttemptStop(attempt)
      if (ownsRecoveredAttempt) recoveredAttempt = null
      return true
    }

    const taskId = accepted.taskId
    if (!taskId) {
      // A response without a task identity is not enough to widen Stop to the
      // session. Keep replaying the same receipt until the task is identified
      // or the bounded worker yields to later reconnect recovery.
      attempt.acceptanceResolved = false
      return false
    }
    return abortRecoveredAcceptedTask(attempt)
  }

  function scheduleAcceptanceRecovery(attempt: SendAttempt) {
    if ((attempt.acceptanceResolved && !attempt.stopRequested) || !attempt.acceptanceRpc) return
    const key = acceptanceAttemptKey(attempt)
    if (acceptanceRecoveryWorkers.has(key)) return

    const operation = (async () => {
      let recoveryAttempt = 0
      while (!attempt.acceptanceResolved || attempt.stopRequested) {
        const delayMs = acceptanceRecoveryDelaysMs[
          Math.min(recoveryAttempt, acceptanceRecoveryDelaysMs.length - 1)
        ]!
        recoveryAttempt += 1
        await new Promise<void>(resolve => globalThis.setTimeout(resolve, delayMs))
        if (attempt.acceptanceResolved) {
          if (await abortRecoveredAcceptedTask(attempt)) return
          continue
        }
        if (attempt.acceptanceInFlight) continue
        attempt.acceptanceInFlight = true
        try {
          const response = await options.rpc.call<ChatSendResponse>(
            attempt.acceptanceRpc!.method,
            attempt.acceptanceRpc!.params,
          )
          if (await settleRecoveredAcceptance(attempt, response)) return
        } catch (error: unknown) {
          const rpcError = error as RpcClientError | null | undefined
          const accepted = acceptedErrorInfo(error)
          if (rpcError?.accepted === false || accepted?.terminalWithoutTask) {
            attempt.acceptanceResolved = true
            if (attempt.stopRequested) clearAttemptStop(attempt)
            if (
              attempt.hiddenControl
              && (
                accepted?.terminalWithoutTask
                || (rpcError?.accepted === false && rpcError.retryable === false)
              )
            ) {
              removeHiddenControl(
                attempt.requestSessionKey,
                attempt.clientRequestId,
                options.hiddenControlStorage,
              )
            }
            if (recoveredAttempt?.clientRequestId === attempt.clientRequestId) {
              recoveredAttempt = null
            }
            return
          }
          // Unknown acceptance stays attached to this exact request. A
          // reconnect/hydrate may improve the projection while the next
          // bounded idempotent receipt replay is waiting.
          if (options.sessionKey.value === attempt.requestSessionKey) {
            void options.reconcileTaskOwnership?.()
          }
        } finally {
          attempt.acceptanceInFlight = false
        }
      }
    })().finally(() => {
      if (acceptanceRecoveryWorkers.get(key) === operation) {
        acceptanceRecoveryWorkers.delete(key)
        noteAcceptanceRecoveryChanged()
      }
    })
    acceptanceRecoveryWorkers.set(key, operation)
    noteAcceptanceRecoveryChanged()
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

  function initialRoutingModeForIntent(intent: string | null): GatewayModelRoutingMode | null {
    return intent === 'new_chat' ? options.initialRoutingMode.value : null
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
    durableRecord: ResponseHandoffWalRecord | null = null,
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
      durableRecord,
    }
    activeResponseHandoff = gate
    if (durableRecord) {
      options.pendingQueueOwnerContext.value = { sessionKey: requestSessionKey, ownerRequestId }
    }
    return gate
  }

  async function persistResponseHandoff(
    attempt: SendAttempt,
    requirePrepared = false,
  ): Promise<ResponseHandoffWalRecord | null> {
    const wal = options.pendingInputWal
    if (!wal) return null
    if (requirePrepared && (!wal.prepareHandoff || !wal.compareAndSwapHandoff)) return null
    if (!requirePrepared && !wal.putHandoff) return null
    const now = Date.now()
    if (requirePrepared) {
      attempt.handoffWalOwnerId ||= createClientRequestId()
      attempt.handoffWalRevision ||= 1
    }
    const record: ResponseHandoffWalRecord = {
      schemaVersion: 1,
      ownerRequestId: attempt.clientRequestId,
      requestSessionKey: attempt.requestSessionKey,
      clientRequestId: attempt.clientRequestId,
      clientMessageId: attempt.clientMessageId,
      params: structuredClone(attempt.params),
      composerText: attempt.composerText,
      recoveryAttachments: attempt.attachments.map(attachment => ({ ...attachment })),
      ...(attempt.restoreComposerOnHandoffFailure === false
        ? { restoreComposerOnFailure: false }
        : {}),
      ...(attempt.replayCoordinationKey
        ? { replayCoordinationKey: attempt.replayCoordinationKey }
        : {}),
      ...(requirePrepared
        ? {
            walOwnerId: attempt.handoffWalOwnerId!,
            walRevision: attempt.handoffWalRevision!,
          }
        : {}),
      state: requirePrepared ? 'preparing' : 'submitting',
      createdAt: now,
      updatedAt: now,
    }
    try {
      if (requirePrepared) {
        const prepared = await wal.prepareHandoff!(record)
        if (prepared.applied) return prepared.record
        const current = prepared.record
        if (
          current?.state === 'preparing'
          && current.ownerRequestId === record.ownerRequestId
          && current.clientMessageId === record.clientMessageId
          && current.replayCoordinationKey === record.replayCoordinationKey
          && current.walOwnerId === record.walOwnerId
          && current.walRevision === record.walRevision
        ) return current
        return null
      }
      await wal.putHandoff!(record)
      return record
    } catch {
      if (requirePrepared && record.walOwnerId && record.walRevision) {
        // The create operation may report failure after its write became
        // visible. A conditional delete cannot erase another tab's arm or
        // acceptance, and a failed delete leaves only an unarmed record.
        await wal.compareAndSwapHandoff?.(
          record.ownerRequestId,
          record.walOwnerId,
          record.walRevision,
          null,
        ).catch(() => {})
      } else {
        await wal.deleteHandoff?.(record.ownerRequestId).catch(() => {})
      }
      return null
    }
  }

  async function armPreparedResponseHandoff(
    record: ResponseHandoffWalRecord | null,
    attempt: SendAttempt,
  ): Promise<ResponseHandoffWalRecord | null> {
    const wal = options.pendingInputWal
    if (
      !record
      || record.state !== 'preparing'
      || !record.walOwnerId
      || !record.walRevision
      || !wal?.compareAndSwapHandoff
    ) return null
    const armed: ResponseHandoffWalRecord = {
      ...record,
      state: 'submitting',
      walRevision: record.walRevision + 1,
      updatedAt: Date.now(),
    }
    try {
      const transition = await wal.compareAndSwapHandoff(
        record.ownerRequestId,
        record.walOwnerId,
        record.walRevision,
        armed,
      )
      if (!transition.applied || !transition.record) return null
      attempt.handoffWalRevision = transition.record.walRevision
      return transition.record
    } catch {
      return null
    }
  }

  async function disarmResponseHandoff(
    record: ResponseHandoffWalRecord,
    attempt: SendAttempt,
  ): Promise<ResponseHandoffWalRecord | null> {
    const wal = options.pendingInputWal
    if (
      record.state !== 'submitting'
      || !record.walOwnerId
      || !record.walRevision
      || !wal?.compareAndSwapHandoff
    ) return null
    const preparing: ResponseHandoffWalRecord = {
      ...record,
      state: 'preparing',
      walRevision: record.walRevision + 1,
      updatedAt: Date.now(),
    }
    const transition = await wal.compareAndSwapHandoff(
      record.ownerRequestId,
      record.walOwnerId,
      record.walRevision,
      preparing,
    ).catch(() => null)
    if (!transition?.applied || !transition.record) return null
    attempt.handoffWalRevision = transition.record.walRevision
    return transition.record
  }

  async function discardUnsentResponseHandoff(
    record: ResponseHandoffWalRecord | null,
  ): Promise<void> {
    if (!record) return
    if (
      record.walOwnerId
      && record.walRevision
      && options.pendingInputWal?.compareAndSwapHandoff
    ) {
      await options.pendingInputWal.compareAndSwapHandoff(
        record.ownerRequestId,
        record.walOwnerId,
        record.walRevision,
        null,
      ).catch(() => {})
      return
    }
    const failed: ResponseHandoffWalRecord = {
      ...record,
      state: 'failed',
      errorCode: 'client_pre_dispatch_guard',
      updatedAt: Date.now(),
    }
    await options.pendingInputWal?.putHandoff?.(failed).catch(() => {})
    await options.pendingInputWal?.deleteHandoff?.(record.ownerRequestId).catch(() => {})
  }

  async function deleteResponseHandoff(
    record: ResponseHandoffWalRecord,
  ): Promise<boolean> {
    if (
      record.walOwnerId
      && record.walRevision
      && options.pendingInputWal?.compareAndSwapHandoff
    ) {
      const deletion = await options.pendingInputWal.compareAndSwapHandoff(
        record.ownerRequestId,
        record.walOwnerId,
        record.walRevision,
        null,
      ).catch(() => null)
      return deletion?.applied === true
    }
    if (!options.pendingInputWal?.deleteHandoff) return false
    try {
      await options.pendingInputWal.deleteHandoff(record.ownerRequestId)
      return true
    } catch {
      return false
    }
  }

  async function markResponseHandoffAccepted(
    gate: ResponseHandoffGate,
    acceptedSessionKey: string,
  ): Promise<void> {
    const current = gate.durableRecord
    if (!current) return
    const accepted: ResponseHandoffWalRecord = {
      ...current,
      state: 'accepted',
      acceptedSessionKey,
      ...(current.walRevision ? { walRevision: current.walRevision + 1 } : {}),
      updatedAt: Date.now(),
    }
    if (
      current.walOwnerId
      && current.walRevision
      && options.pendingInputWal?.compareAndSwapHandoff
    ) {
      const transition = await options.pendingInputWal.compareAndSwapHandoff(
        current.ownerRequestId,
        current.walOwnerId,
        current.walRevision,
        accepted,
      ).catch(() => null)
      if (transition?.applied && transition.record) gate.durableRecord = transition.record
      return
    }
    if (!options.pendingInputWal?.putHandoff) return
    gate.durableRecord = accepted
    await options.pendingInputWal.putHandoff(accepted).catch(() => {})
  }

  async function markResponseHandoffFailed(
    gate: ResponseHandoffGate,
    error: unknown,
  ): Promise<void> {
    const current = gate.durableRecord
    if (!current) return
    const failed: ResponseHandoffWalRecord = {
      ...current,
      state: 'failed',
      errorCode: errorCode(error),
      ...(current.walRevision ? { walRevision: current.walRevision + 1 } : {}),
      updatedAt: Date.now(),
    }
    if (
      current.walOwnerId
      && current.walRevision
      && options.pendingInputWal?.compareAndSwapHandoff
    ) {
      const transition = await options.pendingInputWal.compareAndSwapHandoff(
        current.ownerRequestId,
        current.walOwnerId,
        current.walRevision,
        failed,
      ).catch(() => null)
      if (transition?.applied && transition.record) gate.durableRecord = transition.record
    } else if (options.pendingInputWal?.putHandoff) {
      gate.durableRecord = failed
      await options.pendingInputWal.putHandoff(failed).catch(() => {})
    }
    await options.failPendingQueueHandoff?.(gate.ownerRequestId)
  }

  async function resetResponseHandoffForRetry(
    gate: ResponseHandoffGate,
    attempt: SendAttempt,
  ): Promise<void> {
    const current = gate.durableRecord
    if (
      !current?.walOwnerId
      || !current.walRevision
      || current.state !== 'submitting'
      || !options.pendingInputWal?.compareAndSwapHandoff
    ) return
    const preparing: ResponseHandoffWalRecord = {
      ...current,
      state: 'preparing',
      walRevision: current.walRevision + 1,
      updatedAt: Date.now(),
    }
    const transition = await options.pendingInputWal.compareAndSwapHandoff(
      current.ownerRequestId,
      current.walOwnerId,
      current.walRevision,
      preparing,
    ).catch(() => null)
    if (transition?.applied && transition.record) {
      gate.durableRecord = transition.record
      attempt.handoffWalRevision = transition.record.walRevision
    }
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
    await markResponseHandoffAccepted(gate, key)
    const adoption = key === gate.requestSessionKey && options.sessionKey.value === key
      ? await options.recoverPendingQueueHandoff?.(
          gate.requestSessionKey,
          key,
          gate.ownerRequestId,
        )
      : await options.adoptResponseSession(key, gate.ownerRequestId)
    if (gate.durableRecord && await deleteResponseHandoff(gate.durableRecord)) {
      gate.durableRecord = null
    }
    gate.authoritativeIdle = adoption?.authoritativeIdle === true
    gate.backgroundOnly = adoption?.backgroundOnly === true
    if (gate.stoppedByUser && options.sessionKey.value === key) {
      options.activeStreamSessionKey.value = key
      if (gate.acceptedTaskId) {
        options.taskOwnership?.requestStop(gate.acceptedTaskId)
        bindAcceptedTask(gate.acceptedTaskId)
      }
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

  async function finalizeRecoveredHandoff(
    record: ResponseHandoffWalRecord,
    targetSessionKey: string,
  ): Promise<void> {
    if (options.sessionKey.value === record.requestSessionKey) {
      const gate = beginResponseHandoff(
        record.requestSessionKey,
        record.ownerRequestId,
        record,
      )
      try {
        await handoffResponseSession(targetSessionKey, gate)
      } finally {
        finishResponseHandoff(gate)
      }
      return
    }
    let acceptedRecord: ResponseHandoffWalRecord = {
      ...record,
      state: 'accepted',
      acceptedSessionKey: targetSessionKey,
      ...(record.walRevision ? { walRevision: record.walRevision + 1 } : {}),
      updatedAt: Date.now(),
    }
    if (
      record.walOwnerId
      && record.walRevision
      && options.pendingInputWal?.compareAndSwapHandoff
    ) {
      const transition = await options.pendingInputWal.compareAndSwapHandoff(
        record.ownerRequestId,
        record.walOwnerId,
        record.walRevision,
        acceptedRecord,
      ).catch(() => null)
      if (!transition?.applied || !transition.record) return
      acceptedRecord = transition.record
    } else {
      await options.pendingInputWal?.putHandoff?.(acceptedRecord).catch(() => {})
    }
    await options.recoverPendingQueueHandoff?.(
      record.requestSessionKey,
      targetSessionKey,
      record.ownerRequestId,
    )
    await deleteResponseHandoff(acceptedRecord)
  }

  function restoreResponseHandoffDraft(record: ResponseHandoffWalRecord): boolean {
    if (options.sessionKey.value !== record.requestSessionKey) return false
    if (record.restoreComposerOnFailure === false) return true
    const restoredText = record.composerText.trim()
    if (restoredText && options.inputText.value !== restoredText) {
      options.inputText.value = [restoredText, options.inputText.value]
        .filter(Boolean)
        .join('\n')
    }
    const existingAttachmentIds = new Set(
      options.pendingAttachments.value.map(attachment => attachment.local_id),
    )
    const missingAttachments = record.recoveryAttachments.filter(attachment => (
      !existingAttachmentIds.has(attachment.local_id)
    ))
    if (missingAttachments.length > 0) {
      options.pendingAttachments.value = [
        ...missingAttachments.map(attachment => ({ ...attachment })),
        ...options.pendingAttachments.value,
      ]
    }
    const forkBeforeMessageId = typeof record.params.forkBeforeMessageId === 'string'
      ? record.params.forkBeforeMessageId
      : null
    if (!options.pendingForkBeforeMessageId.value && forkBeforeMessageId) {
      options.pendingForkBeforeMessageId.value = forkBeforeMessageId
    }
    if (!options.pendingSessionIntent.value && typeof record.params.intent === 'string') {
      options.pendingSessionIntent.value = record.params.intent
    }
    options.autoResizeTextarea()
    return true
  }

  function recoverResponseHandoffs(): Promise<void> {
    if (handoffRecoveryPromise) return handoffRecoveryPromise
    const operation = (async () => {
      const wal = options.pendingInputWal
      if (!wal?.listHandoffs || activeResponseHandoff) return
      let records: ResponseHandoffWalRecord[]
      try {
        records = await wal.listHandoffs()
      } catch {
        return
      }
      for (const record of records) {
        if (record.state === 'preparing') {
          if (record.walOwnerId && record.walRevision) {
            await wal.compareAndSwapHandoff?.(
              record.ownerRequestId,
              record.walOwnerId,
              record.walRevision,
              null,
            ).catch(() => {})
          }
          continue
        }
        if (record.state === 'failed') {
          if (restoreResponseHandoffDraft(record)) {
            await deleteResponseHandoff(record)
          }
          continue
        }
        if (record.state === 'accepted' && record.acceptedSessionKey) {
          await finalizeRecoveredHandoff(record, record.acceptedSessionKey)
          continue
        }
        let replayRecord = record
        let refreshedExpiredAttachments = false
        while (true) {
          try {
            const response = await options.rpc.call<ChatSendResponse>(
              'chat.send',
              replayRecord.params,
            )
            const targetSessionKey = response.sessionKey || replayRecord.requestSessionKey
            await finalizeRecoveredHandoff(replayRecord, targetSessionKey)
            break
          } catch (error) {
            const accepted = acceptedErrorInfo(error)
            if (accepted?.sessionKey) {
              await finalizeRecoveredHandoff(replayRecord, accepted.sessionKey)
              break
            }
            const rpcError = error as RpcClientError | null | undefined
            const code = errorCode(error)
            const definitelyRejected = rpcError?.accepted === false
            const canRefreshExpiredAttachments = (
              definitelyRejected
              && !refreshedExpiredAttachments
              && options.prepareAttachmentsForSend
              && (code === 'ATTACHMENT_EXPIRED' || code === 'ATTACHMENT_LOST_IN_RESTART')
              && replayRecord.recoveryAttachments.some(attachment => (
                attachment.kind === 'staged' && Boolean(attachment.file)
              ))
            )
            if (canRefreshExpiredAttachments) {
              refreshedExpiredAttachments = true
              const refreshed = replayRecord.recoveryAttachments.map(attachment => ({
                ...attachment,
                ...(attachment.kind === 'staged' && attachment.file
                  ? { expires_at: 0 }
                  : {}),
              }))
              const ready = await options.prepareAttachmentsForSend!({
                attachments: refreshed,
                isCurrent: () => true,
              })
              const sendable = refreshed.filter(isSendableAttachment)
              if (ready && sendable.length === refreshed.length) {
                replayRecord = {
                  ...replayRecord,
                  params: {
                    ...replayRecord.params,
                    attachments: sendable.map(serializeSendableAttachment),
                  },
                  recoveryAttachments: refreshed,
                  updatedAt: Date.now(),
                }
                await wal.putHandoff?.(replayRecord)
                // The Gateway explicitly rejected the old attachment tokens,
                // so changing only those tokens cannot conflict with a receipt.
                continue
              }
            }
            if (definitelyRejected && rpcError?.retryable === false) {
              await wal.putHandoff?.({
                ...replayRecord,
                state: 'failed',
                errorCode: code,
                updatedAt: Date.now(),
              }).catch(() => {})
              await options.failPendingQueueHandoff?.(replayRecord.ownerRequestId)
              pushToast(sendFailureMessage(error), { tone: 'danger' })
            }
            // Unknown/retryable acceptance deliberately remains submitting
            // and is replayed byte-for-byte after the next reconnect.
            break
          }
        }
      }
    })().finally(() => {
      handoffRecoveryPromise = null
    })
    handoffRecoveryPromise = operation
    return operation
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
    optionsForResponse: { finishFreshStream: boolean; forceFreshStream?: boolean },
  ): boolean {
    const status = terminalResponseStatus(response)
    if (!status) return false
    let finalizedFreshStream = false
    if (
      optionsForResponse.finishFreshStream
      && freshSendToken !== null
      && (
        activeFreshSendToken === freshSendToken
        || optionsForResponse.forceFreshStream === true
      )
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
      if (!hasAuthoritativeWork()) options.schedulePendingDrainAfterTerminal()
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
      source: force ? 'webui_stop' : 'webui_stale_send',
    }
    // A user Stop that raced durable ingress is still task-scoped. If an
    // older/partial response has no task id, tell the gateway to fail closed
    // instead of falling back to the legacy whole-session abort surface.
    if (force) params.scope = 'task'
    if (taskId) params.taskId = taskId
    options.rpc.call<{ aborted?: boolean }>('chat.abort', params)
      .then((response) => {
        if (force && !taskId) {
          void options.reconcileTaskOwnership?.()
          return
        }
        if (!force || response?.aborted === true) return
        options.taskOwnership?.clearStop(taskId)
        void options.reconcileTaskOwnership?.()
        reportAbortFailure([requestSessionKey, acceptedSessionKey])
      })
      .catch(() => {
        if (!force) return
        options.taskOwnership?.clearStop(taskId)
        void options.reconcileTaskOwnership?.()
        reportAbortFailure([requestSessionKey, acceptedSessionKey])
      })
  }

  async function dispatchSteerV2(
    text: string,
    optionsForSteer: {
      composerSnapshot?: ComposerSnapshot
      queuedItem?: ChatPendingItem
    } = {},
  ): Promise<ChatSendOutcome> {
    const requestSessionKey = options.sessionKey.value
    let pendingItem = optionsForSteer.queuedItem
    const recovered = pendingItem
      ? options.steerDelivery.attemptForItem(pendingItem)
      : null
    if (!requestSessionKey || !text.trim()) return 'not_sent'
    if (!options.supportsMethod?.('sessions.steer.v2')) {
      return recovered ? 'retryable_failure' : 'not_sent'
    }
    const durablePending = Boolean(
      recovered?.request.pendingInputId
      || (
        pendingItem?.pendingPersistenceState === 'staged'
        && pendingItem.pendingInputId
        && pendingItem.pendingClientRequestId
        && pendingItem.pendingClientMessageId
        && pendingItem.pendingRequestFingerprint
        && pendingItem.pendingServerRevision
      ),
    )
    if (
      durablePending
      && !options.supportsMethod?.('sessions.pending_inputs.steer')
    ) {
      return recovered ? 'retryable_failure' : 'not_sent'
    }
    if (
      !recovered
      && !canSteerPayload(
        text,
        pendingItem ? pendingItem.attachments : options.pendingAttachments.value,
        pendingItem ? pendingItem.intent : options.pendingSessionIntent.value,
        pendingItem ? null : options.pendingForkBeforeMessageId.value,
      )
    ) return 'not_sent'
    if (options.sendBlockedReason?.value || options.hasPendingAttachmentWork()) {
      return recovered ? 'retryable_failure' : 'not_sent'
    }
    if (
      optionsForSteer.composerSnapshot
      && !composerMatchesSnapshot(optionsForSteer.composerSnapshot)
    ) return 'not_sent'

    const expectedTurnId = recovered?.request.expected_turn_id || capabilityExpectedTurnId()
    if (!expectedTurnId) return 'not_sent'
    const pendingIdentity = pendingItem?.pendingPersistenceState === 'staged'
      && pendingItem.pendingInputId
      && pendingItem.pendingClientRequestId
      && pendingItem.pendingClientMessageId
      && pendingItem.pendingRequestFingerprint
      && pendingItem.pendingServerRevision
      ? {
          pendingInputId: pendingItem.pendingInputId,
          clientRequestId: pendingItem.pendingClientRequestId,
          clientMessageId: pendingItem.pendingClientMessageId,
          requestFingerprint: pendingItem.pendingRequestFingerprint,
          expectedRevision: pendingItem.pendingServerRevision,
        }
      : null
    if (durablePending && !pendingIdentity && !recovered?.request.pendingInputId) {
      return recovered ? 'retryable_failure' : 'not_sent'
    }
    const freshParams: SessionSteerV2Params = {
      key: requestSessionKey,
      message: text.trim(),
      expected_turn_id: expectedTurnId,
      client_request_id: pendingIdentity?.clientRequestId || createClientRequestId(),
      client_message_id: pendingIdentity?.clientMessageId || createClientMessageId(),
      ...(pendingIdentity
        ? {
            pendingInputId: pendingIdentity.pendingInputId,
            requestFingerprint: pendingIdentity.requestFingerprint,
            expectedRevision: pendingIdentity.expectedRevision,
          }
        : {}),
      surface_id: 'webui',
      _source: chatSourceMetadata(options),
    }
    if (recovered && recovered.request.key !== requestSessionKey) {
      return 'retryable_failure'
    }
    const attempt = pendingItem
      ? options.steerDelivery.begin(pendingItem, recovered ? undefined : freshParams)
      : null
    if (!pendingItem) {
      pendingItem = options.enqueuePendingSteerAttempt?.({
        request: freshParams,
        phase: 'submitting',
      }, pendingQueueOwner()) || undefined
      if (!pendingItem) return 'not_sent'
    }
    const activeAttempt = attempt || options.steerDelivery.begin(pendingItem)
    if (!activeAttempt) return 'not_sent'
    const params = activeAttempt.request
    if (!optionsForSteer.queuedItem) {
      // The transport-owned pending row exists before the composer is
      // consumed, so every non-durable outcome remains visible and retryable.
      if (
        optionsForSteer.composerSnapshot
        && !composerMatchesSnapshot(optionsForSteer.composerSnapshot)
      ) {
        options.steerDelivery.reject(pendingItem, false)
        return 'not_sent'
      }
      options.inputText.value = ''
      options.pendingSessionIntent.value = null
      options.pendingForkBeforeMessageId.value = null
      options.autoResizeTextarea()
    } else {
      // Manual retry must replay the complete immutable request snapshot,
      // including source policy and original session/turn identities.
      pendingItem.steerAttempt = activeAttempt
    }
    try {
      const response = await options.rpc.call<SessionSteerV2Response>(
        params.pendingInputId
          ? 'sessions.pending_inputs.steer'
          : 'sessions.steer.v2',
        params as unknown as Record<string, unknown>,
      )
      const sessionChanged = options.sessionKey.value !== requestSessionKey
      if (sessionChanged && response.accepted === true) {
        options.steerDelivery.acknowledgeAcceptedOffscreen(pendingItem)
        return 'accepted'
      }
      if (response.accepted === false) {
        if (!sessionChanged && pendingItem.steerAttempt?.stopRequested) {
          options.steerDelivery.reject(pendingItem)
          return 'accepted'
        }
        if (response.fallback_safe === true) {
          options.steerDelivery.fallback(pendingItem)
          return 'deferred'
        }
        if (
          response.retryable === true
          || (
            response.retryable !== false
            && /retry|resend/i.test(response.recovery || '')
          )
        ) {
          options.steerDelivery.markRetryable(pendingItem, 'retryable_rejected', {
            code: response.failure_code,
          })
          return 'retryable_failure'
        }
        if (sessionChanged) {
          // Navigation parks transport-owned Steers with their source chat.
          // A permanent non-admission turns that parked row back into an
          // ordinary draft; never restore it into the newly selected composer.
          options.steerDelivery.fallback(pendingItem)
          return 'deferred'
        }
        options.steerDelivery.reject(pendingItem)
        return 'not_sent'
      }
      // The v2 RPC contract has one explicit admission bit. A fulfilled but
      // malformed/mixed-version response is still unknown; only a typed event
      // or matching history row may independently prove durability.
      if (response.accepted !== true) {
        options.steerDelivery.markRetryable(pendingItem, 'acceptance_unknown', {
          code: response.failure_code,
        })
        if (!sessionChanged) options.scheduleHistorySync()
        return 'retryable_failure'
      }
      options.steerDelivery.accept({
        clientRequestId: params.client_request_id,
        clientMessageId: params.client_message_id,
        expectedTurnId: params.expected_turn_id,
        userMessageId: String(response.user_message_id || ''),
        disposition: response.disposition || 'steering',
        revision: response.revision,
        turnId: response.turn_id,
        promotedTurnId: response.promoted_turn_id,
        promotedFromTurnId: response.promoted_from_turn_id,
        appliedIteration: response.applied_iteration,
        modelCallId: response.model_call_id,
      }, pendingItem)
      return 'accepted'
    } catch (error: unknown) {
      const accepted = (error as RpcClientError | null | undefined)?.accepted
      const sessionChanged = options.sessionKey.value !== requestSessionKey
      if (accepted === true) {
        if (sessionChanged) {
          options.steerDelivery.acknowledgeAcceptedOffscreen(pendingItem)
          return 'accepted'
        }
        options.steerDelivery.accept({
          clientRequestId: params.client_request_id,
          clientMessageId: params.client_message_id,
          expectedTurnId: params.expected_turn_id,
          userMessageId: String(
          rpcErrorDetail(error, 'user_message_id')
          || rpcErrorDetail(error, 'message_id')
          || '',
          ),
          disposition: (rpcErrorDetail(error, 'disposition') || 'steering') as ChatMessage['inputDisposition'],
          turnId: String(rpcErrorDetail(error, 'turn_id') || params.expected_turn_id),
        }, pendingItem)
        return 'accepted'
      }
      if (accepted === false && steerFallbackSafe(error)) {
        if (!sessionChanged && pendingItem.steerAttempt?.stopRequested) {
          options.steerDelivery.reject(pendingItem)
          return 'accepted'
        }
        options.steerDelivery.fallback(pendingItem)
        return 'deferred'
      }
      if (accepted !== false) {
        options.steerDelivery.markRetryable(pendingItem, 'acceptance_unknown', {
          code: errorCode(error),
          retryAfterMs: Number(rpcErrorDetail(error, 'retry_after_ms')) || undefined,
        })
        if (!sessionChanged) options.scheduleHistorySync()
        return 'retryable_failure'
      }
      if (rpcErrorDetail(error, 'retryable') === true) {
        options.steerDelivery.markRetryable(pendingItem, 'retryable_rejected', {
          code: errorCode(error),
          retryAfterMs: Number(rpcErrorDetail(error, 'retry_after_ms')) || undefined,
        })
        return 'retryable_failure'
      }
      if (sessionChanged) {
        options.steerDelivery.fallback(pendingItem)
        return 'deferred'
      }
      options.steerDelivery.reject(pendingItem)
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
    let durableText = text
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
      durableText = `/${text}`
      sendableAttachments = options.pendingAttachments.value.filter(isSendableAttachment)
      hasPayload = text || sendableAttachments.length > 0
    }

    if (hasPayload) {
      // Transport readiness is a fail-closed precondition. Keep the composer
      // and attachment refs untouched so manual, keyboard, and automatic sends
      // all preserve the exact draft until live subscription recovery succeeds.
      if (options.sendBlockedReason?.value) return
      if (options.taskOwnership && !options.taskOwnership.hydrationResolved.value) return
      if (options.validateActiveProjectBeforeSend) {
        if (await refreshedActiveProjectBlocksSend()) return
      }
      if (options.sessionKey.value !== requestSessionKey) return
      if (!queueOwnerMatchesSnapshot(composerSnapshot)) return
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
        initialRoutingMode: composerSnapshot.initialRoutingMode,
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

    const slashClassification = !bypassSlashCommand
      && !isLiteralSlash
      && text.startsWith('/')
      ? await options.classifySlashCommand(text)
      : null
    if (slashClassification !== null) {
      if (
        options.sessionKey.value !== requestSessionKey
        || !composerMatchesSnapshot(composerSnapshot)
        || !queueOwnerMatchesSnapshot(composerSnapshot)
        || Boolean(options.sendBlockedReason?.value)
        || Boolean(options.taskOwnership && !options.taskOwnership.hydrationResolved.value)
      ) return
      if (
        options.validateActiveProjectBeforeSend
        && await refreshedActiveProjectBlocksSend()
      ) return
      if (
        options.sessionKey.value !== requestSessionKey
        || !composerMatchesSnapshot(composerSnapshot)
        || !queueOwnerMatchesSnapshot(composerSnapshot)
        || Boolean(options.sendBlockedReason?.value)
        || Boolean(options.taskOwnership && !options.taskOwnership.hydrationResolved.value)
      ) return
    }

    const compactInFlight = options.isCompactInFlightForCurrentSession()
    if (
      options.stream.isStreaming.value
      || hasAuthoritativeWork()
      || compactInFlight
      || responseHandoffBlocksCurrentSession()
    ) {
      const currentHandoffInFlight = responseHandoffBlocksCurrentSession()
      const stillBusy = options.stream.isStreaming.value
        || hasAuthoritativeWork()
        || options.isCompactInFlightForCurrentSession()
        || currentHandoffInFlight
      if (stillBusy) {
        if (
          !bypassSlashCommand
          && !isLiteralSlash
          && isControlInput(text)
          && slashClassification !== 'unknown'
        ) {
          // Registered slash commands and bang inputs are live controls. Running
          // them later can target a different task/session, so keep the exact
          // command editable while busy. Confirmed unknown slash input is plain
          // text and continues into the ordinary follow-up queue below.
          return
        }
        if (!hasPayload) return
        if (currentHandoffInFlight && !activeResponseHandoff?.durableRecord) {
          // The fork itself may proceed without IndexedDB, but a follow-up must
          // stay editable until the target session is known. Otherwise refresh
          // can strand an ownerless message on the parent session.
          pushToast(i18n.global.t('chat.toast.queuePersistenceUnavailable'), { tone: 'info' })
          return
        }
        if (
          options.busySendMode.value === 'steer'
          && !options.taskOwnership?.stopRequestedTaskId.value
          && !isLiteralSlash
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
        const queued = await Promise.resolve(
          composerChanged || invocation.textOverride !== undefined
            ? options.enqueuePendingPayload?.({
              text: durableText,
              attachments: composerSnapshot.payloadAttachments,
              intent: composerSnapshot.intent,
              ...(slashClassification === 'unknown'
                ? { confirmedPlainText: true }
                : {}),
            }, queueOwnerFromSnapshot(composerSnapshot)) ?? false
            : slashClassification === 'unknown'
              ? options.enqueuePendingInput(
                durableText,
                queueOwnerFromSnapshot(composerSnapshot),
                { confirmedPlainText: true },
              )
              : options.enqueuePendingInput(
                durableText,
                queueOwnerFromSnapshot(composerSnapshot),
              ),
        )
        if (!queued) {
          pushToast(i18n.global.t('chat.toast.queueFull'), { tone: 'info' })
        }
        return
      }
    }

    if (slashClassification !== null && slashClassification !== 'unknown') {
      const handled = await options.executeSlashCommand(text, slashClassification)
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
    const dispatchText = !item.hiddenControl && text.startsWith('//')
      ? text.slice(1)
      : text
    const ownerSessionKey = expectedSessionKey
      || item.ownerSessionKey
      || options.sessionKey.value
    const retryAttempt = recoveredQueuedAttempts.get(item) ?? null
    const steerRetryAttempt = options.steerDelivery.attemptForItem(item)
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
    const serverStagedItem = item.pendingPersistenceState === 'staged'
      && Boolean(item.pendingInputId)
    if (
      !serverStagedItem
      && item.attachments.some(attachment => !isSendableAttachment(attachment))
    ) {
      return preserveRetryState('not_sent')
    }
    if (
      delivery === 'followup'
      && !item.hiddenControl
      && item.text.trim().startsWith('/')
      && !item.text.trim().startsWith('//')
    ) {
      if (
        options.stream.isStreaming.value
        || hasAuthoritativeWork()
        || options.isCompactInFlightForCurrentSession()
      ) {
        return preserveRetryState('deferred')
      }
      const slashClassification = await options.classifySlashCommand(item.text.trim())
      if (options.sessionKey.value !== ownerSessionKey) {
        return preserveRetryState('not_sent')
      }
      if (options.sendBlockedReason?.value) return blockedOutcome()
      if (
        options.validateActiveProjectBeforeSend
        && await refreshedActiveProjectBlocksSend()
      ) return blockedOutcome()
      if (options.sessionKey.value !== ownerSessionKey) {
        return preserveRetryState('not_sent')
      }
      if (options.sendBlockedReason?.value) return blockedOutcome()
      if (
        options.stream.isStreaming.value
        || hasAuthoritativeWork()
        || options.isCompactInFlightForCurrentSession()
        || responseHandoffBlocksCurrentSession()
      ) {
        return preserveRetryState('deferred')
      }
      if (slashClassification === 'unavailable') {
        return preserveRetryState('retryable_failure')
      }
      if (slashClassification === 'registered') {
        if (item.attachments.length > 0) {
          return preserveRetryState('retryable_failure')
        }
        if (serverStagedItem) {
          if (!await options.cancelDurablePendingItem?.(item, { retainAfterCancel: true })) {
            return preserveRetryState('retryable_failure')
          }
        }
        // A queued item was previously classified as ordinary text. If the
        // catalog now recognizes it, never turn a background drain into an
        // automatic control action. In particular, an idempotent cancel can
        // report success in multiple tabs (or after a lost ACK), so it cannot
        // grant exactly-once authority to execute /reset, /new, /goal, etc.
        // Leave the detached item editable for an explicit user decision.
        return preserveRetryState('not_sent')
      }
      // Confirmed unknown slash input falls through to the normal send path
      // below, mirroring the primary onSend contract.
    }
    if (hasSendableModelInputImageAttachment(item.attachments)) {
      if (options.modelRoutingSettingsBusy.value) {
        return preserveRetryState(delivery === 'followup' ? 'deferred' : 'not_sent')
      }
      if (
        options.imageInputAdmission?.value === 'blocked'
        || (
          options.imageInputAdmission === undefined
          && options.modelRoutingMode.value === 'llm_ensemble'
        )
      ) {
        return preserveRetryState('not_sent')
      }
    }
    if (
      options.isCompactInFlightForCurrentSession()
      || responseHandoffBlocksCurrentSession()
      || (
        delivery === 'followup'
        && (hasAuthoritativeWork() || options.stream.isStreaming.value)
      )
    ) {
      return preserveRetryState(delivery === 'followup' ? 'deferred' : 'not_sent')
    }

    if (delivery === 'steer') {
      if (text.startsWith('//')) return preserveRetryState('not_sent')
      return dispatchSteerV2(text, { queuedItem: item })
    }
    const outcome = await dispatchSend(dispatchText, {
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
      ...(item.pendingInputId
        && item.pendingClientRequestId
        && item.pendingClientMessageId
        ? { durablePendingItem: item }
        : {}),
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
    const preDispatchAllowed = (
      stage: 'preflight' | 'before_rpc' = 'preflight',
    ) => sendOpts.preDispatchGuard?.(stage) !== false
    if (!preDispatchAllowed()) return 'not_sent'
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
    const initialRoutingMode = (
      sendOpts.payload
      && 'initialRoutingMode' in sendOpts.payload
    )
      ? sendOpts.payload.initialRoutingMode ?? null
      : initialRoutingModeForIntent(intent)
    const initialSendableAttachments = sourceAttachments.filter(isSendableAttachment)
    // This is deliberately before optimistic rendering, composer clearing,
    // stream state, and chat.send. A blocked draft remains exactly editable.
    if (modelImageSendBlocked(sourceAttachments)) return 'not_sent'
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
          initialRoutingMode,
          forkBeforeMessageId,
          workspaceId,
        })
        && retryCandidate.queueMode === sendOpts.queueMode
      ),
    )
    const retryAttempt = isRecoveredRetry ? retryCandidate : null
    // The automatic receipt recovery and a user-triggered retry share the
    // immutable SendAttempt. Never put the same idempotency key on the wire
    // twice concurrently; the later caller can observe/retry after the active
    // single-flight settles without mutating the optimistic UI again.
    if (retryAttempt?.acceptanceInFlight) return 'retryable_failure'
    const sendAttachmentIds = new Set(
      (retryAttempt?.attachments || initialSendableAttachments)
        .map(attachment => attachment.local_id),
    )
    // A recovered attempt must keep the exact serialized attachment tokens and
    // metadata that were fingerprinted with its idempotency key.
    const serverStagedPendingItem = sendOpts.durablePendingItem?.pendingPersistenceState === 'staged'
      ? sendOpts.durablePendingItem
      : undefined
    if (!retryAttempt && !serverStagedPendingItem && options.prepareAttachmentsForSend) {
      const ready = await options.prepareAttachmentsForSend({
        isCurrent: () => options.sessionKey.value === requestSessionKey,
        ...(sendOpts.payload ? { attachments: sourceAttachments } : {}),
      })
      if (!ready) return 'not_sent'
      if (options.sessionKey.value !== requestSessionKey) return 'not_sent'
      if (!preDispatchAllowed()) return 'not_sent'
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
      && !serverStagedPendingItem
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
    if (!text && attachmentsToSend.length === 0 && !serverStagedPendingItem) {
      return 'not_sent'
    }

    const userText = text
    let attempt = retryAttempt
    let acceptedVisibleReplayCommitted = false
    const commitAcceptedVisibleReplay = (accepted?: {
      messageId?: string
      turnId?: string
    }): boolean => {
      const replay = sendOpts.acceptedVisibleReplay
      if (!replay || acceptedVisibleReplayCommitted || !attempt) return true
      const anchorIndex = options.messages.value.findIndex(message => (
        message.role === 'user'
        && message.messageId === replay.forkBeforeMessageId
      ))
      if (anchorIndex < 0) return false
      options.messages.value = options.messages.value.slice(0, anchorIndex)
      options.messages.value.push({
        role: 'user',
        text: userText,
        ts: new Date().toISOString(),
        clientId: attempt.clientMessageId,
        ...(accepted?.messageId ? { messageId: accepted.messageId } : {}),
        ...(accepted?.turnId ? { turnId: accepted.turnId } : {}),
      })
      options.autoScroll.value = true
      options.scrollToBottom()
      acceptedVisibleReplayCommitted = true
      return true
    }
    let durableHandoffRecord: ResponseHandoffWalRecord | null = null
    const rejectBeforeDispatch = async (): Promise<ChatSendOutcome> => {
      if (attempt && sendOpts.acceptedVisibleReplay) {
        sendOpts.rememberRetryableAttempt?.(attempt)
      }
      await discardUnsentResponseHandoff(durableHandoffRecord)
      return 'not_sent'
    }
    if (!attempt) {
      const durablePendingItem = sendOpts.durablePendingItem
      const clientMessageId = durablePendingItem?.pendingClientMessageId
        || sendOpts.replayCoordination?.clientMessageId
        || createClientMessageId()
      const params: ChatSendParams = {
        clientRequestId: durablePendingItem?.pendingClientRequestId
          || sendOpts.replayCoordination?.clientRequestId
          || createClientRequestId(),
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
      if (initialRoutingMode) params.initialRoutingMode = initialRoutingMode
      if (forkBeforeMessageId) params.forkBeforeMessageId = forkBeforeMessageId
      if (attachmentsToSend.length > 0 || sendOpts.includeEmptyAttachments) {
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
        initialRoutingMode,
        forkBeforeMessageId,
        workspaceId,
        ...(sendOpts.acceptedVisibleReplay
          ? { restoreComposerOnHandoffFailure: false }
          : {}),
        ...(sendOpts.replayCoordination
          ? { replayCoordinationKey: sendOpts.replayCoordination.key }
          : {}),
        params,
      }
      if (attempt.forkBeforeMessageId) {
        durableHandoffRecord = await persistResponseHandoff(
          attempt,
          sendOpts.requirePreparedHandoff,
        )
        if (sendOpts.requirePreparedHandoff && !durableHandoffRecord) {
          return rejectBeforeDispatch()
        }
        if (!preDispatchAllowed()) return rejectBeforeDispatch()
      }
      if (!sendOpts.acceptedVisibleReplay) {
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
    }
    if (attempt.forkBeforeMessageId && !durableHandoffRecord) {
      durableHandoffRecord = await persistResponseHandoff(
        attempt,
        sendOpts.requirePreparedHandoff,
      )
      if (sendOpts.requirePreparedHandoff && !durableHandoffRecord) {
        return rejectBeforeDispatch()
      }
      if (!preDispatchAllowed()) return rejectBeforeDispatch()
    }
    if (!preDispatchAllowed()) return rejectBeforeDispatch()
    if (!preserveComposer) options.closeSlashMenu()
    recordSessionNavigationDiag('send.start', {
      requestSession: requestSessionKey,
      current: requestSessionKey,
    })
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
    if (!preDispatchAllowed()) return rejectBeforeDispatch()
    const freshSendToken = wasStreaming
      ? null
      : beginFreshStream(requestSessionKey, attempt)
    if (!preDispatchAllowed('before_rpc')) {
      if (freshSendToken && activeFreshSendToken === freshSendToken) {
        activeFreshSendToken = null
        options.activeStreamTaskId.value = ''
        options.activeStreamSessionKey.value = ''
        options.stream.endStreaming()
      }
      return rejectBeforeDispatch()
    }
    if (sendOpts.requirePreparedHandoff) {
      const armed = await armPreparedResponseHandoff(durableHandoffRecord, attempt)
      if (!armed) {
        if (freshSendToken && activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
          options.activeStreamTaskId.value = ''
          options.activeStreamSessionKey.value = ''
          options.stream.endStreaming()
        }
        return rejectBeforeDispatch()
      }
      durableHandoffRecord = armed
      if (!preDispatchAllowed('before_rpc')) {
        durableHandoffRecord = await disarmResponseHandoff(armed, attempt) || armed
        if (freshSendToken && activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
          options.activeStreamTaskId.value = ''
          options.activeStreamSessionKey.value = ''
          options.stream.endStreaming()
        }
        return rejectBeforeDispatch()
      }
    }
    options.aborted.value = false
    let responseHandoff = (
      attempt.forkBeforeMessageId
        ? beginResponseHandoff(
            requestSessionKey,
            attempt.clientRequestId,
            durableHandoffRecord,
          )
        : null
    )
    const acceptanceTransaction = beginAcceptanceTransaction(
      requestSessionKey,
      freshSendToken,
      attempt,
    )

    try {
      const stagedPendingItem = serverStagedPendingItem
      const acceptanceRpc = attempt.acceptanceRpc || {
        method: stagedPendingItem
          ? 'sessions.pending_inputs.dispatch' as const
          : 'chat.send' as const,
        params: stagedPendingItem
          ? {
              key: requestSessionKey,
              pendingInputId: stagedPendingItem.pendingInputId,
              clientRequestId: stagedPendingItem.pendingClientRequestId,
              requestFingerprint: stagedPendingItem.pendingRequestFingerprint,
            }
          : attempt.params as unknown as Record<string, unknown>,
      }
      attempt.acceptanceRpc = acceptanceRpc
      attempt.acceptanceInFlight = true
      const res = await options.rpc.call<ChatSendResponse>(
        acceptanceRpc.method,
        acceptanceRpc.params,
      )
      attempt.acceptanceResolved = true
      attempt.acceptedTaskId = acceptedTaskId(res)
      attempt.acceptedSessionKey = res?.sessionKey || requestSessionKey
      if (!commitAcceptedVisibleReplay({
        messageId: res?.user_message_id || res?.message_id || '',
        turnId: acceptedTaskId(res),
      })) {
        options.scheduleHistorySync()
      }
      if (recoveredAttempt?.clientRequestId === attempt.clientRequestId) {
        recoveredAttempt = null
      }
      // A draft becomes a routable session only after the gateway has durably
      // accepted its first turn. Keeping the intent until this point avoids
      // remounting onto an empty history when acceptance fails or is unknown.
      consumeAcceptedSessionIntent(attempt)
      const accepted = noteAcceptedTask(res, requestSessionKey)
      const taskId = accepted.taskId
      const terminalStatus = terminalResponseStatus(res)
      if (responseHandoff) {
        responseHandoff.acceptedTaskId = taskId
        responseHandoff.terminalResponse = Boolean(terminalStatus)
      }
      const stoppedByUser = acceptanceTransaction.stoppedByUser
        || responseHandoff?.stoppedByUser === true
      const lostFreshStream = !wasStreaming
        && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)
      if (stoppedByUser || lostFreshStream) {
        const acceptedSessionKey = res?.sessionKey || requestSessionKey
        const stoppedTerminalIsCurrent = Boolean(
          stoppedByUser
          && terminalStatus
          && options.sessionKey.value === requestSessionKey
          && acceptedSessionKey === requestSessionKey,
        )
        if (stoppedByUser && (taskId || terminalStatus)) {
          clearAcceptanceStop(acceptanceTransaction)
        }
        if (stoppedTerminalIsCurrent) {
          bindAcceptedUserMessage(attempt.clientMessageId, res)
          handleTerminalResponse(res, freshSendToken, {
            finishFreshStream: !wasStreaming,
            forceFreshStream: true,
          })
          return 'accepted'
        }
        if (stoppedByUser && taskId && options.sessionKey.value === requestSessionKey) {
          options.taskOwnership?.requestStop(taskId)
          bindAcceptedTask(taskId)
        }
        // A same-session accepted row remains part of the visible parent even
        // after Stop or a newer send. A child identity must never be written
        // onto that parent row; the child history owns it after handoff.
        if (
          options.sessionKey.value === requestSessionKey
          && acceptedSessionKey === requestSessionKey
        ) {
          bindAcceptedUserMessage(attempt.clientMessageId, res)
        }
        if (stoppedByUser && taskId && attempt.stopRequested) {
          // A manual receipt replay can win the race with the sleeping
          // automatic worker. Share the same attempt-owned resolution so the
          // worker observes completion instead of issuing a duplicate abort.
          void abortRecoveredAcceptedTask(attempt).then((resolved) => {
            if (!resolved && attempt.stopRequested) scheduleAcceptanceRecovery(attempt)
          })
        } else {
          abortStaleAcceptedTask(res, requestSessionKey, stoppedByUser)
        }
        if (
          stoppedByUser
          && options.sessionKey.value === requestSessionKey
          && acceptedSessionKey !== requestSessionKey
        ) {
          durableHandoffRecord ||= await persistResponseHandoff(attempt)
          responseHandoff ||= beginResponseHandoff(
            requestSessionKey,
            attempt.clientRequestId,
            durableHandoffRecord,
          )
          responseHandoff.stoppedByUser = true
          responseHandoff.acceptedTaskId = taskId
          responseHandoff.terminalResponse = Boolean(terminalStatus)
          await handoffResponseSession(acceptedSessionKey, responseHandoff)
        } else if (responseHandoff && acceptedSessionKey === requestSessionKey) {
          await handoffResponseSession(requestSessionKey, responseHandoff)
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
        // A different same-session task can start while this chat.send ACK is
        // pending. The queued B ACK must release the PENDING render gate to
        // authoritative A (and replay A's early frames), never bind B merely
        // because B is the request whose response arrived.
        if (accepted.renderTaskId) bindAcceptedTask(accepted.renderTaskId)
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
        durableHandoffRecord ||= await persistResponseHandoff(attempt)
        responseHandoff ||= beginResponseHandoff(
          requestSessionKey,
          attempt.clientRequestId,
          durableHandoffRecord,
        )
        responseHandoff.acceptedTaskId = taskId
        responseHandoff.terminalResponse = Boolean(terminalStatus)
        await handoffResponseSession(decision.responseSessionKey, responseHandoff)
      } else if (responseHandoff && decision.reason === 'same_session') {
        await handoffResponseSession(requestSessionKey, responseHandoff)
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
      const rpcError = err as RpcClientError | null | undefined
      const acceptedError = acceptedErrorInfo(err)
      if (acceptedError && !commitAcceptedVisibleReplay({
        messageId: acceptedError.messageId,
      })) {
        options.scheduleHistorySync()
      }
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
        attempt.requiresIdempotentReplay = hasUnknownAcceptance(err)
        if (preserveComposer) {
          if (sendOpts.rememberRetryableAttempt) {
            sendOpts.rememberRetryableAttempt(attempt)
          } else {
            recoveredAttempt = attempt
          }
        } else if (restoreComposer) {
          restoreSendAttempt(attempt, {
            requiresIdempotentReplay: hasUnknownAcceptance(err),
          })
        }
      }
      const stoppedByUser = acceptanceTransaction.stoppedByUser
        || responseHandoff?.stoppedByUser === true
      if (stoppedByUser) {
        if (acceptedError?.terminalWithoutTask || rpcError?.accepted === false) {
          clearAcceptanceStop(acceptanceTransaction)
        } else if (hasUnknownAcceptance(err)) {
          void options.reconcileTaskOwnership?.()
        }
      }
      if (hasUnknownAcceptance(err)) {
        attempt.requiresIdempotentReplay = true
        if (attempt.stopRequested || attempt.autoRecoverAcceptance) {
          scheduleAcceptanceRecovery(attempt)
        }
      } else if (rpcError?.accepted === false || acceptedError?.terminalWithoutTask) {
        attempt.acceptanceResolved = true
      }
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
        durableHandoffRecord ||= await persistResponseHandoff(attempt)
        responseHandoff ||= beginResponseHandoff(
          requestSessionKey,
          attempt.clientRequestId,
          durableHandoffRecord,
        )
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
        if (responseHandoff && acceptedSessionKey === requestSessionKey) {
          await handoffResponseSession(requestSessionKey, responseHandoff)
        }
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
      if (responseHandoff && rpcError?.accepted === false) {
        if (sendOpts.requirePreparedHandoff && rpcError.retryable !== false) {
          await resetResponseHandoffForRetry(responseHandoff, attempt)
        } else if (rpcError.retryable === false) {
          await markResponseHandoffFailed(responseHandoff, err)
        }
      }
      rememberRetryableAttempt(true)
      if (acceptedError || !sendOpts.suppressRejectedFailureMessage) {
        options.messages.value.push({
          role: 'error',
          text: sendFailureMessage(err),
          errorCode: errorCode(err),
          ts: new Date().toISOString(),
        })
      }
      return acceptedError ? 'accepted' : 'retryable_failure'
    } finally {
      attempt.acceptanceInFlight = false
      finishAcceptanceTransaction(acceptanceTransaction)
      finishResponseHandoff(responseHandoff)
    }
  }

  async function sendUsageBarrierReplay(
    payload: UsageBarrierReplayPayload,
  ): Promise<boolean> {
    const requestSessionKey = options.sessionKey.value
    const text = payload.text
    const forkBeforeMessageId = payload.forkBeforeMessageId.trim()
    if (!requestSessionKey || !text || !forkBeforeMessageId || usageBarrierReplayInFlight) {
      return false
    }
    const invalidCoordinationIdentity = (value: string) => (
      value.length > 512 || /[\u0000-\u001f\u007f]/.test(value)
    )
    if (
      invalidCoordinationIdentity(requestSessionKey)
      || invalidCoordinationIdentity(forkBeforeMessageId)
    ) return false
    const replayCoordinationKey = await stableClientUuid(
      `usage-barrier-coordinate\0${canonicalSessionKey(requestSessionKey)}\0${forkBeforeMessageId}`,
    )
    if (!replayCoordinationKey) return false
    const stableClientRequestId = await stableClientUuid(
      `usage-barrier-request\0${replayCoordinationKey}`,
    )
    const stableMessageUuid = await stableClientUuid(
      `usage-barrier-message\0${replayCoordinationKey}`,
    )
    if (!stableClientRequestId || !stableMessageUuid) return false
    const stableClientMessageId = `local-${stableMessageUuid}`
    if (stableClientRequestId.length > 256 || stableClientMessageId.length > 256) return false
    const replayAnchorIsCurrent = () => {
      const anchor = options.messages.value.find(message => (
        message.role === 'user'
        && message.messageId === forkBeforeMessageId
      ))
      return Boolean(
        anchor
        && anchor.text === text
        && (anchor.attachments?.length ?? 0) === 0,
      )
    }
    const replayIsBlocked = (allowOwnedStream = false) => (
      options.sessionKey.value !== requestSessionKey
      || !replayAnchorIsCurrent()
      || Boolean(options.sendBlockedReason?.value)
      || Boolean(options.taskOwnership && !options.taskOwnership.hydrationResolved.value)
      || (!allowOwnedStream && options.stream.isStreaming.value)
      || hasAuthoritativeWork()
      || options.isCompactInFlightForCurrentSession()
      || responseHandoffBlocksCurrentSession()
      || Boolean(handoffRecoveryPromise)
      || options.hasPendingQueueWork?.() === true
      || options.pendingQueueOwnerContext.value?.sessionKey === requestSessionKey
    )
    if (replayIsBlocked()) return false

    if (options.validateActiveProjectBeforeSend) {
      if (await refreshedActiveProjectBlocksSend()) return false
    }
    if (replayIsBlocked()) return false

    if (usageBarrierReplayAttempt && !matchesRecoveredDraft(
      usageBarrierReplayAttempt,
      {
        requestSessionKey,
        text,
        attachments: [],
        intent: null,
        initialCollaborationMode: null,
        initialRoutingMode: null,
        forkBeforeMessageId,
        workspaceId: null,
      },
    )) return false

    usageBarrierReplayInFlight = true
    try {
      const outcome = await dispatchSend(text, {
        payload: {
          attachments: [],
          intent: null,
          forkBeforeMessageId,
          workspaceId: null,
          initialCollaborationMode: null,
          initialRoutingMode: null,
        },
        preserveComposer: true,
        retryAttempt: usageBarrierReplayAttempt,
        rememberRetryableAttempt: attempt => {
          usageBarrierReplayAttempt = attempt
        },
        acceptedVisibleReplay: { forkBeforeMessageId },
        suppressRejectedFailureMessage: true,
        includeEmptyAttachments: true,
        requirePreparedHandoff: true,
        replayCoordination: {
          key: replayCoordinationKey,
          clientRequestId: stableClientRequestId,
          clientMessageId: stableClientMessageId,
        },
        preDispatchGuard: stage => !replayIsBlocked(stage === 'before_rpc'),
      })
      if (outcome === 'accepted') usageBarrierReplayAttempt = null
      return outcome === 'accepted'
    } finally {
      usageBarrierReplayInFlight = false
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
    attempt.requiresIdempotentReplay = recovery.requiresIdempotentReplay
    recoveredAttempt = attempt
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
    // A first Stop can race durable ingress before chat.send returns a task id.
    // Keep that transaction latched until its ACK/reconcile so a double click
    // cannot widen the second request into legacy whole-session cancellation.
    if (acceptanceStopPending.value) return
    const handoffCanStop = responseHandoffBlocksCurrentSession()
    if (!(handoffCanStop || (options.canStop?.() ?? options.stream.isStreaming.value))) return
    const handoff = handoffCanStop ? activeResponseHandoff : null
    const acceptance = activeAcceptanceTransaction?.requestSessionKey === options.sessionKey.value
      ? activeAcceptanceTransaction
      : null
    const ownershipStopTarget = options.taskOwnership?.beginStop() || ''
    const rawStoppedTurnId = ownershipStopTarget || currentExpectedTurnId()
    const stoppedTurnId = rawStoppedTurnId
      && ![
        PENDING_STREAM_TASK_ID,
        FINISHED_STREAM_TASK_ID,
        STOPPED_STREAM_TASK_ID,
      ].includes(rawStoppedTurnId)
      ? rawStoppedTurnId
      : ''
    const taskAcceptancePending = Boolean(handoff || acceptance)
    const acceptanceOwnsStop = taskAcceptancePending && !stoppedTurnId
    if (!acceptanceOwnsStop && acceptance?.attempt) {
      // B's acceptance is still unknown while Stop precisely targets hydrated
      // A. Resolve B's idempotent receipt in the background, but do not copy
      // A's Stop intent onto it.
      acceptance.attempt.autoRecoverAcceptance = true
    }
    if (acceptanceOwnsStop && handoff) handoff.stoppedByUser = true
    if (acceptanceOwnsStop && acceptance) {
      acceptance.stoppedByUser = true
      if (acceptance.attempt) {
        acceptance.attempt.stopRequested = true
        acceptance.attempt.stopOwner = acceptance.id
        stoppedAcceptanceAttempts.set(
          acceptanceAttemptKey(acceptance.attempt),
          acceptance.attempt,
        )
        noteAcceptanceRecoveryChanged()
      }
      acceptanceStopOwner = acceptance.id
      if (acceptance.freshSendToken) acceptance.freshSendToken.stoppedByUser = true
    }
    const abortSessionKey = acceptanceOwnsStop
      ? handoff?.targetSessionKey
        || acceptance?.requestSessionKey
        || options.activeStreamSessionKey.value
        || options.sessionKey.value
      : stoppedTurnId
        ? options.activeStreamSessionKey.value || options.sessionKey.value
        : options.sessionKey.value
    if (acceptanceOwnsStop) {
      acceptanceStopPending.value = true
    }
    options.steerDelivery.markStopRequested(stoppedTurnId)
    const latestUserMessage = [...options.messages.value]
      .reverse()
      .find(message => message.role === 'user')
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
    }
    const abortParams: Record<string, string> = {
      sessionKey: abortSessionKey,
      source: 'webui_stop',
    }
    // Known turns and in-flight send acceptance are precise task Stops. The
    // no-id/no-acceptance case is the existing task-group Stop surface, which
    // intentionally retains legacy session-tree cancellation semantics.
    if (stoppedTurnId || taskAcceptancePending) abortParams.scope = 'task'
    if (stoppedTurnId) abortParams.taskId = stoppedTurnId
    options.rpc.call<{ aborted?: boolean, reason?: string }>('chat.abort', abortParams)
      .then((response) => {
        if (response?.aborted === true) {
          options.scheduleHistorySync()
          return
        }
        if (String(response?.reason || '').toLowerCase() === 'task_cancel_unknown') {
          options.scheduleHistorySync()
          void options.reconcileTaskOwnership?.()
          return
        }
        if (acceptanceOwnsStop) return
        options.taskOwnership?.clearStop(stoppedTurnId)
        if (handoff) handoff.stoppedByUser = false
        if (activeFreshSendToken !== null) activeFreshSendToken.stoppedByUser = false
        void options.reconcileTaskOwnership?.()
        reportAbortFailure([abortSessionKey])
      })
      .catch(() => {
        if (acceptanceOwnsStop) return
        options.taskOwnership?.clearStop(stoppedTurnId)
        if (handoff) handoff.stoppedByUser = false
        if (activeFreshSendToken !== null) activeFreshSendToken.stoppedByUser = false
        void options.reconcileTaskOwnership?.()
        reportAbortFailure([abortSessionKey])
      })
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
      || hasAuthoritativeWork()
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
    const hiddenInitialRoutingMode = initialRoutingModeForIntent(hiddenSessionIntent)
    if (hiddenSessionIntent) params.intent = hiddenSessionIntent
    if (hiddenInitialRoutingMode) params.initialRoutingMode = hiddenInitialRoutingMode
    if (displayText && displayText !== providerText) params.displayText = displayText
    params._source = chatSourceMetadata(options)

    // Hidden controls preserve the composer and render their own outbox-backed
    // bubble, but their acceptance/Stop identity is otherwise the same as an
    // ordinary send. Keep a request-owned attempt so a Stop racing this ACK can
    // retry an exact task-scoped abort without widening to the whole session.
    const attempt: SendAttempt = {
      clientRequestId: stableClientRequestId,
      clientMessageId,
      composerText: displayText,
      requestSessionKey,
      text: providerText,
      attachments: [],
      intent: hiddenSessionIntent,
      initialCollaborationMode: null,
      initialRoutingMode: hiddenInitialRoutingMode,
      forkBeforeMessageId: null,
      workspaceId: null,
      params,
      hiddenControl: true,
      acceptanceRpc: {
        method: 'chat.send',
        params: params as unknown as Record<string, unknown>,
      },
    }

    const wasStreaming = options.stream.isStreaming.value
    const freshSendToken = wasStreaming
      ? null
      : beginFreshStream(requestSessionKey, attempt)
    let responseHandoff: ResponseHandoffGate | null = null
    const acceptanceTransaction = beginAcceptanceTransaction(
      requestSessionKey,
      freshSendToken,
      attempt,
    )

    try {
      attempt.acceptanceInFlight = true
      const res = await options.rpc.call<ChatSendResponse>('chat.send', params)
      attempt.acceptanceResolved = true
      attempt.acceptedTaskId = acceptedTaskId(res)
      attempt.acceptedSessionKey = res?.sessionKey || requestSessionKey
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
      const accepted = noteAcceptedTask(res, requestSessionKey)
      const taskId = accepted.taskId
      const terminalStatus = terminalResponseStatus(res)
      const stoppedByUser = acceptanceTransaction.stoppedByUser
      const lostFreshStream = !wasStreaming
        && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)
      if (stoppedByUser || lostFreshStream) {
        const acceptedSessionKey = res?.sessionKey || requestSessionKey
        const stoppedTerminalIsCurrent = Boolean(
          stoppedByUser
          && terminalStatus
          && options.sessionKey.value === requestSessionKey
          && acceptedSessionKey === requestSessionKey,
        )
        if (stoppedByUser && (taskId || terminalStatus)) {
          clearAcceptanceStop(acceptanceTransaction)
        }
        if (stoppedTerminalIsCurrent) {
          bindAcceptedUserMessage(clientMessageId, res)
          handleTerminalResponse(res, freshSendToken, {
            finishFreshStream: !wasStreaming,
            forceFreshStream: true,
          })
          return hiddenDispatchResult(
            'accepted',
            'accepted',
            stableClientRequestId,
            requestSessionKey,
          )
        }
        if (stoppedByUser && taskId && options.sessionKey.value === requestSessionKey) {
          options.taskOwnership?.requestStop(taskId)
          bindAcceptedTask(taskId)
        }
        if (
          options.sessionKey.value === requestSessionKey
          && acceptedSessionKey === requestSessionKey
        ) {
          bindAcceptedUserMessage(clientMessageId, res)
        }
        if (stoppedByUser && taskId && attempt.stopRequested) {
          // Share the ordinary-send single-flight worker. If the first exact
          // abort response is lost or explicitly unknown, the same task id is
          // retried without replaying the hidden control or touching composer.
          void abortRecoveredAcceptedTask(attempt).then((resolved) => {
            if (!resolved && attempt.stopRequested) scheduleAcceptanceRecovery(attempt)
          })
        } else {
          abortStaleAcceptedTask(res, requestSessionKey, stoppedByUser)
        }
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
        if (accepted.renderTaskId) bindAcceptedTask(accepted.renderTaskId)
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
      const stoppedByUser = acceptanceTransaction.stoppedByUser
      if (stoppedByUser) {
        if (acceptedError?.terminalWithoutTask || accepted === false) {
          attempt.acceptanceResolved = true
          if (attempt.stopRequested) clearAttemptStop(attempt)
          clearAcceptanceStop(acceptanceTransaction)
        }
      }
      if (hasUnknownAcceptance(err)) {
        attempt.requiresIdempotentReplay = true
        // Stop can target an already-running A while this hidden B acceptance
        // remains unknown. Resolve B's receipt too, but only a request-owned
        // Stop may exact-abort B when its task id becomes known.
        if (attempt.stopRequested || attempt.autoRecoverAcceptance) {
          scheduleAcceptanceRecovery(attempt)
        }
        if (stoppedByUser) void options.reconcileTaskOwnership?.()
      } else if (accepted === false || acceptedError?.terminalWithoutTask) {
        attempt.acceptanceResolved = true
      }
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
      attempt.acceptanceInFlight = false
      finishAcceptanceTransaction(acceptanceTransaction)
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
    recoverResponseHandoffs,
    sendHiddenMetaPreflightConfirmation,
    sendUsageBarrierReplay,
    acceptanceRecoveryPendingForCurrentSession,
  }
}
