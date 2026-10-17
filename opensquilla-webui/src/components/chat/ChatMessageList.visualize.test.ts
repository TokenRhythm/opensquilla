// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia } from 'pinia'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import { parseVisualizeReceipt } from '@/visualize/receipts'
import type { VisualizeLivePublications } from '@/visualize/versions'
import ChatMessageList from './ChatMessageList.vue'

vi.mock('./AssistantMessage.vue', async () => {
  const { defineComponent, h, inject } = await import('vue')
  const { VISUALIZE_VERSIONS_KEY } = await import('@/visualize/versions')
  const { visualizeReceiptsFromMessage } = await import('@/visualize/receipts')
  return { default: defineComponent({
    props: ['message', 'sessionKey'],
    setup(props) {
      const versions = inject(VISUALIZE_VERSIONS_KEY)!
      return () => h('div', { 'data-history-visualize': true }, visualizeReceiptsFromMessage(props.message)
        .map(receipt => versions.isHistorical(props.sessionKey, receipt) ? 'historical' : 'current').join(','))
    },
  }) }
})

const apps: App[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.replaceChildren(); sessionStorage.clear() })
function call(version: number): ChatToolCall {
  return { name: version === 1 ? 'show_widget' : 'edit_widget', toolId: `call_${version}`,
    status: 'success', isRunning: false, isError: false,
    result: JSON.stringify({ documentId: 'doc_budget', revisionId: `rev_${version}`, version,
      widgetId: 'budget', title: 'Budget', size: 100, sha256: 'a'.repeat(64), marker: `visualize:doc_budget:rev_${version}` }),
  } as ChatToolCall
}

describe('ChatMessageList live Visualize version ownership', () => {
  it('provides a trailing live edit to historical rows before the turn enters messages', async () => {
    const oldMessage: ChatRenderedMessage = {
      id: 'old_answer', messageId: 'old_answer', role: 'assistant', displayRole: 'assistant',
      roleLabel: 'Assistant', showHeader: false, timeStr: '', text: 'Budget', toolCalls: [call(1)],
    }
    const owner = reactive({ live: undefined as VisualizeLivePublications | undefined })
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ render: () => h(ChatMessageList, {
      messages: [oldMessage], sessionKey: 'session_one', liveVisualizePublications: owner.live,
      shareMode: false, selectedMessageIds: new Set<string>(),
      stripTimePrefix: (text: string) => text, renderMarkdown: (text: string) => text,
      fmtTok: String, subagentSummary: (text: string) => text, subagentBody: (text: string) => text,
      toolCallGroups: () => [], isToolGroupOpen: () => false, isToolItemOpen: () => false,
      toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      copyMessage: async () => true, downloadAttachment: async () => true,
    }) })
    app.provide(GATEWAY_ACCESS_KEY, { deliveryIdentity: 'account_one' } as GatewayAccess)
    app.use(i18n); app.use(createPinia()); apps.push(app); app.mount(host); await nextTick()
    expect(host.querySelector('[data-history-visualize]')?.textContent).toBe('current')
    owner.live = { sessionKey: 'session_one', deliveryIdentity: 'account_one', receipts: [parseVisualizeReceipt(call(2))!] }
    await nextTick()
    expect(oldMessage.toolCalls).toHaveLength(1)
    expect(host.querySelector('[data-history-visualize]')?.textContent).toBe('historical')
  })
})
