import { copySelectedSkills, sameSelectedSkills, type SelectedSkillRef } from '@/types/selectedSkills'
import { copyLocalPathReferences } from '@/types/localPathReferences'
import { getCurrentScope, nextTick, onScopeDispose, watch, type Ref } from 'vue'
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
import { messageContentIdentity, needsCompleteMessageText, type ReadMessageText } from '@/utils/chat/historyMessageContent'

export interface UseChatMessageActionsOptions {
  messages: Ref<ChatMessage[]>
  inputText: Ref<string>
  localPathReferences?: Ref<string[]>
  restoreInput?: (text: string, paths?: readonly string[]) => void
  selectedSkills?: Ref<SelectedSkillRef[]>
  isStreaming: Ref<boolean>
  sessionIdentity?: () => string
  readMessageText?: ReadMessageText
  sanitizeCopyText: (text: string, opts?: {
    assistantBoundary?: boolean
    provenance?: AssistantPresentationProvenance
  }) => string
  stripTimePrefix: (text: string) => string
  autoResizeTextarea: () => void
  sendCurrentInput: () => void
  sendUsageBarrierReplay: (payload: {
    selectedSkills?: SelectedSkillRef[]
    localPathReferences?: string[]
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
  /** The transcript as it stood before edit truncated it. */
  messages: ChatMessage[]
  /** Whatever the composer held before edit overwrote it with the message. */
  inputText: string
  /** What edit put in the composer, so cancel can tell it apart from newer text. */
  editedText: string
  localPathReferences: string[]
  editedLocalPathReferences: string[]
  selectedSkills: SelectedSkillRef[]
  editedSkills: SelectedSkillRef[]
  /** Ties the restore point to the edit that made it; see `cancelEdit`. */
  forkBeforeMessageId: string
}

export function useChatMessageActions(options: UseChatMessageActionsOptions) {
  let editRestorePoint: EditRestorePoint | null = null
  let actionGeneration = 0
  let pendingRead: AbortController | null = null

  function cancelPendingAction(): boolean {
    const wasPending = pendingRead !== null
    actionGeneration += 1
    pendingRead?.abort()
    pendingRead = null
    return wasPending
  }

  function restoreInput(text: string, paths?: readonly string[]) {
    if (options.restoreInput) options.restoreInput(text, paths)
    else options.inputText.value = text
  }

  function discardEditRestorePoint() {
    cancelPendingAction()
    editRestorePoint = null
  }

  if (options.sessionIdentity) watch(options.sessionIdentity, discardEditRestorePoint, { flush: 'sync' })
  if (getCurrentScope()) onScopeDispose(discardEditRestorePoint)

  function withCompleteText(
    source: ChatMessage,
    apply: (text: string, generation: number) => boolean | Promise<boolean>,
  ): boolean | Promise<boolean> {
    cancelPendingAction()
    const generation = actionGeneration
    if (!needsCompleteMessageText(source)) return apply(source.text || '', generation)
    if (!options.readMessageText) return false
    const transcript = options.messages.value
    const rows = transcript.slice()
    const identity = messageContentIdentity(source)
    const session = options.sessionIdentity?.()
    const draft = options.inputText.value
    const paths = JSON.stringify(options.localPathReferences?.value)
    const skills = JSON.stringify(options.selectedSkills?.value)
    const fork = options.pendingForkBeforeMessageId.value
    const current = () => generation === actionGeneration
      && session === options.sessionIdentity?.()
      && transcript === options.messages.value && identity === messageContentIdentity(source)
      && transcript.length === rows.length && transcript.every((row, index) => row === rows[index])
      && draft === options.inputText.value && paths === JSON.stringify(options.localPathReferences?.value)
      && skills === JSON.stringify(options.selectedSkills?.value) && fork === options.pendingForkBeforeMessageId.value
      && !options.isStreaming.value
    const controller = new AbortController()
    pendingRead = controller
    return options.readMessageText({ ...source, contentRef: source.contentRef && { ...source.contentRef } }, controller.signal)
      .then(text => current() && !controller.signal.aborted ? apply(text, generation) : false)
      .catch(error => {
        if (!controller.signal.aborted) console.warn('History message read failed:', error instanceof Error ? error.message : String(error))
        return false
      })
      .finally(() => { if (pendingRead === controller) pendingRead = null })
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
      answer.source === 'explicit-presentation'
      || answer.source === 'terminal-control-boundary'
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
    if (typeof message.sourceIndex === 'number' && message.sourceIndex >= 0
      && (!message.messageId || options.messages.value[message.sourceIndex]?.messageId === message.messageId)) {
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
    if (assistantIndex < 0 && (message.messageId || message.sourceIndex !== undefined)) return false
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
    return withCompleteText(userMessage!, (userText, generation) => {
      if (usageBarrierRetry) {
        return options.sendUsageBarrierReplay({
          text: userText,
          ...(userMessage?.localPathReferences?.length ? { localPathReferences: copyLocalPathReferences(userMessage.localPathReferences, userText) } : {}),
          ...(userMessage?.selectedSkills?.length ? { selectedSkills: copySelectedSkills(userMessage.selectedSkills) } : {}),
          forkBeforeMessageId,
        })
      }
      // Ordinary regenerate remains composer-backed. Fail closed before any of
      // its local mutations when live delivery cannot receive the resulting turn.
      if (options.canDeliver && !options.canDeliver()) {
        options.notifyDeliveryBlocked?.()
        return false
      }
      editRestorePoint = null
      options.pendingForkBeforeMessageId.value = forkBeforeMessageId
      options.messages.value = options.messages.value.slice(0, userMsgIndex)
      if (options.selectedSkills) options.selectedSkills.value = copySelectedSkills(userMessage?.selectedSkills)
      restoreInput(userText, userMessage?.localPathReferences)
      options.autoResizeTextarea()
      const transcript = options.messages.value
      const session = options.sessionIdentity?.()
      const paths = JSON.stringify(options.localPathReferences?.value)
      const skills = JSON.stringify(options.selectedSkills?.value)
      nextTick(() => {
        if (generation !== actionGeneration || session !== options.sessionIdentity?.()
          || transcript !== options.messages.value || options.inputText.value !== userText
          || paths !== JSON.stringify(options.localPathReferences?.value) || skills !== JSON.stringify(options.selectedSkills?.value)
          || options.pendingForkBeforeMessageId.value !== forkBeforeMessageId
          || options.isStreaming.value || (options.canDeliver && !options.canDeliver())) return
        options.sendCurrentInput()
      })
      return true
    })
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
    return withCompleteText(sourceMessage, text => {
      // Everything below this line is undone by `cancelEdit`. Entering edit mode
      // is not a decision the user has confirmed — the transcript shrinks to
      // nothing on the first click, and until #1372 there was no way back:
      // Escape cleared the composer and left the empty state on screen, which
      // reads as the conversation having been deleted.
      const previous = editRestorePoint
      const continuesEdit = previous
        && options.pendingForkBeforeMessageId.value === previous.forkBeforeMessageId
      editRestorePoint = {
        // Choosing an earlier message while editing is still uncommitted. Keep
        // the complete transcript and draft from before the first edit.
        messages: continuesEdit ? previous.messages : options.messages.value,
        inputText: continuesEdit ? previous.inputText : options.inputText.value,
        editedText: text,
        localPathReferences: continuesEdit ? previous.localPathReferences : copyLocalPathReferences(options.localPathReferences?.value, options.inputText.value),
        editedLocalPathReferences: copyLocalPathReferences(sourceMessage.localPathReferences, text),
        selectedSkills: continuesEdit ? previous.selectedSkills : copySelectedSkills(options.selectedSkills?.value),
        editedSkills: copySelectedSkills(sourceMessage?.selectedSkills),
        forkBeforeMessageId,
      }
      options.pendingForkBeforeMessageId.value = forkBeforeMessageId
      options.messages.value = options.messages.value.slice(0, msgIndex)
      if (options.selectedSkills) options.selectedSkills.value = copySelectedSkills(sourceMessage?.selectedSkills)
      restoreInput(text, sourceMessage.localPathReferences)
      options.autoResizeTextarea()
      options.focusComposer()
      return true
    })
  }

  /**
   * Put the transcript and the draft back, if an edit is still uncommitted.
   *
   * Returns whether anything was restored, so a caller can tell an edit
   * cancellation apart from an ordinary Escape and act on only one of them.
   *
   * The restore point is only honoured while `pendingForkBeforeMessageId` still
   * holds the id the latest edit set. Sending consumes that id before admission;
   * a rejected send may restore it, so retain the point until cancellation or
   * session navigation. A second unsubmitted edit keeps the original snapshot.
   */
  function cancelEdit(): boolean {
    const cancelledRead = cancelPendingAction()
    const restore = editRestorePoint
    if (!restore || options.isStreaming.value) return cancelledRead
    if (options.pendingForkBeforeMessageId.value !== restore.forkBeforeMessageId) {
      // Drifted, so there is nothing safe to restore — but the point stays.
      // Escape now consults this on every press, and discarding the undo on a
      // press that could not use it would silently spend the one exit the user
      // has.
      return false
    }
    editRestorePoint = null
    options.pendingForkBeforeMessageId.value = null
    options.messages.value = restore.messages
    // Only put the old draft back over the text this edit itself wrote.
    // Anything else in the composer arrived afterwards — a message popped off
    // the pending queue, a draft recovered from a rejected send — and belongs
    // to the user, not to the edit being cancelled.
    if (options.inputText.value === restore.editedText
      && (!options.localPathReferences || JSON.stringify(options.localPathReferences.value) === JSON.stringify(restore.editedLocalPathReferences))) {
      restoreInput(restore.inputText, restore.localPathReferences)
    }
    if (options.selectedSkills && sameSelectedSkills(options.selectedSkills.value, restore.editedSkills)) {
      options.selectedSkills.value = copySelectedSkills(restore.selectedSkills)
    }
    options.autoResizeTextarea()
    return true
  }

  return {
    copyMessage,
    regenerateMessage,
    editMessage,
    cancelEdit,
    discardEditRestorePoint,
  }
}
