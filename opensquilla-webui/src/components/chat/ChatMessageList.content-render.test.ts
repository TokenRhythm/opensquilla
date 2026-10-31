// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref, toRef, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import type { ChatMessage, ChatRenderedMessage, RawToolCallPayload } from '@/types/chat'
import { useChatHistoryDetails } from '@/composables/chat/useChatHistoryDetails'
import { useChatHistory } from '@/composables/chat/useChatHistory'
import { interleaveHistoryModelCallSegments } from '@/utils/chat/historyModelCallSegments'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import { ContentRangeCache } from '@/utils/chat/contentRangeCache'
import { HISTORY_CONTENT_READER_KEY, type HistoryContentReaderFactory } from '@/modules/historyContent'
import { createHistoryContentReader } from '@/adapters/gateway/historyContentV4'
import { createPrivateHttpTransport } from '@/adapters/gateway/privateHttpTransport'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { createV4ArtifactContentAccess } from '@/adapters/gateway/artifactAccessV4'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import ChatMessageList from './ChatMessageList.vue'
import * as browser from '@/utils/browser'

const apps: App[] = []
const sessionKey = 'agent:main:webchat:automatic-content'
const message = (overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage => ({
  id: 'answer', messageId: 'answer', role: 'assistant', displayRole: 'assistant',
  roleLabel: 'Assistant', text: 'PREVIEW_ONLY', timeStr: '', showHeader: false,
  previewComplete: false,
  contentRef: { version: 1, sessionKey, sessionId: 'session', messageId: 'answer', view: 'display', byteLength: 32768 },
  ...overrides,
})

async function mount(messages: ChatRenderedMessage[], historyMessages?: ChatMessage[], readerFactory?: HistoryContentReaderFactory) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const { renderMarkdown } = useChatTextRendering()
  const canonical = ref(interleaveHistoryModelCallSegments(historyMessages ?? []))
  const openTools = reactive(new Set<string>())
  const showResult = vi.fn()
  const props = reactive({
    messages, sessionKey, shareMode: false, selectedMessageIds: new Set<string>(),
    stripTimePrefix: (value: string) => value, renderMarkdown,
    fmtTok: (value: number) => String(value), subagentSummary: (value: string) => value,
    subagentBody: (value: string) => value, toolCallGroups: () => [],
    isToolGroupOpen: (key: string) => openTools.has(key), isToolItemOpen: (key: string) => openTools.has(key),
    toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
    copyMessage: async () => true, downloadAttachment: async () => true,
  })
  const app = createApp({ setup: () => {
    const key = toRef(props, 'sessionKey')
    const history = useChatHistory({
      sessionKey: key, messages: canonical, sessionReadLeaseReader: { current: () => null },
      lastHeaderRole: ref(''), lastHeaderDay: ref(''), stripTimePrefix: value => value,
      scrollToBottom: () => {},
    })
    const details = useChatHistoryDetails({ sessionKey: key, messages: canonical })
    const projected = historyMessages ? useChatRenderedMessages({
      messages: canonical, sessionKey: key, routerSlots: ref([]), routerModels: ref({}), routerTierConfigs: ref({}),
      routerVisualEffectsEnabled: ref(false), routerVisualMode: ref('real_candidates'), renderMarkdown,
      stripGeneratedArtifactMarkers: text => text, stripTimePrefix: text => text, isSubagentCompletionMessage: () => false,
    }).renderedMessages : undefined
    const toggle = (id: string) => { if (!openTools.delete(id)) openTools.add(id) }
    return () => h(ChatMessageList, {
      ...props, messages: projected?.value ?? props.messages,
      loadHistoryDetails: (message: ChatRenderedMessage) => details.setExpanded(message.contentRef, true),
      onDetailsDisclosure: (message: ChatRenderedMessage, open: boolean) => details.setExpanded(message.contentRef, open),
      onContentHydrated: (message: ChatRenderedMessage, text: string) => history.reconcileHydratedContent(message, text),
      onToggleToolGroup: toggle, onToggleToolItem: toggle, onShowToolResult: showResult,
    })
  } })
  const http = httpTransportTestDouble()
  app.use(i18n)
  app.use(createPinia())
  if (readerFactory) app.provide(HISTORY_CONTENT_READER_KEY, readerFactory)
  app.provide(GATEWAY_ACCESS_KEY, { isLocalOwner: false } as GatewayAccess)
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    content: createV4ArtifactContentAccess(http),
    previews: createV4ArtifactPreviews(http, { baseOrigin: () => 'http://localhost' }),
  } as ArtifactWorkbench)
  app.mount(host)
  apps.push(app)
  for (let index = 0; index < 6; index++) { await Promise.resolve(); await nextTick() }
  return { host, props, canonical, showResult }
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  sessionStorage.clear()
  clearAssistantActivityExpansionState()
})

describe('automatic content through the real assistant renderer', () => {
  it.each([1, 2])('reconstructs %i persisted Steer boundaries after delayed full-body hydration', async count => {
    const parts = ['前😀'.repeat(80), '中🙂'.repeat(70), '后🚀'.repeat(60)].slice(0, count + 1)
    const body = parts.join('')
    let finish!: (text: string) => void
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
      .mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const rows: ChatMessage[] = [{ role: 'user', text: '原始问题', ts: 1, messageId: 'original', turnId: 'turn' }]
    const segments = parts.slice(1).map((part, index) => {
      rows.push({ role: 'user', text: `调整${index + 1}`, ts: index + 2, messageId: `steer-${index}`,
        turnId: 'turn', inputDisposition: 'applied', steerModelCallId: `${index + 2}.0`, steerAppliedIteration: index + 2 })
      const start = Array.from(parts.slice(0, index + 1).join('')).length
      return { model_call_id: `${index + 2}.0`, iteration: index + 2, start_codepoint: start,
        end_codepoint: start + Array.from(part).length }
    })
    const preview = Array.from(body).slice(0, 8).join('')
    rows.push({ role: 'assistant', text: preview, ts: 5, messageId: 'answer', turnId: 'turn',
      previewComplete: false, contentRef: { ...message().contentRef!, revision: 'r1' }, contentRevision: 'r1',
      usage: { model_call_segments: segments }, restoredFromHistory: true })
    const { host, canonical } = await mount([], rows)
    expect(read).toHaveBeenCalledTimes(1)
    expect(host.textContent).not.toContain(parts[0])
    finish(body)
    for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick() }
    const expected = ['原始问题', ...parts.flatMap((part, index) => index ? [`调整${index}`, part] : [part])]
    const renderedRows = [...host.querySelectorAll('[data-testid="chat-message-row"]')]
    expect(renderedRows).toHaveLength(expected.length)
    expected.forEach((text, index) => expect(renderedRows[index]?.textContent).toContain(text))
    expect(canonical.value.filter(row => row.role === 'assistant').map(row => row.text).join('')).toBe(preview)
    expect(canonical.value.filter(row => row.messageId === 'answer')).toHaveLength(1)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('uses the cached full body when an earlier history page supplies the Steer anchor', async () => {
    const prefix = '前😀'.repeat(80)
    const suffix = '后🙂'.repeat(70)
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(prefix + suffix)
    const assistant: ChatMessage = { role: 'assistant', text: '前', ts: 3, messageId: 'answer', turnId: 'turn',
      previewComplete: false, contentRef: message().contentRef, usage: { model_call_segments: [{ model_call_id: '2.0',
        iteration: 2, start_codepoint: Array.from(prefix).length, end_codepoint: Array.from(prefix + suffix).length }] } }
    const { host, canonical } = await mount([], [assistant])
    expect(canonical.value).toHaveLength(1)
    canonical.value = [{ role: 'user', text: '调整', ts: 2, messageId: 'steer', turnId: 'turn',
      inputDisposition: 'applied', steerModelCallId: '2.0', steerAppliedIteration: 2 }, ...canonical.value]
    for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick() }
    const rows = [...host.querySelectorAll('[data-testid="chat-message-row"]')]
    expect(rows).toHaveLength(3)
    expect(rows.map(row => row.textContent)).toEqual([expect.stringContaining(prefix), expect.stringContaining('调整'), expect.stringContaining(suffix)])
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('does not apply a delayed Steer body after switching sessions', async () => {
    let finish!: (text: string) => void
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
      .mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const { host, props, canonical } = await mount([], [
      { role: 'user', text: '旧调整', ts: 2, messageId: 'steer', turnId: 'turn', inputDisposition: 'applied',
        steerModelCallId: '2.0', steerAppliedIteration: 2 },
      { role: 'assistant', text: '前', ts: 3, messageId: 'answer', turnId: 'turn', previewComplete: false,
        contentRef: message().contentRef, usage: { model_call_segments: [{ model_call_id: '2.0', iteration: 2,
          start_codepoint: 2, end_codepoint: 4 }] } },
    ])
    props.sessionKey = 'another-session'
    canonical.value = [{ role: 'assistant', text: '新会话正文', ts: 4, messageId: 'answer', previewComplete: true }]
    await nextTick()
    finish('前😀后续')
    for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick() }
    expect(read.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    expect(canonical.value.map(row => row.text)).toEqual(['新会话正文'])
    expect(host.textContent).toContain('新会话正文')
    expect(host.textContent).not.toContain('前😀')
    expect(host.textContent).not.toContain('旧调整')
  })
  it('uses the authenticated reader for both mounted body hydration and disclosed details', async () => {
    sessionStorage.setItem('opensquilla.wsToken', 'synthetic-component-token')
    const body = '## 完整正文\n\n**普通聊天正文🙂。**'
    const reasoning = '完整思考🙂'.repeat(5000)
    const fetcher = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
      const authenticated = new Headers(options?.headers).get('Authorization') === 'Bearer synthetic-component-token'
      if (!authenticated) return new Response('Unauthorized', { status: 401 })
      return new Response(String(url).includes('view=details')
        ? JSON.stringify({ message_id: 'answer', role: 'assistant', reasoning_content: reasoning,
          tool_calls: [{ type: 'text', text: body, presentation: 'answer' }] })
        : body)
    })
    const { host, canonical } = await mount([], [{
      role: 'assistant', text: body.slice(0, 8), messageId: 'answer', ts: 1, previewComplete: false,
      contentRef: { ...message().contentRef!, revision: 'r1' }, contentRevision: 'r1',
      historyPayloadPreview: { detailsTruncated: true, textUtf16Lengths: [body.length] }, reasoning: { text: '思考预览', seconds: 1 },
      tool_calls: [{ type: 'text', text: body.slice(0, 8), presentation: 'answer' }],
    }], createHistoryContentReader(createPrivateHttpTransport({ baseUrl: 'http://localhost/', fetch: fetcher })))
    await vi.waitFor(() => expect(host.textContent).toContain('普通聊天正文🙂。'))
    host.querySelector<HTMLButtonElement>('.assistant-activity__summary')!.click()
    await vi.waitFor(() => expect(canonical.value[0]?.reasoning?.text).toBe(reasoning))
    expect(fetcher.mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining('view=display'), expect.stringContaining('view=details'),
    ])
    await vi.waitFor(() => expect([...host.querySelectorAll('strong')].map(node => node.textContent))
      .toEqual(['普通聊天正文🙂。']))
    expect(host.textContent?.split('完整正文')).toHaveLength(2)
  })
  it.each(['immediate', 'pending', 'delayed', 'closed', 'replaced', 'failed'])('loads complete details and shares the existing read with inline copy (%s)', async mode => {
    const reasoning = '深入思考🙂'.repeat(3_000)
    const input = { data: 'long input '.repeat(300) }
    const result = 'full-result-'.repeat(300)
    const copy = vi.spyOn(browser, 'copyTextWithFallback').mockResolvedValue(undefined)
    const segments: RawToolCallPayload[] = [
      { type: 'tool_use', tool_use_id: 'tool', name: 'details_probe', input },
      { type: 'tool_result', tool_use_id: 'tool', name: 'details_probe', result },
      { type: 'text', text: 'Final answer', presentation: 'answer' },
    ]
    let release!: () => void
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      const response = () => new Response(JSON.stringify({
        message_id: 'answer', role: 'assistant', reasoning_content: reasoning, tool_calls: segments,
      }))
      return mode === 'immediate' || mode === 'pending' ? Promise.resolve(response())
        : new Promise((resolve, reject) => { release = () => mode === 'failed'
          ? reject(new Error('details unavailable')) : resolve(response()) })
    })
    const bodyRead = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
    const { host, canonical } = await mount([], [{
      role: 'assistant', text: 'Final answer', messageId: 'answer', ts: 1, previewComplete: true,
      contentRef: { ...message().contentRef!, revision: mode === 'pending' ? 'legacy-v1:active:1:1:0:pending' : 'r1',
        byteLength: mode === 'pending' ? undefined : 32768 }, contentRevision: 'r1',
      historyPayloadPreview: { detailsTruncated: true }, reasoning: { text: reasoning.slice(0, 30), seconds: 3 },
      tool_calls: [{ ...segments[0], input: { data: 'preview data '.repeat(60) } },
        { ...segments[1], result: 'preview result '.repeat(60) }, segments[2]],
    }])
    expect(fetcher).not.toHaveBeenCalled()
    expect(bodyRead).not.toHaveBeenCalled()
    const activity = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')!
    expect(activity).not.toBeNull()
    activity.click()
    if (mode !== 'immediate' && mode !== 'pending') {
      await nextTick()
      const row = host.querySelector<HTMLButtonElement>('.tool-row')!
      if (row.getAttribute('aria-expanded') !== 'true') row.click()
      await nextTick()
      host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')!.click()
      expect(copy).not.toHaveBeenCalled()
      if (mode === 'closed') activity.click()
      if (mode === 'replaced') canonical.value = []
      await nextTick()
      release()
    }
    for (let i = 0; i < 16; i++) { await Promise.resolve(); await nextTick() }
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]?.[0]).toContain('view=details&export=1')
    if (mode === 'closed' || mode === 'replaced' || mode === 'failed') {
      expect(copy).not.toHaveBeenCalled()
      if (mode !== 'replaced') expect(canonical.value[0]?.reasoning?.text).toBe(reasoning.slice(0, 30))
      return
    }
    expect(canonical.value[0]?.reasoning?.text).toBe(reasoning)
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe(reasoning)
    if (mode === 'immediate' || mode === 'pending') {
      const toolRow = host.querySelector<HTMLButtonElement>('.tool-row')!
      expect(toolRow).not.toBeNull()
      if (toolRow.getAttribute('aria-expanded') !== 'true') toolRow.click()
      await nextTick()
      const fullButton = host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')!
      expect(fullButton).not.toBeNull()
      fullButton.click()
    }
    await vi.waitFor(() => expect(copy).toHaveBeenCalledOnce())
    expect(copy.mock.calls[0]?.[0]).toContain(input.data)
    expect(copy.mock.calls[0]?.[0]).toContain(result.trimEnd())
    expect(host.querySelector('.chat-history-content-page')).toBeNull()
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
  })

  it('hydrates an unindexed body in the mounted row without waiting for metadata backfill', async () => {
    const body = '完整旧正文🙂'.repeat(4000)
    const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body))
    const { host } = await mount([message({
      contentAvailability: 'preparing',
      contentRef: { version: 1, sessionKey, sessionId: 'session', messageId: 'answer',
        view: 'display', source: 'active', revision: 'legacy-v1:active:1:1:0:pending' },
    })])
    await vi.waitFor(() => expect(host.querySelector('.assistant-answer .msg-ai-text')?.textContent?.trimEnd()).toBe(body))
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]?.[0]).toContain('view=display&export=1')
    expect(fetcher.mock.calls[0]?.[0]).toContain('revision=legacy-v1%3Aactive%3A1%3A1%3A0%3Apending')
    expect(host.textContent).not.toContain('PREVIEW_ONLY')
  })

  it('renders hydrated Markdown once without a separate reader or stale timeline preview', async () => {
    const body = '## 完整回答🙂\n\n**正文已恢复**\n\n- 条目一\n- 条目二\n'
    const prefix = body.slice(0, 5)
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(body)
    const { host } = await mount([message({
      text: prefix, historyPayloadPreview: { textUtf16Lengths: [body.length] },
      timelineItems: [{ type: 'text', key: 'answer', rawText: prefix, html: 'PREVIEW_ONLY', presentation: 'answer', activityOrder: 1 }],
    })])
    expect(read).toHaveBeenCalledOnce()
    // happy-dom's nodeName differs from browser semantics used by DOMPurify
    // (also documented in useChatTextRendering.test.ts). Check retained text
    // and real supported Markdown elements; browser acceptance covers headings.
    expect(host.textContent?.split('完整回答🙂')).toHaveLength(2)
    expect([...host.querySelectorAll('strong')].map(node => node.textContent)).toContain('正文已恢复')
    expect([...host.querySelectorAll('li')].map(node => node.textContent)).toEqual(['条目一', '条目二'])
    expect(host.textContent?.split('正文已恢复')).toHaveLength(2)
    expect(host.textContent).not.toContain('PREVIEW_ONLY')
    expect(host.querySelector('.chat-history-content-page')).toBeNull()
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
    expect([...host.querySelectorAll('button')].some(button => /Read full|阅读完整|Previous|Next|上一页|下一页/.test(button.textContent ?? ''))).toBe(false)
  })

  it.each(['', '\n\n'])('restores a mixed text/tool/text answer without repeating it (separator %j)', async separator => {
    const note = '检查😀文件。'.repeat(100)
    const answer = '## 结果🙂\n\n**唯一最终正文**\n'
    const text = note + separator + answer
    vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(text)
    const { host } = await mount([message({
      text: note.slice(0, 20), historyPayloadPreview: { textUtf16Lengths: [note.length, answer.length] },
      timelineItems: [
        { type: 'text', key: 'note', rawText: note.slice(0, 20), html: 'OLD_NOTE', presentation: 'intermediate', activityOrder: 2 },
        { type: 'tool-group', key: 'tool', activityOrder: 3, group: {
          groupId: 'tool', operationKey: 'file.read', label: 'Read', iconName: 'gear', secondary: '',
          isRunning: false, isError: false, status: 'success', calls: [{
            toolId: 'call', renderKey: 'call', name: 'read_file', displayName: 'Read', inputPreview: '{}',
            result: 'ok', resultPreview: 'ok', isOpen: false, isRunning: false, isError: false, status: 'success', activityOrder: 3,
          }],
        } },
        { type: 'text', key: 'answer', rawText: answer.slice(0, 5), html: 'OLD_ANSWER', presentation: 'answer', activityOrder: 5 },
      ],
    })])
    expect(host.textContent?.split('结果🙂')).toHaveLength(2)
    expect(host.querySelector('strong')?.textContent).toBe('唯一最终正文')
    expect(host.textContent?.split('唯一最终正文')).toHaveLength(2)
    expect(host.textContent).not.toContain('OLD_ANSWER')
    expect(host.querySelector('.chat-history-content-page')).toBeNull()
  })

  it('does not fetch a complete normal Markdown answer', async () => {
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
    const { host } = await mount([message({ text: '## 已完成\n\n**保留格式**', previewComplete: true })])
    expect(read).not.toHaveBeenCalled()
    expect(host.textContent?.split('已完成')).toHaveLength(2)
    expect(host.querySelector('strong')?.textContent).toBe('保留格式')
  })

  it('keeps three mounted 8 MiB answers complete and only evicts a body after its row leaves', async () => {
    const body = 'x'.repeat(8 * 1024 * 1024)
    const read = vi.spyOn(ContentRangeCache.prototype, 'readDisplay').mockResolvedValue(body)
    const rows = ['first', 'second', 'third'].map(id => message({
      id, messageId: id,
      contentRef: { ...message().contentRef!, messageId: id, byteLength: body.length },
    }))
    const { host, props } = await mount(rows)
    expect(read).toHaveBeenCalledTimes(3)
    const answers = [...host.querySelectorAll('.assistant-answer .msg-ai-text')]
    expect(answers).toHaveLength(3)
    expect(answers.every(answer => answer.textContent === body)).toBe(true)
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
    props.messages = rows.slice(1)
    await nextTick()
    props.messages = rows
    for (let index = 0; index < 6; index++) { await Promise.resolve(); await nextTick() }
    expect(read).toHaveBeenCalledTimes(4)
    expect(read.mock.calls[3]?.[0].messageId).toBe('first')
    expect([...host.querySelectorAll('.assistant-answer .msg-ai-text')].every(answer => answer.textContent === body)).toBe(true)
  })
})

it.each(['loaded', 'closed', 'replaced'])('hydrates the independent incomplete reasoning summary (%s)', async mode => {
  const reasoning = 'Committed reasoning 🙂 '.repeat(3000)
  let release!: () => void
  const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    new Promise(resolve => { release = () => resolve(new Response(JSON.stringify({
      message_id: 'answer', role: 'assistant', reasoning_content: reasoning,
      tool_calls: [{ type: 'text', text: 'Final answer', presentation: 'answer' }],
    }))) }),
  )
  const { host, canonical } = await mount([], [{
    role: 'assistant', text: 'Final answer', messageId: 'answer', ts: 1, previewComplete: true,
    contentRef: { ...message().contentRef!, revision: 'r1' }, contentRevision: 'r1',
    historyPayloadPreview: { detailsTruncated: true },
    reasoning: { text: reasoning.slice(0, 30), seconds: 0 },
    tool_calls: [{ type: 'text', text: 'Final answer', presentation: 'answer' }],
    activitySnapshotIncomplete: true,
  }])
  const summary = host.querySelector<HTMLDetailsElement>('.assistant-reasoning-summary')!
  expect(summary).not.toBeNull()
  expect(fetcher).not.toHaveBeenCalled()
  summary.querySelector<HTMLElement>('summary')!.click()
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce())
  expect(host.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded')).toBe('false')
  if (mode === 'closed') summary.querySelector<HTMLElement>('summary')!.click()
  if (mode === 'replaced') canonical.value = []
  await nextTick()
  release()
  for (let index = 0; index < 16; index++) { await Promise.resolve(); await nextTick() }
  expect(fetcher).toHaveBeenCalledOnce()
  if (mode === 'replaced') {
    expect(host.querySelector('.assistant-reasoning-summary')).toBeNull()
  } else if (mode === 'closed') {
    expect(canonical.value[0]?.reasoning?.text).toBe(reasoning.slice(0, 30))
  } else {
    expect(canonical.value[0]?.reasoning?.text).toBe(reasoning)
    expect(summary.querySelector('.thinking-block__body')?.textContent).toBe(reasoning)
    expect(canonical.value[0]?.historyPayloadPreview?.detailsTruncated).toBe(false)
  }
})
