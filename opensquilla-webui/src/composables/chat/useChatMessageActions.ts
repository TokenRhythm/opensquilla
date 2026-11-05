import { copySelectedSkills, sameSelectedSkills, type SelectedSkillRef } from '@/types/selectedSkills'
import { copyLocalPathReferences } from '@/types/localPathReferences'
import { getCurrentScope, nextTick, onScopeDispose, toRaw, watch, type Ref } from 'vue'
import type {
  Attachment,
  ChatMessage,
  ChatRenderedMessage,
  ChatStreamTimelineItem,
} from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
import { snapshotAttachment } from '@/utils/chat/attachments'
import { resolveAssistantAnswer } from '@/utils/chat/assistantActivity'
import { turnOutcomePresentation } from '@/utils/chat/turnOutcome'
import {
  isUsageAccountingBarrierMessage,
  strictUsageBarrierRetryUserMessageIndex,
} from '@/utils/chat/usageAccountingFailure'
import { sanitizeAssistantPresentationSegments } from '@/utils/chat/silentSentinels'
import type { AssistantPresentationProvenance } from '@/utils/chat/silentSentinels'
import {
  messageContentIdentity,
  needsCompleteMessageText,
  restoreHistoryTimelineText,
  type ReadMessageText,
} from '@/utils/chat/historyMessageContent'

export interface RegenerateInputPayload {
  text: string
  attachments: Attachment[]
  selectedSkills?: SelectedSkillRef[]
  localPathReferences?: string[]
  forkBeforeMessageId: string
}

export interface UseChatMessageActionsOptions {
  messages: Ref<ChatMessage[]>
  inputText: Ref<string>
  pendingAttachments?: Ref<Attachment[]>
  attachmentWorkBusy?: () => boolean
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
  readAttachments?: (message: ChatMessage, signal: AbortSignal) => Promise<Attachment[]>
  sendRegeneratedInput: (payload: RegenerateInputPayload, isCurrent: () => boolean) => Promise<boolean>
  notifyAttachmentRecoveryFailed?: (name: string) => void
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
  attachments: Attachment[]
  editedAttachments: Attachment[]
  localPathReferences: string[]
  editedLocalPathReferences: string[]
  selectedSkills: SelectedSkillRef[]
  editedSkills: SelectedSkillRef[]
  /** Ties the restore point to the edit that made it; see `cancelEdit`. */
  forkBeforeMessageId: string
}

function sameAttachmentDraft(current: readonly Attachment[] | undefined, saved: readonly Attachment[]): boolean {
  const attachments = current ?? []
  return attachments.length === saved.length
    && JSON.stringify(attachments) === JSON.stringify(saved)
    && attachments.every((attachment, index) => toRaw(attachment.file) === toRaw(saved[index]?.file))
}

function stagedInputIdentity(attachment: Attachment): string {
  const input = { ...attachment }
  delete input.file
  delete input.file_uuid
  delete input.expires_at
  delete input.ttl_seconds
  // Upload refresh rebuilds the record and may change property insertion order.
  return JSON.stringify(Object.fromEntries(Object.entries(input).sort(([left], [right]) => left.localeCompare(right))))
}

function sameEditedAttachments(current: readonly Attachment[], saved: readonly Attachment[]): boolean {
  return current.length === saved.length && current.every((attachment, index) => {
    const previous = saved[index]!
    // Cancellation follows logical input ownership. The sender can refresh an
    // expired upload and restore its new receipt after rejection without the
    // operator having edited the image. Async read guards remain stricter.
    if (attachment.kind === 'staged' && previous.kind === 'staged'
      && attachment.file && toRaw(attachment.file) === toRaw(previous.file)) {
      return stagedInputIdentity(attachment) === stagedInputIdentity(previous)
    }
    return sameAttachmentDraft([attachment], [previous])
  })
}

function copyBodyIdentity(message: ChatMessage | ChatRenderedMessage): string {
  const ref = message.contentRef
  if (!ref) return messageContentIdentity(message)
  return JSON.stringify([message.role, message.turnId, message.contentRevision,
    ref.version, ref.sessionKey, ref.sessionId, ref.messageId, ref.source, ref.view,
    ref.revision, ref.byteLength, ref.sha256])
}

export function useChatMessageActions(options: UseChatMessageActionsOptions) {
  let editRestorePoint: EditRestorePoint | null = null
  let actionGeneration = 0
  let pendingRead: AbortController | null = null
  let copyGeneration = 0
  let pendingCopyRead: AbortController | null = null

  function cancelPendingCopy() {
    copyGeneration += 1
    pendingCopyRead?.abort()
    pendingCopyRead = null
  }

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

  function discardSessionActions() {
    discardEditRestorePoint()
    cancelPendingCopy()
  }

  if (options.sessionIdentity) watch(options.sessionIdentity, discardSessionActions, { flush: 'sync' })
  if (getCurrentScope()) onScopeDispose(discardSessionActions)

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
    const sourceInput = JSON.stringify([source.attachments, source.localPathReferences, source.selectedSkills])
    const sourceFiles = source.attachments?.map(attachment => toRaw(attachment.localFile)) ?? []
    const session = options.sessionIdentity?.()
    const draft = options.inputText.value
    const attachments = options.pendingAttachments?.value.map(snapshotAttachment) ?? []
    const paths = JSON.stringify(options.localPathReferences?.value)
    const skills = JSON.stringify(options.selectedSkills?.value)
    const fork = options.pendingForkBeforeMessageId.value
    const current = () => generation === actionGeneration
      && session === options.sessionIdentity?.()
      && transcript === options.messages.value && identity === messageContentIdentity(source)
      && sourceInput === JSON.stringify([source.attachments, source.localPathReferences, source.selectedSkills])
      && sourceFiles.every((file, index) => file === toRaw(source.attachments?.[index]?.localFile))
      && transcript.length === rows.length && transcript.every((row, index) => row === rows[index])
      && sameAttachmentDraft(options.pendingAttachments?.value, attachments)
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
    cancelPendingCopy()
    const generation = copyGeneration
    let controller: AbortController | undefined
    try {
      let complete = msg
      if (needsCompleteMessageText(msg)) {
        if (!options.readMessageText) return false
        const requested = {
          ...msg,
          contentRef: msg.contentRef && { ...msg.contentRef },
          contentSlice: msg.contentSlice && { ...msg.contentSlice },
        }
        const session = options.sessionIdentity?.()
        const identity = copyBodyIdentity(requested)
        const source = options.messages.value[sourceMessageIndex(msg)]
        const sourceIdentity = source && copyBodyIdentity(source)
        const ownerId = source?.contentRef?.messageId
        controller = new AbortController()
        pendingCopyRead = controller
        const text = await options.readMessageText(requested, controller.signal)
        const currentSource = ownerId
          ? options.messages.value.find(row => row.messageId === ownerId)
          : options.messages.value[sourceMessageIndex(requested)]
        // Hydration may replace the aggregate with slices of the same body.
        // Keep the clicked scope, but require its durable owner and body identity
        // to remain current; composer changes do not cancel a Copy.
        if (controller.signal.aborted || generation !== copyGeneration
          || session !== options.sessionIdentity?.() || identity !== copyBodyIdentity(msg)
          || (source && (!currentSource || sourceIdentity !== copyBodyIdentity(currentSource)
            || sourceIdentity !== copyBodyIdentity(source)))) return false
        complete = {
          ...requested, text,
          timelineItems: restoreHistoryTimelineText(requested, text) ?? requested.timelineItems,
        }
      }
      const text = copyableMessageText(complete)
      if (!text) return false
      const isAssistant = (msg.displayRole || msg.role) === 'assistant'
      const label = isAssistant ? options.aiGeneratedLabel?.().trim() : ''
      await copyTextWithFallback(label && text ? `${text}\n\n${label}` : text)
      return true
    } catch (err) {
      console.warn('Copy failed:', err instanceof Error ? err.message : String(err))
      return false
    } finally {
      if (pendingCopyRead === controller) pendingCopyRead = null
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
      // Regeneration owns the original input, never the current composer draft.
      // Recover everything before the sender may commit the visible branch.
      if (options.canDeliver && !options.canDeliver()) {
        options.notifyDeliveryBlocked?.()
        return false
      }
      const transcript = options.messages.value
      const rows = transcript.slice()
      const session = options.sessionIdentity?.()
      const source = userMessage!
      const identity = messageContentIdentity(source)
      const attachmentsIdentity = JSON.stringify(source.attachments)
      const snapshot: ChatMessage = {
        ...source,
        text: userText,
        attachments: source.attachments?.map(attachment => ({
          ...attachment,
          ...(attachment.fileSnapshotRef ? { fileSnapshotRef: { ...attachment.fileSnapshotRef } } : {}),
          ...(attachment.workspaceFile ? { workspaceFile: { ...attachment.workspaceFile } } : {}),
        })),
      }
      const paths = copyLocalPathReferences(source.localPathReferences, userText)
      const skills = copySelectedSkills(source.selectedSkills)
      const controller = new AbortController()
      pendingRead = controller
      const current = () => generation === actionGeneration && !controller.signal.aborted
        && session === options.sessionIdentity?.()
        && transcript === options.messages.value && identity === messageContentIdentity(source)
        && attachmentsIdentity === JSON.stringify(source.attachments)
        && transcript.length === rows.length && transcript.every((row, index) => row === rows[index])
        && JSON.stringify(paths) === JSON.stringify(copyLocalPathReferences(source.localPathReferences, userText))
        && sameSelectedSkills(skills, source.selectedSkills)
      return (async () => {
        try {
          // Keep the same cancellation window for inline and historical inputs.
          await nextTick()
          if (!current() || options.isStreaming.value) return false
          const attachments = snapshot.attachments?.length
            ? await options.readAttachments?.(snapshot, controller.signal)
            : []
          if (!current() || options.isStreaming.value) return false
          if (!attachments || attachments.length !== (snapshot.attachments?.length ?? 0)) {
            throw new Error('Original attachments are unavailable')
          }
          if (options.canDeliver && !options.canDeliver()) {
            options.notifyDeliveryBlocked?.()
            return false
          }
          const previousEdit = editRestorePoint
          const accepted = await options.sendRegeneratedInput({
            text: userText, attachments,
            selectedSkills: skills, localPathReferences: paths, forkBeforeMessageId,
          }, current)
          // A blocked/rejected replay must leave an unsubmitted edit cancellable.
          // A later action may have installed a different restore point meanwhile.
          if (accepted && editRestorePoint === previousEdit) editRestorePoint = null
          return accepted
        } catch (error) {
          if (current()) {
            options.notifyAttachmentRecoveryFailed?.(snapshot.attachments?.map(attachment => attachment.name).join(', ') || '')
            console.warn('Regenerate input recovery failed:', error instanceof Error ? error.message : String(error))
          }
          return false
        } finally {
          if (pendingRead === controller) pendingRead = null
        }
      })()
    })
  }

  function editMessage(message: ChatRenderedMessage) {
    if (options.isStreaming.value) {
      console.warn('Wait for the current response to finish')
      options.notifyEditBlocked?.()
      return
    }
    // Switching away from an unfinished intake would strand the saved draft:
    // its completion callback can only update attachments still in the composer.
    if (options.attachmentWorkBusy?.()) {
      options.notifyDeliveryBlocked?.()
      return false
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
    return withCompleteText(sourceMessage, (text, generation) => {
      const apply = (attachments: Attachment[]) => {
        if (options.attachmentWorkBusy?.()) {
          options.notifyDeliveryBlocked?.()
          return false
        }
        const previous = editRestorePoint
        const continuesEdit = previous
          && options.pendingForkBeforeMessageId.value === previous.forkBeforeMessageId
        editRestorePoint = {
          // Keep the complete transcript and draft from before the first edit.
          messages: continuesEdit ? previous.messages : options.messages.value,
          inputText: continuesEdit ? previous.inputText : options.inputText.value,
          editedText: text,
          attachments: continuesEdit ? previous.attachments : options.pendingAttachments?.value.map(snapshotAttachment) ?? [],
          editedAttachments: attachments.map(snapshotAttachment),
          localPathReferences: continuesEdit ? previous.localPathReferences : copyLocalPathReferences(options.localPathReferences?.value, options.inputText.value),
          editedLocalPathReferences: copyLocalPathReferences(sourceMessage.localPathReferences, text),
          selectedSkills: continuesEdit ? previous.selectedSkills : copySelectedSkills(options.selectedSkills?.value),
          editedSkills: copySelectedSkills(sourceMessage?.selectedSkills),
          forkBeforeMessageId,
        }
        // Attachment recovery must succeed before any visible edit mutation.
        options.pendingForkBeforeMessageId.value = forkBeforeMessageId
        options.messages.value = options.messages.value.slice(0, msgIndex)
        if (options.pendingAttachments) options.pendingAttachments.value = attachments
        if (options.selectedSkills) options.selectedSkills.value = copySelectedSkills(sourceMessage?.selectedSkills)
        restoreInput(text, sourceMessage.localPathReferences)
        options.autoResizeTextarea()
        options.focusComposer()
        return true
      }
      if (!sourceMessage.attachments?.length) return apply([])

      const transcript = options.messages.value
      const rows = transcript.slice()
      const session = options.sessionIdentity?.()
      const identity = messageContentIdentity(sourceMessage)
      const sourceAttachments = JSON.stringify(sourceMessage.attachments)
      const snapshot: ChatMessage = {
        ...sourceMessage, text,
        attachments: sourceMessage.attachments.map(attachment => ({
          ...attachment,
          ...(attachment.fileSnapshotRef ? { fileSnapshotRef: { ...attachment.fileSnapshotRef } } : {}),
          ...(attachment.workspaceFile ? { workspaceFile: { ...attachment.workspaceFile } } : {}),
        })),
      }
      const draft = options.inputText.value
      const draftAttachments = options.pendingAttachments?.value.map(snapshotAttachment) ?? []
      const draftPaths = JSON.stringify(options.localPathReferences?.value)
      const draftSkills = JSON.stringify(options.selectedSkills?.value)
      const sourcePaths = JSON.stringify(sourceMessage.localPathReferences)
      const sourceSkills = JSON.stringify(sourceMessage.selectedSkills)
      const fork = options.pendingForkBeforeMessageId.value
      const controller = new AbortController()
      pendingRead = controller
      const current = () => generation === actionGeneration && !controller.signal.aborted
        && session === options.sessionIdentity?.() && transcript === options.messages.value
        && rows.length === transcript.length && rows.every((row, index) => row === transcript[index])
        && identity === messageContentIdentity(sourceMessage)
        && sourceAttachments === JSON.stringify(sourceMessage.attachments)
        && snapshot.attachments?.every((attachment, index) => toRaw(attachment.localFile) === toRaw(sourceMessage.attachments?.[index]?.localFile))
        && sourcePaths === JSON.stringify(sourceMessage.localPathReferences)
        && sourceSkills === JSON.stringify(sourceMessage.selectedSkills)
        && draft === options.inputText.value && sameAttachmentDraft(options.pendingAttachments?.value, draftAttachments)
        && draftPaths === JSON.stringify(options.localPathReferences?.value)
        && draftSkills === JSON.stringify(options.selectedSkills?.value)
        && fork === options.pendingForkBeforeMessageId.value
        && !options.isStreaming.value
      return (async () => {
        try {
          if (!options.pendingAttachments || !options.readAttachments) throw new Error('Original attachments are unavailable')
          const attachments = await options.readAttachments(snapshot, controller.signal)
          if (!current()) return false
          if (attachments.length !== snapshot.attachments!.length) throw new Error('Original attachments are unavailable')
          return apply(attachments)
        } catch (error) {
          if (current()) {
            options.notifyAttachmentRecoveryFailed?.(snapshot.attachments!.map(attachment => attachment.name).join(', '))
            console.warn('Edit input recovery failed:', error instanceof Error ? error.message : String(error))
          }
          return false
        } finally {
          if (pendingRead === controller) pendingRead = null
        }
      })()
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
    if (options.pendingAttachments && sameEditedAttachments(options.pendingAttachments.value, restore.editedAttachments)) {
      options.pendingAttachments.value = restore.attachments.map(snapshotAttachment)
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
