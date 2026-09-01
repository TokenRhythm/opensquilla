import { nextTick, toRaw, watch, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatRenderedMessage,
  ChatStreamTimelineItem,
} from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
import { resolveAssistantAnswer } from '@/utils/chat/assistantActivity'
import { turnOutcomePresentation } from '@/utils/chat/turnOutcome'
import {
  isUsageAccountingBarrierMessage,
  strictUsageBarrierRetryUserMessageIndex,
} from '@/utils/chat/usageAccountingFailure'
import { sanitizeAssistantPresentationSegments } from '@/utils/chat/silentSentinels'
import type { AssistantPresentationProvenance } from '@/utils/chat/silentSentinels'

export interface UseChatMessageActionsOptions {
  sessionKey: Ref<string>
  messages: Ref<ChatMessage[]>
  inputText: Ref<string>
  isStreaming: Ref<boolean>
  sanitizeCopyText: (text: string, opts?: {
    assistantBoundary?: boolean
    provenance?: AssistantPresentationProvenance
  }) => string
  stripTimePrefix: (text: string) => string
  autoResizeTextarea: () => void
  sendCurrentInput: () => void
  sendUsageBarrierReplay: (payload: {
    text: string
    forkBeforeMessageId: string
  }) => Promise<boolean>
  focusComposer: () => void
  pendingForkBeforeMessageId: Ref<string | null>
  aiGeneratedLabel?: () => string
  canDeliver?: () => boolean
  notifyDeliveryBlocked?: () => void
  /**
   * User-visible feedback when regenerate/edit cannot run because the anchor
   * user message has no durable server id yet (chat.send ack lost, or an
   * older gateway omitted the id). Without it the buttons look dead: the
   * only trace of the refusal would be a console warning.
   */
  notifyMessagePending?: () => void
  /**
   * User-visible feedback when edit is clicked while the assistant is still
   * streaming. The edit button is disabled in that state, but other entry
   * points (keyboard, future surfaces) must not fail silently either.
   */
  notifyEditBlocked?: () => void
}

interface EditRestorePoint {
  /** Session owner; restore points never cross a session boundary. */
  sessionKey: string
  /** The exact transcript array installed by this edit. */
  editingMessages: ChatMessage[]
  /** Shallow item identities installed by this edit. */
  editingMessageOwners: ChatMessage[]
  /** The transcript as it stood before edit truncated it. */
  messages: ChatMessage[]
  /** Whatever the composer held before edit overwrote it with the message. */
  inputText: string
  /** Fork owner that was active before this edit replaced it. */
  previousForkBeforeMessageId: string | null
  /** Ties the restore point to the edit that made it; see `cancelEdit`. */
  forkBeforeMessageId: string
}

export function useChatMessageActions(options: UseChatMessageActionsOptions) {
  let editRestorePoint: EditRestorePoint | null = null

  // Session transitions replace the transcript and composer domain. Retire the
  // old restore point synchronously so even an immediate switch back cannot
  // revive state captured before the boundary.
  watch(options.sessionKey, () => {
    editRestorePoint = null
  }, { flush: 'sync' })

  function copyableMessageText(message: ChatRenderedMessage): string {
    // User bubbles render the raw text with only the time prefix stripped, so
    // copy must match: the markdown sanitizers would truncate or strip literal
    // text (e.g. "<details>") that is visible on screen.
    if ((message.displayRole || message.role) === 'user') {
      return options.stripTimePrefix(message.text || '').trim()
    }
    const outcome = turnOutcomePresentation(message.turnOutcome)
    const answer = resolveAssistantAnswer(
      message,
      message.timelineItems ?? [],
      outcome === 'stopped' || outcome === 'interrupted' || message.interrupted
        ? 'interrupted'
        : outcome === 'timeout' || outcome === 'failed' || message.terminalFailure
          ? 'failed'
          : message.isStreaming
            ? 'working'
            : 'settled',
    )
    const provenance: AssistantPresentationProvenance = {
      inputMode: message.turnInputMode,
      runKind: message.turnRunKind,
    }
    // The same structurally proven PlanRun answer shown outside the collapsed
    // activity must also be what Copy returns. Otherwise the compact completed
    // state would silently copy the entire execution narration.
    if (
      answer.source === 'terminal-control-boundary'
      || answer.source === 'terminal-timeline-boundary'
    ) {
      return options.sanitizeCopyText(answer.text, { provenance })
    }
    // Canonical is the fail-open presentation used by the message body. Keep
    // its exact paragraph spacing instead of rebuilding it from timeline
    // chunks, which can insert separators that are not visible on screen.
    if (answer.source === 'canonical') {
      return options.sanitizeCopyText(answer.text, { provenance })
    }
    if (answer.source === 'explicit-no-answer') return ''
    // The raw message text can be absent in older history, so rebuild only
    // that source-less compatibility case from the available segments while
    // applying the same provenance-aware silent-reply projection as the body.
    const segmentTexts = sanitizeAssistantPresentationSegments(
      (message.timelineItems || [])
        .filter((item): item is Extract<ChatStreamTimelineItem, { type: 'text' }> => item.type === 'text')
        .map(item => item.rawText || ''),
      provenance,
    )
      .map(text => options.sanitizeCopyText(text, { assistantBoundary: false }))
      .filter(Boolean)
    if (segmentTexts.length) return segmentTexts.join('\n\n')
    return options.sanitizeCopyText(message.text || '', { provenance })
  }

  async function copyMessage(msg: ChatRenderedMessage): Promise<boolean> {
    try {
      const text = copyableMessageText(msg)
      if (!text) return false
      const isAssistant = (msg.displayRole || msg.role) === 'assistant'
      const label = isAssistant ? options.aiGeneratedLabel?.().trim() : ''
      await copyTextWithFallback(label && text ? `${text}\n\n${label}` : text)
      return true
    } catch (err) {
      console.warn('Copy failed:', err instanceof Error ? err.message : String(err))
      return false
    }
  }

  function sourceMessageIndex(message: ChatRenderedMessage): number {
    if (typeof message.sourceIndex === 'number' && message.sourceIndex >= 0) {
      return message.sourceIndex
    }
    if (message.messageId) {
      return options.messages.value.findIndex(msg => msg.messageId === message.messageId)
    }
    return -1
  }

  function previousUserMessageIndex(beforeIndex: number): number {
    const startIndex = beforeIndex >= 0 ? beforeIndex - 1 : options.messages.value.length - 1
    for (let i = startIndex; i >= 0; i--) {
      if (options.messages.value[i]?.role === 'user') return i
    }
    return -1
  }

  function regenerateMessage(message: ChatRenderedMessage): boolean | Promise<boolean> {
    if (options.isStreaming.value) {
      console.warn('Wait for the current response to finish')
      return false
    }
    const usageBarrierRetry = isUsageAccountingBarrierMessage(message)
    const assistantIndex = sourceMessageIndex(message)
    const usageBarrierUserIndex = strictUsageBarrierRetryUserMessageIndex(
      options.messages.value,
      assistantIndex,
      message,
    )
    if (usageBarrierRetry && usageBarrierUserIndex < 0) {
      console.warn('Usage accounting retry is missing a safe replay proof or primary user')
      return false
    }
    const userMsgIndex = usageBarrierRetry
      ? usageBarrierUserIndex
      : previousUserMessageIndex(assistantIndex)
    if (userMsgIndex < 0) {
      console.warn('No previous message to regenerate')
      return false
    }

    const userMessage = options.messages.value[userMsgIndex]
    const forkBeforeMessageId = userMessage?.messageId || ''
    if (!forkBeforeMessageId) {
      console.warn('Wait for the message to finish saving before regenerating')
      options.notifyMessagePending?.()
      return false
    }
    const userText = userMessage?.text || ''
    if (usageBarrierRetry) {
      return options.sendUsageBarrierReplay({
        text: userText,
        forkBeforeMessageId,
      })
    }
    // Ordinary regenerate remains composer-backed. Fail closed before any of
    // its local mutations when live delivery cannot receive the resulting turn.
    if (options.canDeliver && !options.canDeliver()) {
      options.notifyDeliveryBlocked?.()
      return false
    }
    options.pendingForkBeforeMessageId.value = forkBeforeMessageId
    options.messages.value = options.messages.value.slice(0, userMsgIndex)
    options.inputText.value = userText
    options.autoResizeTextarea()
    nextTick(() => options.sendCurrentInput())
    return true
  }

  function editMessage(message: ChatRenderedMessage) {
    if (options.isStreaming.value) {
      console.warn('Wait for the current response to finish')
      options.notifyEditBlocked?.()
      return
    }
    const msgIndex = sourceMessageIndex(message)
    if (msgIndex < 0) return
    if (options.messages.value[msgIndex]?.role !== 'user') return
    const sourceMessage = options.messages.value[msgIndex]
    const forkBeforeMessageId = sourceMessage?.messageId || ''
    if (!forkBeforeMessageId) {
      console.warn('Wait for the message to finish saving before editing')
      options.notifyMessagePending?.()
      return
    }
    const text = sourceMessage.text || ''
    const editingMessages = options.messages.value.slice(0, msgIndex)
    // Everything below this line is undone by `cancelEdit`. Entering edit mode
    // is not a decision the user has confirmed — the transcript shrinks to
    // nothing on the first click, and until #1372 there was no way back:
    // Escape cleared the composer and left the empty state on screen, which
    // reads as the conversation having been deleted.
    editRestorePoint = {
      sessionKey: options.sessionKey.value,
      editingMessages,
      editingMessageOwners: editingMessages.map(message => toRaw(message)),
      messages: options.messages.value,
      inputText: options.inputText.value,
      previousForkBeforeMessageId: options.pendingForkBeforeMessageId.value,
      forkBeforeMessageId,
    }
    options.pendingForkBeforeMessageId.value = forkBeforeMessageId
    options.messages.value = editingMessages
    options.inputText.value = text
    options.autoResizeTextarea()
    options.focusComposer()
  }

  /**
   * Put the transcript and the draft back, if an edit is still uncommitted.
   *
   * Returns whether anything was restored, so a caller can tell an edit
   * cancellation apart from an ordinary Escape and act on only one of them.
   *
   * The restore point is only honoured while `pendingForkBeforeMessageId` still
   * holds the id the edit set. Sending consumes that id, and a second edit
   * replaces it; in both cases the truncation has been made real by something
   * the user did mean, and resurrecting the old array would put back messages
   * the server no longer has.
   */
  function cancelEdit(): boolean {
    const restore = editRestorePoint
    if (!restore) return false
    editRestorePoint = null
    const currentMessages = options.messages.value
    if (
      options.sessionKey.value !== restore.sessionKey
      || toRaw(currentMessages) !== restore.editingMessages
      || currentMessages.length !== restore.editingMessageOwners.length
      || currentMessages.some(
        (message, index) => toRaw(message) !== restore.editingMessageOwners[index],
      )
      || options.pendingForkBeforeMessageId.value !== restore.forkBeforeMessageId
    ) {
      return false
    }
    options.pendingForkBeforeMessageId.value = restore.previousForkBeforeMessageId
    options.messages.value = restore.messages
    options.inputText.value = restore.inputText
    options.autoResizeTextarea()
    return true
  }

  return {
    copyMessage,
    regenerateMessage,
    editMessage,
    cancelEdit,
  }
}
