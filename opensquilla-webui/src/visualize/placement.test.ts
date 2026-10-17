// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import AssistantMessage from '@/components/chat/AssistantMessage.vue'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
vi.mock('@/components/chat/VisualizeCanvas.vue', () => ({ default: {
  props: ['receipt', 'sessionKey'],
  template: '<section class="test-canvas">{{ receipt.name }}</section>',
} }))
const apps: App[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.replaceChildren() })
const call = (id: string): ChatToolCall => ({ toolId: `call_${id}`, name: 'open_workspace_preview', displayName: 'Preview', inputPreview: '', resultPreview: '', isOpen: false, isRunning: false, isError: false, status: 'success', result: JSON.stringify({ previewStatus: 'inline-ready', presentation: 'inline', documentId: `doc_${id}`, revisionId: `rev_${id}`, artifactId: `artifact_${id}`, name: `${id}.html`, mime: 'text/html', size: 10, sha256: 'a'.repeat(64), marker: `visualize:doc_${id}:rev_${id}` }) })
describe('Visualize whole-message placement', () => {
  it('deduplicates timeline and history receipts and keeps two canvases between their own paragraphs', async () => {
    const calls = [call('first'), call('second')]
    const message: ChatRenderedMessage = { role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant', timeStr: '', showHeader: false,
      text: 'Before\n\n`visualize:doc_first:rev_first`\n\nMiddle\n\n`visualize:doc_second:rev_second`\n\nAfter',
      toolCalls: calls, timelineItems: [{ type: 'tool-group', key: 'tools', group: { groupId: 'tools', operationKey: 'preview', label: 'Preview', iconName: 'chat', calls: calls.map(item => ({ ...item, renderKey: item.toolId })), secondary: '', isRunning: false, isError: false, status: 'success' } }],
    }
    const { renderMarkdown } = useChatTextRendering()
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ render: () => h(AssistantMessage, { message, index: 0, shareMode: false, shareSelected: false, shareMessageId: 'test', sessionKey: 'session_test', renderMarkdown,
      fmtTok: String, toolCallGroups: () => [], isToolGroupOpen: () => false, isToolItemOpen: () => false, toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '', copyMessage: async () => true }) })
    apps.push(app); app.use(i18n); app.mount(host); await nextTick()
    expect(host.querySelectorAll('.test-canvas')).toHaveLength(2)
    expect(host.querySelector('.assistant-answer')?.textContent).toMatch(/Before.*first.html.*Middle.*second.html.*After/s)
  })
})
