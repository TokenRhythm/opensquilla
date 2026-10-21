import { hasInjectionContext, inject, onScopeDispose, watch, type Ref } from 'vue'
import type { ChatMessage, RawToolCallPayload } from '@/types/chat'
import { activityReasoningBlocks } from '@/utils/chat/activitySnapshot'
import { ContentRangeCache, type ContentRangeRef } from '@/utils/chat/contentRangeCache'
import { HISTORY_CONTENT_READER_KEY } from '@/modules/historyContent'

function identity(ref?: ContentRangeRef): string {
  return ref ? JSON.stringify([ref.sessionKey, ref.sessionId, ref.messageId, ref.source, ref.revision]) : ''
}

/** Detail reads belong to the existing disclosure, never to a background history scan. */
export function useChatHistoryDetails(options: { sessionKey: Ref<string>; messages: Ref<ChatMessage[]> }) {
  const createReader = hasInjectionContext() ? inject(HISTORY_CONTENT_READER_KEY, null) : null
  const reader = createReader ? createReader() : new ContentRangeCache()
  const pending = new Map<string, { controller: AbortController; promise: Promise<void> }>()

  function cancel(key: string) {
    pending.get(key)?.controller.abort()
    pending.delete(key)
  }

  function setExpanded(ref: ContentRangeRef | undefined, expanded: boolean): Promise<void> {
    const key = identity(ref)
    if (!expanded) {
      cancel(key)
      return Promise.resolve()
    }
    if (!ref?.revision || ref.sessionKey !== options.sessionKey.value) return Promise.resolve()
    const existing = pending.get(key)
    if (existing) return existing.promise
    const matches = (message: ChatMessage) => identity(message.contentRef) === key
      && !message.clientId?.startsWith('history-model-call-segment:')
    if (!options.messages.value.some(message => matches(message) && message.historyPayloadPreview?.detailsTruncated)) {
      return Promise.resolve()
    }
    const controller = new AbortController()
    const entry = { controller, promise: Promise.resolve() }
    entry.promise = (async () => {
      try {
        const data = await reader.readDetails(ref, { signal: controller.signal })
        if (controller.signal.aborted || ref.sessionKey !== options.sessionKey.value) return
        options.messages.value = options.messages.value.map(message => {
          if (!matches(message) || !message.historyPayloadPreview?.detailsTruncated) return message
          const reasoningText = data.reasoning
          const blocks = message.activitySnapshot?.complete
            ? activityReasoningBlocks(message.activitySnapshot, reasoningText)
            : undefined
          return {
            ...message,
            reasoning: reasoningText ? { text: reasoningText, seconds: message.reasoning?.seconds ?? 0 } : undefined,
            reasoningBlocks: blocks,
            tool_calls: data.toolCalls as RawToolCallPayload[],
            // Existing timelines reference tool IDs; the renderer resolves them
            // against the full calls while retaining text/interrupt positions.
            historyPayloadPreview: { ...message.historyPayloadPreview, detailsTruncated: false },
            detailsCompleteRevision: ref.revision,
          }
        })
      } catch {
        // Keep the current content. Closing and reopening retries failed/oversized reads.
      } finally {
        if (pending.get(key) === entry) pending.delete(key)
      }
    })()
    pending.set(key, entry)
    return entry.promise
  }

  watch(() => [options.sessionKey.value, ...options.messages.value.map(message => identity(message.contentRef))], () => {
    const current = new Set(options.messages.value.flatMap(message => message.contentRef?.sessionKey === options.sessionKey.value
      ? [identity(message.contentRef)] : []))
    for (const key of pending.keys()) if (!current.has(key)) cancel(key)
  }, { flush: 'sync' })

  onScopeDispose(() => {
    for (const key of pending.keys()) cancel(key)
    reader.clear()
  })
  return { setExpanded }
}
