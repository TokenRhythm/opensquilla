// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, ref, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import type { ChatMessage, ChatRenderedMessage, RawToolCallPayload } from '@/types/chat'
import { useChatHistoryDetails } from '@/composables/chat/useChatHistoryDetails'
import { useChatRenderedMessages } from '@/composables/chat/useChatRenderedMessages'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import { ContentRangeCache } from '@/utils/chat/contentRangeCache'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { createV4ArtifactContentAccess } from '@/adapters/gateway/artifactAccessV4'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import ChatMessageList from './ChatMessageList.vue'

const apps: App[] = []
const sessionKey = 'agent:main:webchat:automatic-content'
const message = (overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage => ({
  id: 'answer', messageId: 'answer', role: 'assistant', displayRole: 'assistant',
  roleLabel: 'Assistant', text: 'PREVIEW_ONLY', timeStr: '', showHeader: false,
  previewComplete: false,
  contentRef: { version: 1, sessionKey, sessionId: 'session', messageId: 'answer', view: 'display', byteLength: 32768 },
  ...overrides,
})

async function mount(messages: ChatRenderedMessage[], historyMessages?: ChatMessage[]) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const { renderMarkdown } = useChatTextRendering()
  const canonical = ref(historyMessages ?? [])
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
    const key = ref(sessionKey)
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
      onToggleToolGroup: toggle, onToggleToolItem: toggle, onShowToolResult: showResult,
    })
  } })
  const http = httpTransportTestDouble()
  app.use(i18n)
  app.use(createPinia())
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
  clearAssistantActivityExpansionState()
})

describe('automatic content through the real assistant renderer', () => {
  it.each(['immediate', 'delayed', 'closed', 'replaced', 'failed'])('loads complete details and shares an existing read with the full viewer (%s)', async mode => {
    const reasoning = '深入思考🙂'.repeat(3_000)
    const input = { data: 'long input '.repeat(3_000) }
    const result = 'full-result-'.repeat(3_000)
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
      return mode === 'immediate' ? Promise.resolve(response())
        : new Promise((resolve, reject) => { release = () => mode === 'failed'
          ? reject(new Error('details unavailable')) : resolve(response()) })
    })
    const bodyRead = vi.spyOn(ContentRangeCache.prototype, 'readDisplay')
    const { host, canonical, showResult } = await mount([], [{
      role: 'assistant', text: 'Final answer', messageId: 'answer', ts: 1, previewComplete: true,
      contentRef: { ...message().contentRef!, revision: 'r1' }, contentRevision: 'r1',
      historyPayloadPreview: { detailsTruncated: true }, reasoning: { text: reasoning.slice(0, 30), seconds: 3 },
      tool_calls: [{ ...segments[0], input: { data: 'preview data '.repeat(60) } },
        { ...segments[1], result: 'preview result '.repeat(60) }, segments[2]],
    }])
    expect(fetcher).not.toHaveBeenCalled()
    expect(bodyRead).not.toHaveBeenCalled()
    const activity = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')!
    expect(activity).not.toBeNull()
    activity.click()
    if (mode !== 'immediate') {
      await nextTick()
      const row = host.querySelector<HTMLButtonElement>('.tool-row')!
      if (row.getAttribute('aria-expanded') !== 'true') row.click()
      await nextTick()
      host.querySelector<HTMLButtonElement>('.activity-tool-details__view')!.click()
      expect(showResult).not.toHaveBeenCalled()
      if (mode === 'closed') activity.click()
      if (mode === 'replaced') canonical.value = []
      await nextTick()
      release()
    }
    for (let i = 0; i < 16; i++) { await Promise.resolve(); await nextTick() }
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]?.[0]).toContain('view=details&export=1')
    if (mode === 'closed' || mode === 'replaced' || mode === 'failed') {
      expect(showResult).not.toHaveBeenCalled()
      if (mode !== 'replaced') expect(canonical.value[0]?.reasoning?.text).toBe(reasoning.slice(0, 30))
      return
    }
    expect(canonical.value[0]?.reasoning?.text).toBe(reasoning)
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe(reasoning)
    if (mode === 'immediate') {
      const toolRow = host.querySelector<HTMLButtonElement>('.tool-row')!
      expect(toolRow).not.toBeNull()
      if (toolRow.getAttribute('aria-expanded') !== 'true') toolRow.click()
      await nextTick()
      const fullButton = host.querySelector<HTMLButtonElement>('.activity-tool-details__view')!
      expect(fullButton).not.toBeNull()
      fullButton.click()
    }
    expect(showResult.mock.calls[0]?.[0]).toContain(input.data)
    expect(showResult.mock.calls[0]?.[0]).toContain(result.trimEnd())
    expect(host.querySelector('.chat-history-content-page')).toBeNull()
    expect(host.querySelector('[data-testid="chat-history-content-hydration"]')).toBeNull()
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
