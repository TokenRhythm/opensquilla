// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { ContentRangeCache } from '@/utils/chat/contentRangeCache'
import { projectAssistantActivity } from '@/utils/chat/assistantActivity'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import ChatMessageList from './ChatMessageList.vue'

vi.mock('@/components/chat/AssistantMessage.vue', () => ({
  default: { props: ['message'], setup: (props: { message: ChatRenderedMessage }) => () => h('div', {
    'data-testid': 'assistant-body', 'data-artifacts': JSON.stringify(props.message.artifacts),
    'data-timeline': JSON.stringify(props.message.timelineItems),
  }, projectAssistantActivity(props.message, value => value).answerPart?.rawText ?? props.message.text) },
}))
vi.mock('@/components/chat/UserMessage.vue', () => ({
  default: { props: ['message'], setup: (props: { message: ChatRenderedMessage }) => () => h('div', { 'data-testid': 'user-body' }, props.message.text) },
}))

const apps: App<Element>[] = []
const sessionKey = 'agent:main:webchat:content'

function assistant(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    id: 'answer', messageId: 'answer', role: 'assistant', displayRole: 'assistant',
    roleLabel: 'Assistant', text: 'Preview', timeStr: '', showHeader: false,
    contentRef: {
      version: 1, sessionKey, sessionId: 'session', messageId: 'answer',
      byteLength: 32 * 1024, revision: 'revision', view: 'display',
    },
    ...overrides,
  }
}

function rawUser(bytes = 32 * 1024): ChatRenderedMessage {
  const value = assistant({ role: 'user', displayRole: 'user', roleLabel: 'User' })
  return { ...value, contentRef: { ...value.contentRef!, view: 'raw', byteLength: bytes } }
}

async function mountList(messages: ChatRenderedMessage[] = [assistant()], renderMarkdown = (value: string) => value) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const props = reactive({
    messages, sessionKey, scrollEpoch: 0, shareMode: false,
    selectedMessageIds: new Set<string>(), stripTimePrefix: (value: string) => value,
    renderMarkdown, fmtTok: (value: number) => String(value),
    subagentSummary: (value: string) => value, subagentBody: (value: string) => value,
    toolCallGroups: () => [], isToolGroupOpen: () => false, isToolItemOpen: () => false,
    toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
    copyMessage: async () => true, downloadAttachment: async () => true,
  })
  const app = createApp({ setup: () => () => h(ChatMessageList, props) })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { host, props }
}

async function retry(host: HTMLElement, selector = '[data-testid="chat-history-content-hydration"] button') {
  const button = host.querySelector<HTMLButtonElement>(selector)
  expect(button).not.toBeNull()
  button!.click()
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

async function settleHydration() {
  for (let index = 0; index < 6; index++) {
    await Promise.resolve()
    await nextTick()
  }
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ChatMessageList inline history content', () => {
  it('reads the complete 8 MiB single-line answer through the real Markdown renderer', async () => {
    const body = '0123456789abcdef'.repeat(512 * 1024)
    const preview = body.slice(0, 4096)
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(body)
    const { renderMarkdown } = useChatTextRendering()
    const row = assistant({
      text: preview, previewComplete: false,
      historyPayloadPreview: { textUtf16Lengths: [body.length] },
      timelineItems: [{ type: 'text', key: 'answer', rawText: preview, html: preview,
        presentation: 'answer', activityOrder: 1 }],
    })
    row.contentRef = { ...row.contentRef!, byteLength: body.length }
    const { host, props } = await mountList([row], renderMarkdown)
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    const rendered = host.querySelector('[data-testid="assistant-body"]')!
    expect(rendered.textContent === body).toBe(true)
    const timeline = JSON.parse(rendered.getAttribute('data-timeline')!)
    const fullText = document.createElement('div')
    fullText.innerHTML = timeline[0].html
    expect(fullText.textContent === body).toBe(true)
    expect(fullText.querySelector('.chat-markdown-plain')).not.toBeNull()
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
    props.messages = [assistant({ text: 'Next answer', previewComplete: true })]
    await nextTick()
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe('Next answer')
  })

  it.each(['', '\n\n'].flatMap(separator => ['ascii', 'unicode'].map(alphabet => ({ separator, alphabet }))))(
    'hydrates mixed text/tool/text without losing answer or activity order ($alphabet, separator $separator)', async ({ separator, alphabet }) => {
    const note = (alphabet === 'unicode' ? '检查😀文件。' : 'Working note. ').repeat(1600).trim()
    const answer = (alphabet === 'unicode' ? '完成🙂答复。' : 'Complete final answer. ').repeat(1600).trim()
    const notePreview = Array.from(note).slice(0, 100).join('')
    const answerPreview = Array.from(answer).slice(0, 100).join('')
    if (alphabet === 'unicode') {
      expect(note.length).toBeGreaterThan(Array.from(note).length)
      expect(answer.length).toBeGreaterThan(Array.from(answer).length)
    }
    const body = note + separator + answer
    vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(body)
    const timeline: NonNullable<ChatRenderedMessage['timelineItems']> = [
      { type: 'text', key: 'note', rawText: notePreview, html: '', presentation: 'intermediate', activityOrder: 2 },
      { type: 'tool-group', key: 'tool', activityOrder: 3, group: {
        groupId: 'tool', operationKey: 'file.read', label: 'Read', iconName: 'gear',
        secondary: '', isRunning: false, isError: false, status: 'success',
        calls: [{ toolId: 'call', renderKey: 'call', name: 'read_file', displayName: 'Read',
          inputPreview: '{}', result: 'ok', resultPreview: 'ok', isOpen: false,
          isRunning: false, isError: false, status: 'success', activityOrder: 3 }],
      } },
      { type: 'text', key: 'answer', rawText: answerPreview, html: '', presentation: 'answer', activityOrder: 5 },
    ]
    const render = vi.fn((value: string) => value)
    const { host } = await mountList([assistant({
      text: notePreview, previewComplete: false, timelineItems: timeline,
      historyPayloadPreview: { textUtf16Lengths: [note.length, answer.length] },
    })], render)
    expect(host.querySelector('[data-testid="chat-history-detail-preview"]')).toBeNull()
    await settleHydration()
    expect(render).toHaveBeenCalledTimes(2)
    expect(render.mock.calls.map(([text]) => text.length)).toEqual([note.length, answer.length])
    const rendered = host.querySelector('[data-testid="assistant-body"]')!
    expect(rendered.textContent).toBe(answer)
    const restored = JSON.parse(rendered.getAttribute('data-timeline')!)
    expect(restored.map((item: { activityOrder: number }) => item.activityOrder)).toEqual([2, 3, 5])
    expect(restored.map((item: { rawText?: string }) => item.rawText)).toEqual([note, undefined, answer])
    expect(restored[1]).toEqual(timeline[1])
    expect(timeline[0]?.type === 'text' && timeline[0].rawText).toBe(notePreview)
    for (const item of restored.filter((item: { type: string }) => item.type === 'text')) {
      expect(new TextDecoder().decode(new TextEncoder().encode(item.rawText))).toBe(item.rawText)
    }
  })

  it('keeps internal detail-preview metadata out of the conversation', async () => {
    const { host } = await mountList([assistant({
      text: 'Complete answer', previewComplete: true,
      historyPayloadPreview: { detailsTruncated: true, reasoningUtf16Length: 90000 },
    })])
    expect(host.querySelector('[data-testid="chat-history-detail-preview"]')).toBeNull()
    expect(host.querySelector('[role="status"]')).toBeNull()
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe('Complete answer')
  })
  it.each([
    ['assistant', 'preparing'], ['assistant', 'unavailable'],
    ['user', 'preparing'], ['user', 'unavailable'],
  ] as const)('shows existing %s %s feedback for an empty incomplete body without polling', async (role, availability) => {
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
    const { host } = await mountList([assistant({
      text: '', contentRef: undefined, previewComplete: false, contentAvailability: availability,
      role, displayRole: role,
      contentUnavailableReason: availability === 'preparing' ? 'content_metadata_pending' : 'content_reference_unavailable',
    })])
    const status = host.querySelector('[data-testid="chat-history-content-hydration"]')!
    expect(status.getAttribute('role')).toBe('status')
    expect(status.textContent).toContain(i18n.global.t(availability === 'preparing' ? 'shared.loading' : 'historyContent.unavailable'))
    expect(status.querySelector('button')).toBeNull()
    expect(read).not.toHaveBeenCalled()
  })

  it('shows complete ordinary text immediately without a redundant read action', async () => {
    const text = 'Complete answer. '.repeat(300)
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
    const { host } = await mountList([assistant({ text, previewComplete: true })])
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe(text)
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
    expect(read).not.toHaveBeenCalled()
  })

  it('hydrates display text while preserving presentation and artifacts, then accepts a complete server body', async () => {
    const text = 'Working note.\n\nFinal answer.'
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(text)
    const { host, props } = await mountList([assistant({
      previewComplete: false, contentRevision: 'revision', artifacts: [{ id: 'artifact' }],
      timelineItems: [
        { type: 'text', key: 'note', rawText: 'Working note.', html: 'Working note.', presentation: 'intermediate' },
        { type: 'text', key: 'answer', rawText: 'Final answer.', html: 'Final answer.', presentation: 'answer' },
      ],
    })])
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    const body = host.querySelector('[data-testid="assistant-body"]')!
    expect(body.textContent).toBe('Final answer.')
    expect(body.getAttribute('data-artifacts')).toBe('[{"id":"artifact"}]')
    props.messages = [assistant({ text: 'Authoritative complete body', previewComplete: true })]
    await nextTick()
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe('Authoritative complete body')
  })

  it.each(['content', 'reference'] as const)('does not apply a late hydrated body after the %s revision changes', async revision => {
    let resolveOld!: (text: string) => void
    vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockImplementation(() => new Promise(() => {}))
    const { host, props } = await mountList()
    await settleHydration()
    props.messages = [assistant({
      text: 'New revision preview',
      ...(revision === 'content' ? { contentRevision: 'r2' }
        : { contentRef: { ...assistant().contentRef!, revision: 'r2' } }),
    })]
    await nextTick()
    resolveOld('Old revision body')
    await nextTick()
    await nextTick()
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe('New revision preview')
    expect(host.textContent).not.toContain('Old revision body')
  })

  it('restores an ordinary assistant answer through its semantic projection without an export action', async () => {
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue('Complete ordinary answer')
    const raw = vi.spyOn(ContentRangeCache.prototype, 'readText')
    const { host } = await mountList()
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    expect(raw).not.toHaveBeenCalled()
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe('Complete ordinary answer')
    expect(host.querySelector('[data-testid="chat-content-export"]')).toBeNull()
  })

  it('lets the bounded display endpoint project stored JSON larger than the raw row rendering threshold', async () => {
    const row = assistant()
    row.contentRef = { ...row.contentRef!, byteLength: 4 * 1024 * 1024 }
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue('Projected answer, without internal JSON')
    const { host } = await mountList([row])
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    expect(host.textContent).toContain('Projected answer, without internal JSON')
    expect(host.textContent).not.toContain('Semantic preview unavailable')
  })

  it('hydrates a user caption by display bytes even when its inline-image envelope exceeds the raw limit', async () => {
    const row = assistant({ role: 'user', displayRole: 'user', text: 'Caption preview', previewComplete: false })
    row.contentRef = { ...row.contentRef!, view: 'display', byteLength: 8 * 1024 * 1024 }
    const caption = 'Synthetic caption. '.repeat(1200)
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(caption)
    const raw = vi.spyOn(ContentRangeCache.prototype, 'readText')
    const { host } = await mountList([row])
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    expect(raw).not.toHaveBeenCalled()
    expect(host.querySelector('[data-testid="user-body"]')?.textContent).toBe(caption)
  })

  it.each([undefined, 'a'.repeat(64)])('hydrates ordinary raw user rows without authorizing raw assistant text (digest: %s)', async sha256 => {
    const raw = vi.spyOn(ContentRangeCache.prototype, 'readText').mockResolvedValue('Complete user message')
    const display = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
    const legacy = assistant()
    legacy.id = legacy.messageId = 'legacy'
    legacy.contentRef = { ...legacy.contentRef!, messageId: 'legacy', view: 'raw', sha256 }
    const { host } = await mountList([rawUser(), legacy])
    await settleHydration()
    expect(raw).toHaveBeenCalledOnce()
    expect(display).not.toHaveBeenCalled()
    expect(host.querySelector('[data-testid="user-body"]')?.textContent).toBe('Complete user message')
    expect(host.querySelector('[data-testid="assistant-body"]')?.textContent).toBe('Preview')
  })

  it('does not fetch a raw body above the existing reader capacity or keep a loading state', async () => {
    const read = vi.spyOn(ContentRangeCache.prototype, 'readText')
    const row = rawUser(128 * 1024 * 1024)
    row.contentAvailability = 'preparing'
    row.previewComplete = false
    const { host, props } = await mountList([row])
    await settleHydration()
    expect(read).not.toHaveBeenCalled()
    expect(host.textContent).toContain('Preview')
    expect(host.textContent).toContain(i18n.global.t('historyContent.unavailable'))
    expect(host.textContent).not.toContain(i18n.global.t('shared.loading'))
    expect(host.querySelector('[data-testid="chat-history-content-hydration"] button')).toBeNull()
    props.messages = [...props.messages]
    await settleHydration()
    expect(read).not.toHaveBeenCalled()
  })

  it('keeps semantic failures local and retries without requesting raw protocol bytes', async () => {
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
      .mockRejectedValueOnce(new Error('Projection temporarily unavailable'))
      .mockResolvedValueOnce('Recovered semantic answer')
    const raw = vi.spyOn(ContentRangeCache.prototype, 'readText')
    const { host } = await mountList()
    await settleHydration()
    expect(host.textContent).toContain('Projection temporarily unavailable')
    expect(host.textContent).toContain('Preview')
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    await retry(host)
    expect(read).toHaveBeenCalledTimes(2)
    expect(raw).not.toHaveBeenCalled()
    expect(host.textContent).toContain('Recovered semantic answer')
  })

  it('rejects a late response after a session switch and keeps the new session request pending', async () => {
    let resolveOld!: (text: string) => void
    let resolveNew!: (text: string) => void
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveNew = resolve }))
    const { host, props } = await mountList()
    await settleHydration()
    const oldSignal = read.mock.calls[0]![1]!.signal!
    props.sessionKey = 'agent:main:webchat:other'
    props.messages = [assistant({ contentRef: { ...assistant().contentRef!, sessionKey: props.sessionKey } })]
    await nextTick()
    await settleHydration()
    expect(read).toHaveBeenCalledTimes(2)
    resolveOld('Old session answer')
    await settleHydration()
    expect(oldSignal.aborted).toBe(true)
    expect(host.textContent).not.toContain('Old session answer')
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')?.textContent)
      .toContain(i18n.global.t('shared.loading'))
    resolveNew('New session answer')
    await nextTick()
    await nextTick()
    expect(host.textContent).toContain('New session answer')
  })

  it.each(['session', 'revision'] as const)('loads the new %s while an old shared raw request never settles', async change => {
    const raw = vi.spyOn(ContentRangeCache.prototype, 'readText')
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValueOnce('New current body')
    const { host, props } = await mountList([rawUser()])
    await settleHydration()
    expect(raw).toHaveBeenCalledOnce()
    const oldSignal = raw.mock.calls[0]![1]!.signal!
    if (change === 'session') props.sessionKey = 'agent:main:webchat:replacement'
    const current = rawUser()
    current.contentRef = { ...current.contentRef!, sessionKey: props.sessionKey, revision: 'new-revision' }
    props.messages = [current]
    await settleHydration()
    expect(oldSignal.aborted).toBe(true)
    expect(raw).toHaveBeenCalledTimes(2)
    expect(host.querySelector('[data-testid="user-body"]')?.textContent).toBe('New current body')
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
  })

  it('releases an unmounted raw waiter so the same row can attach again before its old read settles', async () => {
    const raw = vi.spyOn(ContentRangeCache.prototype, 'readText')
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValueOnce('Reattached body')
    const row = rawUser()
    const { host, props } = await mountList([row])
    await settleHydration()
    const oldSignal = raw.mock.calls[0]![1]!.signal!
    props.messages = []
    await settleHydration()
    expect(oldSignal.aborted).toBe(true)
    props.messages = [row]
    await settleHydration()
    expect(raw).toHaveBeenCalledTimes(2)
    expect(raw.mock.calls[1]![1]!.signal).not.toBe(oldSignal)
    expect(host.querySelector('[data-testid="user-body"]')?.textContent).toBe('Reattached body')
  })

  it('hydrates transformed slices from one canonical semantic read', async () => {
    const row = assistant()
    const segment = assistant({ id: 'slice', clientId: 'history-model-call-segment:slice', text: 'Slice preview', previewComplete: false, contentSlice: { startCodepoint: 2, endCodepoint: 4 } })
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue('A😀世界Z')
    const { host } = await mountList([row, segment])
    await settleHydration()
    expect(read).toHaveBeenCalledOnce()
    expect([...host.querySelectorAll('[data-testid="assistant-body"]')].map(node => node.textContent)).toEqual(['A😀世界Z', '世界'])
  })
})
