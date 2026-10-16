// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { computed, effectScope, nextTick, ref, shallowRef, type EffectScope } from 'vue'
import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'
import type { ChatMessageListVirtualizer, ChatViewportSnapshot } from '@/types/chatVirtualizer'
import { SessionMessageSearchError, type SessionMessageSearch, type SessionMessageSearchMatch } from '@/modules/sessionMessageSearch'
import type { SessionReadHistoryPage } from '@/modules/sessionReadLifecycle'
import { useChatFindPresentation } from './useChatFindPresentation'

const scopes: EffectScope[] = []
function message(messageId: string, text = 'alpha beta alpha'): ChatMessage {
  return { messageId, role: 'assistant', text, ts: null }
}
function rendered(item: ChatMessage): ChatRenderedMessage {
  return { ...item, role: item.role, displayRole: item.role, roleLabel: item.role, timeStr: '', showHeader: false }
}
const match: SessionMessageSearchMatch = { messageId: 'live', entryCursor: 'entry-live', contentRevision: 1,
  offsetUtf16: 11, lengthUtf16: 5, snippet: 'source alpha beta alpha' }
const page: SessionReadHistoryPage = { messages: [], hasMore: false, oldestCursor: null, newestCursor: null,
  scope: 'complete', loadedCount: 1, pageSize: 100, canonicalAvailable: true, canonicalComplete: true,
  compactionSummaries: [], turnOutcomes: [], additional: {} }
const viewport: ChatViewportSnapshot = { scrollTop: 412, width: 800, followLiveEdge: false,
  anchor: { key: 'message-live', intraOffset: 16 }, rows: [{ key: 'message-live', size: 100 }] }
function context(controller = new AbortController()) {
  return { query: 'alpha', sessionId: 'canonical-A', sessionEpoch: 3, signal: controller.signal }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
function setup() {
  const scope = effectScope()
  scopes.push(scope)
  const root = document.createElement('article')
  root.innerHTML = '<p>alpha beta alpha</p>'
  const threadElement = document.createElement('div')
  threadElement.append(root)
  document.body.append(threadElement)
  threadElement.scrollTop = 412
  const releaseHandoff = vi.fn()
  const list = {
    messageAnchorId: (index: number) => `find-turn-${index}`,
    ensureMessageVisible: vi.fn<(index: number) => Promise<HTMLElement | null>>().mockResolvedValue(root),
    releaseEnsuredMessage: vi.fn(), messageIndexAtOffset: vi.fn(() => 0), scrollToMessage: vi.fn(),
    scrollToEnd: vi.fn(), getDistanceFromEnd: vi.fn(() => 200), hasPendingLayout: vi.fn(() => false),
    cancelScroll: vi.fn(), beginScrollHandoff: vi.fn(() => releaseHandoff), geometryVersion: vi.fn(() => 0),
    remeasure: vi.fn(), isVirtualized: vi.fn(() => true), captureViewport: vi.fn(() => viewport), restoreViewport: vi.fn(),
  } satisfies ChatMessageListVirtualizer
  const messages = shallowRef<ChatRenderedMessage[]>([rendered(message('live'))])
  const contextMessages = shallowRef<ChatMessage[] | null>(null)
  const contextRequest = vi.fn<SessionMessageSearch['context']>().mockResolvedValue(page)
  const source: SessionMessageSearch = { search: vi.fn(), context: contextRequest }
  const options = {
    sessionKey: ref('session-A'), source, messages,
    displayMessages: computed(() => contextMessages.value?.map(rendered) ?? messages.value),
    contextMessages, project: vi.fn(() => [message('historical')]),
    list: shallowRef<ChatMessageListVirtualizer | null>(list), thread: shallowRef<HTMLElement | null>(threadElement),
    autoScroll: ref(true), focusComposer: vi.fn(),
  }
  return { ...options, listMock: list, contextRequest, releaseHandoff, root, threadElement, scope,
    api: scope.run(() => useChatFindPresentation(options))! }
}
afterEach(() => {
  scopes.splice(0).forEach(scope => scope.stop())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useChatFindPresentation', () => {
  it('restores the captured viewport and following state only when closing in the same session', async () => {
    const h = setup()
    h.api.capture()
    expect(h.autoScroll.value).toBe(false)
    h.contextMessages.value = [message('historical')]
    h.threadElement.scrollTop = 900
    await h.api.reset(true)
    expect(h.contextMessages.value).toBeNull()
    expect(h.listMock.restoreViewport).toHaveBeenCalledWith(viewport)
    expect(h.autoScroll.value).toBe(true)
    expect(h.focusComposer).toHaveBeenCalledOnce()
  })

  it('preserves a captured manual-reading state when closing', async () => {
    const h = setup()
    h.autoScroll.value = false
    h.api.capture()
    await h.api.reset(true)
    expect(h.autoScroll.value).toBe(false)
    expect(h.listMock.restoreViewport).toHaveBeenCalledWith(viewport)
  })

  it('restores a raw scroll position when no virtualizer viewport was available', async () => {
    const h = setup()
    h.list.value = null
    h.api.capture()
    h.threadElement.scrollTop = 999
    await h.api.reset(true)
    expect(h.threadElement.scrollTop).toBe(412)
    expect(h.autoScroll.value).toBe(true)
    expect(h.focusComposer).toHaveBeenCalledOnce()
  })

  it.each(['close-false', 'different-session'] as const)('clears context without restoring a previous session on %s', async reason => {
    const h = setup()
    h.api.capture()
    h.contextMessages.value = [message('historical')]
    if (reason === 'different-session') h.sessionKey.value = 'session-B'
    await h.api.reset(reason !== 'close-false')
    expect(h.contextMessages.value).toBeNull()
    expect(h.autoScroll.value).toBe(false)
    expect(h.listMock.restoreViewport).not.toHaveBeenCalled()
    expect(h.focusComposer).not.toHaveBeenCalled()
  })

  it('does not restore a queued viewport after another selection reset or session switch', async () => {
    const h = setup()
    h.api.capture()
    const restoring = h.api.reset(true)
    h.api.clear()
    await restoring
    expect(h.listMock.restoreViewport).not.toHaveBeenCalled()
    expect(h.focusComposer).not.toHaveBeenCalled()
    const switched = h.api.reset(true)
    h.sessionKey.value = 'session-B'
    await switched
    expect(h.listMock.restoreViewport).not.toHaveBeenCalled()
  })

  it('selects a live message without fetching or mutating canonical messages', async () => {
    const h = setup(), canonical = h.messages.value
    const before = JSON.stringify(canonical)
    h.contextMessages.value = [message('historical')]
    await h.api.select(match, 1, context())
    expect(h.contextRequest).not.toHaveBeenCalled()
    expect(h.contextMessages.value).toBeNull()
    expect(h.messages.value).toBe(canonical)
    expect(JSON.stringify(h.messages.value)).toBe(before)
    expect(h.listMock.ensureMessageVisible).toHaveBeenCalledWith(0)
    expect(h.listMock.scrollToMessage).toHaveBeenCalledWith(0, { align: 'center', behavior: 'auto' })
    expect(h.listMock.cancelScroll).toHaveBeenCalledOnce()
    expect(h.api.sourceSnippet.value).toBe('')
  })

  it('acquires precise-text scroll ownership after the initial row seek is cancelled', async () => {
    const h = setup()
    let handoffActive = false
    h.listMock.scrollToMessage.mockImplementation(() => { handoffActive = false })
    h.listMock.cancelScroll.mockImplementation(() => { handoffActive = false })
    h.listMock.beginScrollHandoff.mockImplementation(() => {
      handoffActive = true
      return h.releaseHandoff
    })
    await h.api.select(match, 0, context())
    const seekOrder = h.listMock.scrollToMessage.mock.invocationCallOrder[0]!
    const cancelOrder = h.listMock.cancelScroll.mock.invocationCallOrder[0]!
    const acquireOrder = h.listMock.beginScrollHandoff.mock.invocationCallOrder[0]!
    expect(cancelOrder).toBeGreaterThan(seekOrder)
    expect(acquireOrder).toBeGreaterThan(cancelOrder)
    expect(handoffActive).toBe(true)
    h.api.clear()
    expect(h.releaseHandoff).toHaveBeenCalledOnce()
  })

  it('installs historical context as a presentation window while new live messages stay canonical', async () => {
    const h = setup(), pending = deferred<SessionReadHistoryPage>()
    h.contextRequest.mockReturnValue(pending.promise)
    const opening = h.api.select({ ...match, messageId: 'historical' }, 0, context())
    h.messages.value = [...h.messages.value, rendered(message('new-live', 'new live response'))]
    const canonical = h.messages.value
    pending.resolve(page)
    await opening
    expect(h.contextRequest).toHaveBeenCalledWith(expect.objectContaining({ key: 'session-A', messageId: 'historical', sessionId: 'canonical-A', sessionEpoch: 3 }))
    expect(h.project).toHaveBeenCalledWith(page)
    expect(h.contextMessages.value?.[0]?.messageId).toBe('historical')
    expect(h.messages.value).toBe(canonical)
    expect(h.messages.value[1]?.messageId).toBe('new-live')
    expect(h.listMock.ensureMessageVisible).toHaveBeenCalledWith(0)
  })

  it('reuses an already displayed context row without another context request', async () => {
    const h = setup()
    h.contextMessages.value = [message('historical')]
    await h.api.select({ ...match, messageId: 'historical' }, 0, context())
    expect(h.contextRequest).not.toHaveBeenCalled()
    expect(h.project).not.toHaveBeenCalled()
    expect(h.listMock.scrollToMessage).toHaveBeenCalledOnce()
  })

  it.each(['abort', 'clear', 'session', 'dispose'] as const)('does not install a late context page after %s', async reason => {
    const h = setup(), pending = deferred<SessionReadHistoryPage>(), controller = new AbortController()
    h.contextRequest.mockReturnValue(pending.promise)
    const opening = h.api.select({ ...match, messageId: 'historical' }, 0, context(controller))
    if (reason === 'abort') controller.abort()
    else if (reason === 'clear') h.api.clear()
    else if (reason === 'session') h.sessionKey.value = 'session-B'
    else h.scope.stop()
    pending.resolve(page)
    await opening
    expect(h.project).not.toHaveBeenCalled()
    expect(h.contextMessages.value).toBeNull()
    expect(h.listMock.ensureMessageVisible).not.toHaveBeenCalled()
    expect(h.listMock.scrollToMessage).not.toHaveBeenCalled()
  })

  it('keeps a newer live selection when a previous historical context request completes late', async () => {
    const h = setup(), pending = deferred<SessionReadHistoryPage>()
    h.contextRequest.mockReturnValueOnce(pending.promise)
    const old = h.api.select({ ...match, messageId: 'historical' }, 0, context())
    await h.api.select(match, 0, context())
    pending.resolve(page)
    await old
    expect(h.project).not.toHaveBeenCalled()
    expect(h.contextMessages.value).toBeNull()
    expect(h.listMock.scrollToMessage).toHaveBeenCalledOnce()
  })

  it('does not scroll or highlight a late DOM mount after clear', async () => {
    const h = setup(), pending = deferred<HTMLElement | null>()
    h.listMock.ensureMessageVisible.mockReturnValue(pending.promise)
    const opening = h.api.select(match, 0, context())
    await nextTick()
    expect(h.listMock.ensureMessageVisible).toHaveBeenCalledOnce()
    expect(h.listMock.beginScrollHandoff).not.toHaveBeenCalled()
    h.api.clear()
    pending.resolve(h.root)
    await opening
    expect(h.releaseHandoff).not.toHaveBeenCalled()
    expect(h.listMock.scrollToMessage).not.toHaveBeenCalled()
    expect(h.api.fallbackIndex.value).toBe(-1)
  })

  it('reports unavailable message content without inventing a successful target', async () => {
    const h = setup()
    h.listMock.ensureMessageVisible.mockResolvedValue(null)
    await expect(h.api.select(match, 0, context())).rejects.toThrow('Search message unavailable')
    expect(h.listMock.scrollToMessage).not.toHaveBeenCalled()
    expect(h.api.fallbackIndex.value).toBe(-1)
    h.api.clear()
    expect(h.releaseHandoff).not.toHaveBeenCalled()
  })

  it('waits for complete message content before choosing a DOM occurrence', async () => {
    const h = setup(), pending = deferred<HTMLElement | null>()
    const fullContentList = { ...h.listMock, ensureMessageContentVisible: vi.fn(() => pending.promise) }
    h.list.value = fullContentList
    const opening = h.api.select(match, 1, context())
    await nextTick()
    expect(fullContentList.ensureMessageContentVisible).toHaveBeenCalledWith(0)
    expect(h.listMock.ensureMessageVisible).not.toHaveBeenCalled()
    expect(h.listMock.scrollToMessage).not.toHaveBeenCalled()
    pending.resolve(h.root)
    await opening
    expect(h.listMock.scrollToMessage).toHaveBeenCalledOnce()
    expect(h.api.sourceSnippet.value).toBe('')
  })

  it('does not misidentify a Markdown destination match as a later visible text occurrence', async () => {
    const h = setup()
    h.root.innerHTML = '<p><a href="https://alpha.example">alpha</a> alpha</p>'
    const rawBodyList = { ...h.listMock, messageBodyText: () => '[alpha](https://alpha.example) alpha' }
    h.list.value = rawBodyList
    await h.api.select(match, 1, context())
    expect(h.api.sourceSnippet.value).toBe(match.snippet)
    expect(h.api.fallbackIndex.value).toBe(0)
  })

  it('shows the source snippet when the matched raw body is absent from the rendered text', async () => {
    const h = setup()
    h.root.innerHTML = '<p>Rendered diagram</p>'
    await h.api.select(match, 0, context())
    expect(h.api.fallbackIndex.value).toBe(0)
    expect(h.api.sourceSnippet.value).toBe(match.snippet)
    h.api.clear()
    expect(h.api.fallbackIndex.value).toBe(-1)
    expect(h.api.sourceSnippet.value).toBe('')
    expect(h.releaseHandoff).toHaveBeenCalledOnce()
  })

  it('preserves the previous source selection and scroll ownership while another history window stays busy', async () => {
    const h = setup()
    h.root.innerHTML = '<p>Rendered diagram</p>'
    await h.api.select(match, 0, context())
    h.contextRequest.mockRejectedValue(new SessionMessageSearchError('busy', 'busy'))
    for (let retry = 0; retry < 3; retry++) {
      await expect(h.api.select({ ...match, messageId: 'historical' }, 0, context())).rejects.toMatchObject({ reason: 'busy' })
    }
    expect(h.api.fallbackIndex.value).toBe(0)
    expect(h.api.sourceSnippet.value).toBe(match.snippet)
    expect(h.contextMessages.value).toBeNull()
    expect(h.releaseHandoff).not.toHaveBeenCalled()
    expect(h.listMock.scrollToMessage).toHaveBeenCalledOnce()
    h.contextRequest.mockResolvedValue(page)
    await h.api.select({ ...match, messageId: 'historical', snippet: 'historical source' }, 0, context())
    expect(h.contextMessages.value?.[0]?.messageId).toBe('historical')
    expect(h.api.sourceSnippet.value).toBe('historical source')
    expect(h.releaseHandoff).toHaveBeenCalledOnce()
  })

  it('keeps the previous CSS active range until a new context is ready and clears it on close', async () => {
    const registry = new Map<string, unknown>()
    vi.stubGlobal('CSS', { highlights: registry })
    vi.stubGlobal('Highlight', class { priority = 0; constructor(..._ranges: Range[]) {} })
    const h = setup(), pending = deferred<SessionReadHistoryPage>()
    await h.api.select(match, 0, context())
    const active = registry.get('opensquilla-chat-find-active')
    expect(active).toBeDefined()
    h.contextRequest.mockReturnValueOnce(pending.promise)
    const selecting = h.api.select({ ...match, messageId: 'historical' }, 0, context())
    expect(registry.get('opensquilla-chat-find-active')).toBe(active)
    expect(h.releaseHandoff).not.toHaveBeenCalled()
    h.api.clear()
    pending.resolve(page)
    await selecting
    expect(registry.size).toBe(0)
    expect(h.contextMessages.value).toBeNull()
  })

  it('reuses body highlights when navigating another hit in the same message', async () => {
    const h = setup()
    await h.api.select(match, 0, context())
    const createRange = vi.spyOn(document, 'createRange')
    await h.api.select(match, 1, context())
    expect(createRange).not.toHaveBeenCalled()
    expect(h.api.sourceSnippet.value).toBe('')
  })

  it('coalesces streamed body changes and cancels a queued repaint when closed', async () => {
    const h = setup()
    h.root.innerHTML = '<span class="activity">1 s</span><div data-chat-find-body>alpha alpha</div>'
    await h.api.select(match, 1, context())
    const createRange = vi.spyOn(document, 'createRange')
    h.root.querySelector('.activity')!.textContent = '2 s'
    await new Promise(resolve => setTimeout(resolve, 100))
    expect(createRange).not.toHaveBeenCalled()
    const body = h.root.querySelector('[data-chat-find-body]')!
    body.append(' alpha')
    body.append(' alpha')
    await new Promise(resolve => setTimeout(resolve, 120))
    expect(createRange).toHaveBeenCalledTimes(4)
    createRange.mockClear()
    body.append(' alpha')
    await new Promise(resolve => setTimeout(resolve, 0))
    h.api.clear()
    await new Promise(resolve => setTimeout(resolve, 100))
    expect(createRange).not.toHaveBeenCalled()
  })
})
