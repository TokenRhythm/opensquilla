import { nextTick, type Ref } from 'vue'
import type {
  ChatMessage,
  ChatRenderedMessage,
  ChatStreamTimelineItem,
} from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'

export interface UseChatMessageActionsOptions {
  messages: Ref<ChatMessage[]>
  inputText: Ref<string>
  isStreaming: Ref<boolean>
  sanitizeCopyText: (text: string) => string
  stripTimePrefix: (text: string) => string
  autoResizeTextarea: () => void
  sendCurrentInput: () => void
  focusComposer: () => void
  pendingForkBeforeMessageId: Ref<string | null>
  aiGeneratedLabel?: () => string
  /**
   * User-visible feedback when regenerate/edit cannot run because the anchor
   * user message has no durable server id yet (chat.send ack lost, or an
   * older gateway omitted the id). Without it the buttons look dead: the
   * only trace of the refusal would be a console warning.
   */
  notifyMessagePending?: () => void
}

export function useChatMessageActions(options: UseChatMessageActionsOptions) {
  function copyableMessageText(message: ChatRenderedMessage): string {
    // User bubbles render the raw text with only the time prefix stripped, so
    // copy must match: the markdown sanitizers would truncate or strip literal
    // text (e.g. "<details>") that is visible on screen.
    if ((message.displayRole || message.role) === 'user') {
      return options.stripTimePrefix(message.text || '').trim()
    }
    // Tool-bearing turns render text as separate timeline segments; the raw
    // message text concatenates them without separators, so rebuild from the
    // segments to keep paragraph boundaries in the copied markdown.
    const segmentTexts = (message.timelineItems || [])
      .filter((item): item is Extract<ChatStreamTimelineItem, { type: 'text' }> => item.type === 'text')
      .map(item => options.sanitizeCopyText(item.rawText || ''))
      .filter(Boolean)
    if (segmentTexts.length) return segmentTexts.join('\n\n')
    return options.sanitizeCopyText(message.text || '')
  }

  async function copyMessage(msg: ChatRenderedMessage): Promise<boolean> {
    try {
      const text = copyableMessageText(msg)
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

  function regenerateMessage(message: ChatRenderedMessage) {
    if (options.isStreaming.value) {
      console.warn('Wait for the current response to finish')
      return
    }
    const assistantIndex = sourceMessageIndex(message)
    const userMsgIndex = previousUserMessageIndex(assistantIndex)
    if (userMsgIndex < 0) {
      console.warn('No previous message to regenerate')
      return
    }

    const userMessage = options.messages.value[userMsgIndex]
    const forkBeforeMessageId = userMessage?.messageId || ''
    if (!forkBeforeMessageId) {
      console.warn('Wait for the message to finish saving before regenerating')
      options.notifyMessagePending?.()
      return
    }
    const userText = userMessage?.text || ''
    options.pendingForkBeforeMessageId.value = forkBeforeMessageId
    options.messages.value = options.messages.value.slice(0, userMsgIndex)
    options.inputText.value = userText
    options.autoResizeTextarea()
    nextTick(() => options.sendCurrentInput())
  }

  function editMessage(message: ChatRenderedMessage) {
    if (options.isStreaming.value) {
      console.warn('Wait for the current response to finish')
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
    options.pendingForkBeforeMessageId.value = forkBeforeMessageId
    options.messages.value = options.messages.value.slice(0, msgIndex)
    options.inputText.value = text
    options.autoResizeTextarea()
    options.focusComposer()
  }

  return {
    copyMessage,
    regenerateMessage,
    editMessage,
  }
}
