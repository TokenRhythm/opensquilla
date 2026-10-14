import { nextTick, onScopeDispose, ref, type Ref } from 'vue'
import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'
import type { ChatMessageListVirtualizer, ChatViewportSnapshot } from '@/types/chatVirtualizer'
import type { SessionMessageSearch, SessionMessageSearchMatch } from '@/modules/sessionMessageSearch'
import type { SessionReadHistoryPage } from '@/modules/sessionReadLifecycle'
import { createChatFindHighlighter } from '@/utils/chat/chatFindHighlight'

/** A search window is presentation only; live messages and task ownership stay intact. */
export function useChatFindPresentation(options: {
  sessionKey: Readonly<Ref<string>>
  source: SessionMessageSearch | null
  messages: Readonly<Ref<ChatRenderedMessage[]>>
  displayMessages: Readonly<Ref<ChatRenderedMessage[]>>
  contextMessages: Ref<ChatMessage[] | null>
  project: (page: SessionReadHistoryPage) => ChatMessage[]
  list: Ref<ChatMessageListVirtualizer | null>
  thread: Ref<HTMLElement | null>
  autoScroll: Ref<boolean>
  focusComposer: () => void
}) {
  const fallbackIndex = ref(-1)
  const sourceSnippet = ref('')
  let repaint: (() => void) | undefined
  let repaintTimer: ReturnType<typeof setTimeout> | undefined
  const highlighter = createChatFindHighlighter(() => {
    // Streaming can mutate several nodes per token. Coalesce body changes and
    // let the highlighter yield between bounded work slices.
    if (!repaint || repaintTimer !== undefined) return
    repaintTimer = setTimeout(() => { repaintTimer = undefined; repaint?.() }, 80)
  })
  let savedViewport: ChatViewportSnapshot | undefined
  let savedScroll = 0
  let savedFollowing = false
  let savedKey = ''
  let sequence = 0
  let releaseHandoff: (() => void) | undefined

  function clear(preserveCache = false) {
    sequence += 1
    if (repaintTimer !== undefined) clearTimeout(repaintTimer)
    repaintTimer = undefined
    repaint = undefined
    highlighter.clear(preserveCache)
    fallbackIndex.value = -1
    sourceSnippet.value = ''
    options.list.value?.releaseEnsuredMessage()
    releaseHandoff?.()
    releaseHandoff = undefined
  }
  function capture() {
    savedKey = options.sessionKey.value
    savedViewport = options.list.value?.captureViewport?.()
    savedScroll = options.thread.value?.scrollTop ?? 0
    savedFollowing = options.autoScroll.value
    options.autoScroll.value = false
  }
  async function reset(restore: boolean) {
    clear()
    options.contextMessages.value = null
    if (!restore || savedKey !== options.sessionKey.value) return
    const key = savedKey
    const ownSequence = sequence
    await nextTick()
    if (key !== options.sessionKey.value || ownSequence !== sequence) return
    options.autoScroll.value = savedFollowing
    if (savedViewport) options.list.value?.restoreViewport?.(savedViewport)
    else if (options.thread.value) options.thread.value.scrollTop = savedScroll
    options.focusComposer()
  }
  async function select(match: SessionMessageSearchMatch, occurrence: number, context: {
    query: string; sessionId: string; sessionEpoch: number; signal: AbortSignal
  }) {
    // Keep the previous result visible while acquiring another history window.
    // Admission backpressure must not erase the selection we can return to.
    let ownSequence = ++sequence
    const key = options.sessionKey.value
    const current = () => !context.signal.aborted && ownSequence === sequence && key === options.sessionKey.value
    options.autoScroll.value = false
    const inLive = options.messages.value.some(message => message.messageId === match.messageId)
    let nextContext: ChatMessage[] | null | undefined = inLive ? null : undefined
    if (!inLive && !options.displayMessages.value.some(message => message.messageId === match.messageId)) {
      if (!options.source) throw new Error('Search unavailable')
      const page = await options.source.context({ key, messageId: match.messageId,
        sessionId: context.sessionId, sessionEpoch: context.sessionEpoch, signal: context.signal })
      if (!current()) return
      nextContext = options.project(page)
    }
    if (!current()) return
    clear(true)
    ownSequence = sequence
    if (nextContext !== undefined) options.contextMessages.value = nextContext
    await nextTick()
    if (!current()) return
    const index = options.displayMessages.value.findIndex(message => message.messageId === match.messageId)
    const list = options.list.value
    if (index < 0 || !list) throw new Error('Search message unavailable')
    const root = await (list.ensureMessageContentVisible?.(index) ?? list.ensureMessageVisible(index))
    if (!current()) return
    if (!root) throw new Error('Search message unavailable')
    list.scrollToMessage(index, { align: 'center', behavior: 'auto' })
    await nextTick()
    if (!current()) return
    let needsScroll = true
    async function paint() {
      if (!current() || !root) return
      // Link destinations / Markdown syntax can match source text without a
      // rendered counterpart. Keep its source excerpt instead of selecting a
      // different visible occurrence by accident.
      const result = await highlighter.updateAsync(root, context.query, occurrence,
        { sourceText: list?.messageBodyText?.(index), signal: context.signal })
      if (!current() || !result) return
      fallbackIndex.value = result.supported && result.activeRange ? -1 : index
      sourceSnippet.value = result.activeRange ? '' : match.snippet
      const thread = options.thread.value
      if (needsScroll && thread && result.activeRect) {
        const bounds = thread.getBoundingClientRect()
        thread.scrollTop += result.activeRect.top - bounds.top - bounds.height / 2
      }
      needsScroll = false
    }
    // The virtualizer owns mounting first, then the precise text range owns
    // positioning. Releasing its pending row seek avoids later jumps to row top.
    list.cancelScroll()
    releaseHandoff = list.beginScrollHandoff()
    repaint = () => { void paint().catch(() => {
      if (!current()) return
      fallbackIndex.value = index
      sourceSnippet.value = match.snippet
    }) }
    await paint()
  }
  onScopeDispose(clear)
  return { fallbackIndex, sourceSnippet, capture, reset, clear, select,
    capturedViewport: () => savedViewport,
  }
}
