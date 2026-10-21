import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'

export type HistoryMessageContent = Pick<ChatMessage,
  'role' | 'text' | 'messageId' | 'contentRef' | 'contentRevision' | 'contentSlice'
  | 'previewComplete' | 'contentUnavailableReason'>

export type ReadMessageText = (message: HistoryMessageContent, signal: AbortSignal) => Promise<string>

export function needsCompleteMessageText(message: HistoryMessageContent): boolean {
  return message.previewComplete !== true && (
    message.previewComplete === false || Boolean(message.contentRef) || Boolean(message.contentUnavailableReason)
  )
}

/** Includes the slice: several rendered messages can share one durable body. */
export function messageContentIdentity(message: HistoryMessageContent): string {
  const ref = message.contentRef
  return JSON.stringify([
    message.messageId, message.contentRevision, message.previewComplete, message.contentUnavailableReason,
    ref?.sessionKey, ref?.sessionId, ref?.messageId, ref?.source, ref?.view,
    ref?.revision, ref?.byteLength, ref?.sha256, message.contentSlice, message.text,
  ])
}

export function sliceMessageContent(message: HistoryMessageContent, text: string): string {
  const slice = message.contentSlice
  if (!slice) return text
  if (!Number.isSafeInteger(slice.startCodepoint) || !Number.isSafeInteger(slice.endCodepoint)
    || slice.startCodepoint < 0 || slice.endCodepoint < slice.startCodepoint) {
    throw new Error('Invalid history content slice')
  }
  const codepoints = Array.from(text)
  if (slice.endCodepoint > codepoints.length) throw new Error('Incomplete history content slice')
  return codepoints.slice(slice.startCodepoint, slice.endCodepoint).join('')
}

/** Restore the original timeline boundaries without changing presentation or tool order. */
export function restoreHistoryTimelineText(
  message: ChatRenderedMessage,
  text: string,
): ChatRenderedMessage['timelineItems'] {
  const lengths = message.historyPayloadPreview?.textUtf16Lengths
  const timeline = message.timelineItems
  if (!lengths || !timeline || timeline.filter(item => item.type === 'text').length !== lengths.length) return undefined
  // Finalizers can join segments directly or insert readable paragraph boundaries.
  for (const readable of [false, true]) {
    let cursor = 0
    let index = 0
    let previous = ''
    let valid = true
    const restored = timeline.map(item => {
      if (item.type !== 'text') return item
      const length = lengths[index++]!
      const preview = item.rawText ?? ''
      if (readable && previous && !/\s$/.test(previous) && !/^\s/.test(preview)) {
        if (text.slice(cursor, cursor + 2) !== '\n\n') valid = false
        cursor += 2
      }
      const rawText = text.slice(cursor, cursor + length)
      cursor += length
      if (!Number.isSafeInteger(length) || length < preview.length || rawText.length !== length || !rawText.startsWith(preview)) valid = false
      previous = rawText
      return { ...item, rawText }
    })
    if (valid && cursor === text.length) return restored
  }
  return undefined
}
