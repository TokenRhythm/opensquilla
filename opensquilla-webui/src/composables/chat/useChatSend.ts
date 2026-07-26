import type { Ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { RpcClientError } from '@/lib/rpc'
import type { Attachment, ChatMessage } from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { SandboxRunMode } from '@/types/sandbox'
import { normalizeSandboxRunMode } from '@/types/sandbox'
import type {
  ChatSendParams,
  ChatSendResponse,
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
import { createClientMessageId, createClientRequestId } from '@/utils/chat/messageIdentity'
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
  forkBeforeMessageId: string | null
  params: ChatSendParams
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
    forkBeforeMessageId: string | null
  },
): boolean {
  return (
    attempt.requestSessionKey === input.requestSessionKey &&
    attempt.text === input.text &&
    attempt.intent === input.intent &&
    attempt.forkBeforeMessageId === input.forkBeforeMessageId &&
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
  pendingSessionIntent: Ref<string | null>
  pendingForkBeforeMessageId: Ref<string | null>
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
  prepareAttachmentsForSend?: (options?: { isCurrent?: () => boolean }) => Promise<boolean>
  enqueuePendingInput: (text: string, owner?: PendingQueueOwner) => boolean
  enqueueHiddenControl?: (
    item: { text: string; displayText: string },
    owner?: PendingQueueOwner,
  ) => boolean
  popAllPendingIntoComposer: () => boolean
  executeSlashCommand: (text: string) => Promise<boolean>
  closeSlashMenu: () => void
  autoResizeTextarea: () => void
  scrollToBottom: () => void
}

export function useChatSend(options: UseChatSendOptions) {
  const { pushToast } = useToasts()
  let activeFreshSendToken: FreshSendToken | null = null
  let activeResponseHandoff: ResponseHandoffGate | null = null
  let recoveredAttempt: SendAttempt | null = null

  function modelImageSendBlocked(attachments: readonly Attachment[]): boolean {
    if (!hasSendableModelInputImageAttachment(attachments)) return false
    return options.modelRoutingSettingsBusy.value
      || options.modelRoutingMode.value === 'llm_ensemble'
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

  async function onSend() {
    let text = options.inputText.value.trim()
    let sendableAttachments = options.pendingAttachments.value.filter(isSendableAttachment)
    let hasPayload = text || sendableAttachments.length > 0
    let isLiteralSlash = false
    const handoffInFlight = responseHandoffBlocksCurrentSession()

    if (options.hasPendingAttachmentWork()) {
      pushToast(i18n.global.t('chat.toast.waitAttachments'), { tone: 'info' })
      return
    }

    if (text.startsWith('//')) {
      isLiteralSlash = true
      text = text.slice(1)
      sendableAttachments = options.pendingAttachments.value.filter(isSendableAttachment)
      hasPayload = text || sendableAttachments.length > 0
    }

    // Retry an ambiguous prior send with its exact original queue semantics,
    // even if the ambient stream state changed while the error was visible.
    // Deriving steer/followup again here would create a new fingerprint and
    // could duplicate a turn that the gateway already accepted.
    if (
      !handoffInFlight &&
      recoveredAttempt &&
      matchesRecoveredDraft(recoveredAttempt, {
        requestSessionKey: options.sessionKey.value,
        text,
        attachments: sendableAttachments,
        intent: options.pendingSessionIntent.value,
        forkBeforeMessageId: options.pendingForkBeforeMessageId.value,
      })
    ) {
      await dispatchSend(text, {
        composerText: options.inputText.value,
        queueMode: recoveredAttempt.queueMode,
      })
      return
    }

    const compactInFlight = options.isCompactInFlightForCurrentSession()
    if (options.stream.isStreaming.value || compactInFlight || handoffInFlight) {
      if (!isLiteralSlash && text.startsWith('/')) {
        pushToast(i18n.global.t(
          compactInFlight ? 'chat.toast.waitCompactionBeforeCommand' : 'chat.toast.waitResponseBeforeCommand',
          { command: text.split(/\s+/, 1)[0] },
        ), { tone: 'info' })
        return
      }
      if (!hasPayload) return
      // Ensemble is text-only in P0. Do not consume the draft into Queue or
      // Steer while the selected routing mode cannot accept its image blocks.
      if (modelImageSendBlocked(sendableAttachments)) return
      // Steer injects into the active run right away; compaction cannot be
      // steered, so those sends still queue until it finishes.
      if (options.busySendMode.value === 'steer' && !compactInFlight && !handoffInFlight) {
        await dispatchSend(text, {
          composerText: options.inputText.value,
          queueMode: 'steer',
        })
        return
      }
      // Surface a full queue instead of silently dropping the send: the draft is
      // preserved (enqueue returns false before clearing the composer).
      if (!options.enqueuePendingInput(text, pendingQueueOwner())) {
        pushToast(i18n.global.t('chat.toast.queueFull'), { tone: 'info' })
      }
      return
    }

    if (!isLiteralSlash && text.startsWith('/')) {
      const handled = await options.executeSlashCommand(text)
      if (handled) return
    }

    if (!hasPayload || !options.sessionKey.value) return

    await dispatchSend(text, { composerText: options.inputText.value })
  }

  async function dispatchSend(
    text: string,
    sendOpts?: { composerText?: string; queueMode?: 'steer' },
  ) {
    const requestSessionKey = options.sessionKey.value
    if (!requestSessionKey) return
    const initialSendableAttachments = options.pendingAttachments.value.filter(isSendableAttachment)
    // This is deliberately before optimistic rendering, composer clearing,
    // stream state, and chat.send. A blocked draft remains exactly editable.
    if (modelImageSendBlocked(initialSendableAttachments)) return
    const retryCandidate = recoveredAttempt
    const isRecoveredRetry = Boolean(
      retryCandidate &&
      matchesRecoveredDraft(retryCandidate, {
        requestSessionKey,
        text,
        attachments: initialSendableAttachments,
        intent: options.pendingSessionIntent.value,
        forkBeforeMessageId: options.pendingForkBeforeMessageId.value,
      }) &&
      retryCandidate.queueMode === sendOpts?.queueMode,
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
      })
      if (!ready) return
      if (options.sessionKey.value !== requestSessionKey) return
    }
    const attachmentsToSend = retryAttempt?.attachments || options.pendingAttachments.value.filter((a): a is SendableAttachment => sendAttachmentIds.has(a.local_id) && isSendableAttachment(a))
    // Routing can change while an expiring staged upload is refreshed. Recheck
    // the authoritative live state before any visible or RPC mutation.
    if (modelImageSendBlocked(attachmentsToSend)) return
    const attachmentsToKeep = options.pendingAttachments.value.filter(a => !sendAttachmentIds.has(a.local_id) || !isSendableAttachment(a))
    if (!text && attachmentsToSend.length === 0) return

    options.aborted.value = false
    options.closeSlashMenu()
    recordSessionNavigationDiag('send.start', {
      requestSession: requestSessionKey,
      current: requestSessionKey,
    })

    const userText = text
    const intent = options.pendingSessionIntent.value
    const forkBeforeMessageId = options.pendingForkBeforeMessageId.value
    let attempt = retryAttempt
    if (!attempt) {
      const clientMessageId = createClientMessageId()
      const params: ChatSendParams = {
        clientRequestId: createClientRequestId(),
        clientMessageId,
        message: text || 'Describe these attachments',
        sessionKey: requestSessionKey,
      }
      if (sendOpts?.queueMode) params.queueMode = sendOpts.queueMode
      params._source = chatSourceMetadata(options)
      if (intent) params.intent = intent
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
        forkBeforeMessageId,
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
    recoveredAttempt = null

    options.inputText.value = ''
    options.autoResizeTextarea()
    options.pendingAttachments.value = attachmentsToKeep
    if (options.pendingSessionIntent.value === intent) options.pendingSessionIntent.value = null
    if (options.pendingForkBeforeMessageId.value === forkBeforeMessageId) {
      options.pendingForkBeforeMessageId.value = null
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
        return
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
    } catch (err: unknown) {
      const acceptedError = acceptedErrorInfo(err)
      const acceptedSessionKey = acceptedError?.sessionKey || requestSessionKey
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
        return
      }
      if (acceptedError && options.sessionKey.value === requestSessionKey) {
        bindUserMessageId(attempt.clientMessageId, acceptedError.messageId)
        options.scheduleHistorySync()
      }
      if (options.sessionKey.value !== requestSessionKey) {
        recordSessionNavigationDiag('send.error.stale', {
          requestSession: requestSessionKey,
          current: options.sessionKey.value,
          reason: errorMessage(err),
        })
        return
      }
      if (!wasStreaming && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)) {
        return
      }
      if (!wasStreaming) {
        if (activeFreshSendToken === freshSendToken) {
          activeFreshSendToken = null
        }
        options.activeStreamTaskId.value = ''
        options.activeStreamSessionKey.value = ''
        options.stream.endStreaming()
      }
      if (shouldRestoreSendAttempt(err)) restoreSendAttempt(attempt)
      options.messages.value.push({
        role: 'error',
        text: sendFailureMessage(err),
        errorCode: errorCode(err),
        ts: new Date().toISOString(),
      })
    } finally {
      finishResponseHandoff(responseHandoff)
    }
  }

  function restoreSendAttempt(attempt: SendAttempt) {
    const currentText = options.inputText.value
    if (!currentText) {
      options.inputText.value = attempt.composerText
    } else if (currentText !== attempt.composerText) {
      options.inputText.value = [attempt.composerText, currentText].filter(Boolean).join('\n')
    }
    restoreSendableAttachments(attempt.attachments)
    if (!options.pendingSessionIntent.value) options.pendingSessionIntent.value = attempt.intent
    if (!options.pendingForkBeforeMessageId.value) {
      options.pendingForkBeforeMessageId.value = attempt.forkBeforeMessageId
    }
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
    options.activeStreamTaskId.value = STOPPED_STREAM_TASK_ID
    // Be honest if the abort can't reach the gateway (e.g. the socket dropped):
    // we still tear the local stream down for responsiveness, but the user must
    // know the server-side run may keep going rather than trust a false "stopped".
    const abortParams: Record<string, string> = { sessionKey: abortSessionKey, source: 'webui_stop' }
    options.rpc.call('chat.abort', abortParams).catch(() => {
      reportAbortFailure([abortSessionKey])
    })
    options.stream.endStreaming({ reason: 'aborted' })
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
  async function dispatchHiddenSend(providerText: string, displayText: string) {
    const requestSessionKey = options.sessionKey.value
    if (!requestSessionKey || !providerText) return
    const compactInFlight = options.isCompactInFlightForCurrentSession()
    const handoffInFlight = responseHandoffBlocksCurrentSession()
    if (options.stream.isStreaming.value || compactInFlight || handoffInFlight) {
      options.enqueueHiddenControl?.(
        { text: providerText, displayText },
        pendingQueueOwner(),
      )
      return
    }

    options.aborted.value = false
    recordSessionNavigationDiag('hiddenSend.start', {
      requestSession: requestSessionKey,
      current: requestSessionKey,
    })
    // Show the visible confirmation as a user bubble (NOT the marker text).
    const now = new Date().toISOString()
    const clientMessageId = createClientMessageId()
    if (displayText) {
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
      clientRequestId: createClientRequestId(),
      clientMessageId,
      message: providerText,
      sessionKey: requestSessionKey,
    }
    if (displayText && displayText !== providerText) params.displayText = displayText
    params._source = chatSourceMetadata(options)

    const wasStreaming = options.stream.isStreaming.value
    const freshSendToken = wasStreaming
      ? null
      : beginFreshStream(requestSessionKey)
    let responseHandoff: ResponseHandoffGate | null = null

    try {
      const res = await options.rpc.call<ChatSendResponse>('chat.send', params)
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
        return
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
    } catch (err: unknown) {
      const acceptedError = acceptedErrorInfo(err)
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
        return
      }
      if (acceptedError && options.sessionKey.value === requestSessionKey) {
        bindUserMessageId(clientMessageId, acceptedError.messageId)
        options.scheduleHistorySync()
      }
      if (options.sessionKey.value !== requestSessionKey) {
        recordSessionNavigationDiag('hiddenSend.error.stale', {
          requestSession: requestSessionKey,
          current: options.sessionKey.value,
          reason: errorMessage(err),
        })
        return
      }
      if (!wasStreaming && !freshSendStillOwnsStream(freshSendToken, requestSessionKey)) {
        return
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
    } finally {
      finishResponseHandoff(responseHandoff)
    }
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
    dispatchHiddenSend,
    sendHiddenMetaPreflightConfirmation,
  }
}
