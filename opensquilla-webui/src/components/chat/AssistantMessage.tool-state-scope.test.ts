// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, ref, type App } from 'vue'

import i18n from '@/i18n'
import type {
  ChatRenderedMessage,
  ChatStreamTimelineItem,
  ChatToolCallRenderItem,
} from '@/types/chat'
import { useToolDetailPreference } from '@/composables/useToolDetailPreference'
import AssistantMessage from './AssistantMessage.vue'

const mountedApps: App[] = []

function toolCall(): ChatToolCallRenderItem {
  return {
    toolId: 'shared-tool',
    renderKey: 'shared-tool',
    name: 'web_search',
    displayName: 'Search',
    inputRaw: '{}',
    inputPreview: '{}',
    isRunning: false,
    status: 'success',
    isError: false,
    result: 'ok',
    resultPreview: 'ok',
    isOpen: false,
  }
}

function timelineItems(): ChatStreamTimelineItem[] {
  return [{
    type: 'tool-group',
    key: 'shared-group',
    group: {
      groupId: 'shared-group',
      operationKey: 'web.search',
      label: 'Search',
      iconName: 'search',
      calls: [toolCall()],
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }]
}

function flip(values: Set<string>, key: string): void {
  if (values.has(key)) values.delete(key)
  else values.add(key)
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  useToolDetailPreference().setMode('auto')
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('AssistantMessage tool disclosure scope', () => {
  it('does not carry fallback message ids across sessions', async () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const sessionKey = ref('session-a')
    const itemToggles = reactive(new Set<string>())
    const onToggleToolItem = vi.fn((id: string) => flip(itemToggles, id))
    const message: ChatRenderedMessage = {
      role: 'assistant',
      displayRole: 'assistant',
      roleLabel: 'Assistant',
      text: '',
      timeStr: '',
      ts: null,
      sourceIndex: 0,
      showHeader: false,
      timelineItems: timelineItems(),
    }

    const Host = defineComponent({
      setup() {
        return () => h(AssistantMessage, {
          message,
          index: 0,
          sessionKey: sessionKey.value,
          shareMode: false,
          shareSelected: false,
          shareMessageId: 'assistant-0',
          renderMarkdown: (text: string) => text,
          fmtTok: (value: number) => String(value),
          toolCallGroups: () => [],
          isToolGroupOpen: () => false,
          isToolItemOpen: (id: string) => itemToggles.has(id),
          toolGroupStatusText: () => '',
          toolStatusText: () => '',
          toolSecondaryText: () => '',
          copyMessage: async () => true,
          onToggleToolItem,
        })
      },
    })

    const app = createApp(Host)
    mountedApps.push(app)
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const row = () => el.querySelector<HTMLButtonElement>('.tool-row')
    expect(row()?.getAttribute('aria-expanded')).toBe('false')

    row()?.click()
    await nextTick()
    expect(onToggleToolItem).toHaveBeenLastCalledWith(
      `${JSON.stringify(['session-a', 'assistant-0'])}:shared-tool`,
    )
    expect(row()?.getAttribute('aria-expanded')).toBe('true')

    sessionKey.value = 'session-b'
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('false')

    row()?.click()
    await nextTick()
    expect(onToggleToolItem).toHaveBeenLastCalledWith(
      `${JSON.stringify(['session-b', 'assistant-0'])}:shared-tool`,
    )
    expect(itemToggles.size).toBe(2)
  })
})
