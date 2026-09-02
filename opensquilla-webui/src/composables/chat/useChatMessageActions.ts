import { nextTick, ref, toRaw, watch, type Ref } from 'vue'
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
  /** Hold receipt/history reconciliation while an exact Edit snapshot is active. */
  onEditStarted?: () => void
  /** Release deferred receipt/history reconciliation after Edit leaves ownership. */
  onEditSettled?: () => void
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
  /** The edit that was active before this one, for layered Escape restores. */
  previousRestorePoint: EditRestorePoint | null
}

export function useChatMessageActions(options: UseChatMessageActionsOptions) {
  let editRestorePoint: EditRestorePoint | null = null
  const editGeneration = ref(0)
  const editActive = ref(false)

  // Session transitions replace the transcript and composer domain. Retire the
  // old restore point synchronously so even an immediate switch back cannot
  // revive state captured before the boundary.
  watch(options.sessionKey, () => {
    editRestorePoint = null
    editActive.value = false
    editGeneration.value += 1
  }, { flush: 'sync' })

  function restoreOwnsCurrentSessionAndFork(restore: EditRestorePoint): boolean {
    return options.sessionKey.value === restore.sessionKey
      && options.pendingForkBeforeMessageId.value === restore.forkBeforeMessageId
  }

  function restoreOwnsCurrentTranscript(restore: EditRestorePoint): boolean {
    const currentMessages = options.messages.value
    return toRaw(currentMessages) === restore.editingMessages
      && currentMessages.length === restore.editingMessageOwners.length
      && currentMessages.every(
        (message, index) => toRaw(message) === restore.editingMessageOwners[index],
      )
  }

  function retireOwnedEdit(restore: EditRestorePoint): void {
    editRestorePoint = null
    editActive.value = false
    editGeneration.value += 1
    if (
      options.sessionKey.value === restore.sessionKey
      && options.pendingForkBeforeMessageId.value === restore.forkBeforeMessageId
    ) {
      options.pendingForkBeforeMessageId.value = null
    }
  }

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
    if (editRestorePoint) {
      // Regenerate and edit both replace the visible branch, but only edit has
      // an Escape restore frame. Let the user cancel or send that edit first;
      // otherwise regenerate would replace its fork and orphan the frame.
      console.warn('Finish or cancel the current message edit before regenerating')
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
    const previousRestore = editRestorePoint
    if (
      previousRestore
      && (
        !restoreOwnsCurrentSessionAndFork(previousRestore)
        || !restoreOwnsCurrentTranscript(previousRestore)
      )
    ) {
      // Never hang a new edit from a stale lower frame. In particular, an
      // authoritative history replacement must not leave its old fork anchor
      // underneath the new restore point, where a later Escape could revive it.
      retireOwnedEdit(previousRestore)
    }
    if (!editRestorePoint && options.pendingForkBeforeMessageId.value) {
      // A regenerate (or another branch owner) already owns this composer.
      // Replacing its fork without a corresponding restore frame would make
      // Escape unable to return to either operation coherently.
      console.warn('Finish the current branched draft before editing another message')
      return
    }
    const startsEditIsolation = editRestorePoint === null
    editGeneration.value += 1
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
      previousRestorePoint: editRestorePoint,
    }
    if (startsEditIsolation) {
      editActive.value = true
      options.onEditStarted?.()
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
   * Returns whether Escape handled an edit, including retiring an edit whose
   * transcript was authoritatively replaced. The latter must consume Escape so
   * the replacement owner's draft is not cleared by the ordinary shortcut.
   *
   * The top restore point is only honoured while
   * `pendingForkBeforeMessageId` still holds the id that edit set. Sending
   * consumes that id and retires the whole stack; a nested edit instead becomes
   * the new top and Escape returns one layer at a time.
   */
  function cancelEdit(): boolean {
    const restore = editRestorePoint
    if (!restore) return false
    if (!restoreOwnsCurrentSessionAndFork(restore)) {
      // The fork was consumed or replaced by another action. Drop every lower
      // frame without touching the new owner or resurrecting an older branch.
      editRestorePoint = null
      editActive.value = false
      editGeneration.value += 1
      options.onEditSettled?.()
      return false
    }
    if (!restoreOwnsCurrentTranscript(restore)) {
      // A same-session history refresh can replace the array while an edit-owned
      // send is awaiting preflight. The authoritative transcript must win, but
      // the abandoned edit must not leave either that send generation or its
      // fork anchor live for the next ordinary draft.
      retireOwnedEdit(restore)
      options.onEditSettled?.()
      return true
    }
    editRestorePoint = restore.previousRestorePoint
    editActive.value = editRestorePoint !== null
    editGeneration.value += 1
    options.pendingForkBeforeMessageId.value = restore.previousForkBeforeMessageId
    options.messages.value = restore.messages
    options.inputText.value = restore.inputText
    options.autoResizeTextarea()
    if (!editRestorePoint) options.onEditSettled?.()
    return true
  }

  /** Retire the matching restore frame after Gateway acceptance commits it. */
  function commitEdit(generation: number): boolean {
    if (editGeneration.value !== generation) return false
    if (!editRestorePoint) return true
    editRestorePoint = null
    editActive.value = false
    editGeneration.value += 1
    options.onEditSettled?.()
    return true
  }

  /**
   * Revalidate the uncommitted edit immediately before a send mutates state.
   * Generation alone cannot detect a same-session history refresh because it
   * happens outside this composable.
   */
  function validateEditOwner(generation: number): boolean {
    if (editGeneration.value !== generation) return false
    const restore = editRestorePoint
    if (!restore) return true
    if (!restoreOwnsCurrentSessionAndFork(restore)) {
      editRestorePoint = null
      editActive.value = false
      editGeneration.value += 1
      options.onEditSettled?.()
      return false
    }
    if (restoreOwnsCurrentTranscript(restore)) return true
    retireOwnedEdit(restore)
    options.onEditSettled?.()
    return false
  }

  /**
   * A definitely rejected send may leave only its own optimistic/error rows in
   * the edit-owned transcript. Adopt those exact identities so Escape can still
   * restore the pre-edit conversation. Arbitrary suffixes are never accepted.
   */
  function adoptRejectedEditRows(
    generation: number,
    rows: readonly ChatMessage[],
  ): boolean {
    if (editGeneration.value !== generation || rows.length === 0) return false
    const restore = editRestorePoint
    if (!restore) return false
    if (!restoreOwnsCurrentSessionAndFork(restore)) {
      editRestorePoint = null
      editActive.value = false
      editGeneration.value += 1
      options.onEditSettled?.()
      return false
    }
    const currentMessages = options.messages.value
    const expectedLength = restore.editingMessageOwners.length + rows.length
    const ownsPrefix = toRaw(currentMessages) === restore.editingMessages
      && currentMessages.length === expectedLength
      && restore.editingMessageOwners.every(
        (message, index) => toRaw(currentMessages[index]) === message,
      )
    const ownsSuffix = rows.every((message, index) => (
      toRaw(currentMessages[restore.editingMessageOwners.length + index]) === toRaw(message)
    ))
    if (!ownsPrefix || !ownsSuffix) {
      retireOwnedEdit(restore)
      options.onEditSettled?.()
      return false
    }
    restore.editingMessageOwners.push(...rows.map(message => toRaw(message)))
    return true
  }

  return {
    copyMessage,
    regenerateMessage,
    editMessage,
    cancelEdit,
    commitEdit,
    validateEditOwner,
    adoptRejectedEditRows,
    editGeneration,
    editActive,
  }
}
