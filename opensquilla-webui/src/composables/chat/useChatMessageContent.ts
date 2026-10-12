import { hasInjectionContext, inject, onScopeDispose, type Ref } from 'vue'
import { HISTORY_CONTENT_READER_KEY } from '@/modules/historyContent'
import { ContentRangeCache } from '@/utils/chat/contentRangeCache'
import { sliceMessageContent, type ReadMessageText } from '@/utils/chat/historyMessageContent'

/** Actions read the durable body, independently of the message list's mounted-row cache. */
export function useChatMessageContent(sessionKey: Readonly<Ref<string>>) {
  const createReader = hasInjectionContext() ? inject(HISTORY_CONTENT_READER_KEY, null) : null
  const reader = createReader ? createReader() : new ContentRangeCache()
  onScopeDispose(() => reader.clear())

  const readMessageText: ReadMessageText = async (message, signal) => {
    const ref = message.contentRef
    if (!ref?.sessionKey || !ref.sessionId || !ref.messageId || !ref.revision
      || ref.sessionKey !== sessionKey.value
      || (message.messageId && message.messageId !== ref.messageId)) {
      throw new Error('A complete history content identity is required')
    }
    // Raw assistant/tool storage may be an envelope. Only the server's display
    // projection is safe to use as their visible body.
    if (ref.view !== 'display' && message.role !== 'user') {
      throw new Error('A display content reference is required')
    }
    const text = ref.view === 'display'
      ? await reader.readDisplay(ref, { signal })
      : await reader.readText(ref, { signal })
    if (signal.aborted) throw new DOMException('The operation was aborted.', 'AbortError')
    return sliceMessageContent(message, text)
  }
  return { readMessageText }
}
