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
import ToolCallTimeline from './ToolCallTimeline.vue'
import { createPinia } from 'pinia'

const mountedApps: App[] = []

function toolCall(): ChatToolCallRenderItem {
  return {
    toolId: 'shared-tool',
    renderKey: 'shared-tool',
    name: 'custom_tool',
    displayName: 'Custom tool',
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
      operationKey: 'tool.custom',
      label: 'Custom tool',
      iconName: 'gear',
      calls: [toolCall()],
      secondary: '',
      isRunning: false,
      isError: false,
      status: 'success',
    },
  }]
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  useToolDetailPreference().setMode('collapsed')
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('AssistantMessage tool disclosure scope', () => {
  it('keeps a live choice when the completed message remounts with a different render key', async () => {
    const el = document.createElement('div')
    document.body.appendChild(el)
    const settled = ref(false)
    const choices = reactive(new Map<string, boolean>())
    const items = timelineItems()
    if (items[0]?.type === 'tool-group') items[0].group.calls[0]!.renderKey = 'live:tool:shared-tool:0'
    const message: ChatRenderedMessage = {
      id: 'assistant-history',
      role: 'assistant',
      displayRole: 'assistant',
      roleLabel: 'Assistant',
      text: '',
      timeStr: '',
      showHeader: false,
      turnKey: 'turn:owning-user',
      timelineItems: timelineItems(),
    }
    const common = {
      isToolGroupOpen: () => undefined,
      isToolItemOpen: (id: string) => choices.get(id),
      toolGroupStatusText: () => '',
      toolStatusText: () => '',
      toolSecondaryText: () => '',
    }
    const writeChoice = (id: string, open: boolean) => choices.set(id, open)
    const Host = defineComponent({
      setup: () => () => settled.value
        ? h(AssistantMessage, {
            ...common,
            message,
            index: 0,
            sessionKey: 'session-a',
            shareMode: false,
            shareSelected: false,
            shareMessageId: 'assistant-history',
            renderMarkdown: (text: string) => text,
            fmtTok: String,
            toolCallGroups: () => [],
            copyMessage: async () => true,
            onToggleToolItem: writeChoice,
          })
        : h(ToolCallTimeline, {
            ...common,
            items,
            stateScope: JSON.stringify(['session-a', 'turn:owning-user']),
            onToggleItem: writeChoice,
          }),
    })
    const app = createApp(Host)
    mountedApps.push(app)
    app.use(i18n)
    app.use(createPinia())
    app.mount(el)
    await nextTick()
    const row = () => el.querySelector<HTMLButtonElement>('.tool-row')
    expect(row()?.getAttribute('aria-expanded')).toBe('false')
    row()?.click()
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('true')
    settled.value = true
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('true')
    expect(choices.get(`${JSON.stringify(['session-a', 'turn:owning-user'])}:shared-tool`)).toBe(true)
    useToolDetailPreference().setMode('expanded')
    await nextTick()
    row()?.click()
    await nextTick()
    useToolDetailPreference().setMode('collapsed')
    await nextTick()
    useToolDetailPreference().setMode('expanded')
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe('false')
  })

  it.each(['expanded', 'collapsed'] as const)('does not carry fallback message ids across sessions in %s mode', async mode => {
    useToolDetailPreference().setMode(mode)
    const initiallyOpen = mode === 'expanded'
    const el = document.createElement('div')
    document.body.appendChild(el)
    const sessionKey = ref('session-a')
    const itemToggles = reactive(new Map<string, boolean>())
    const onToggleToolItem = vi.fn((id: string, open: boolean) => itemToggles.set(id, open))
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
          isToolGroupOpen: () => undefined,
          isToolItemOpen: (id: string) => itemToggles.get(id),
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
    app.use(createPinia())
    app.mount(el)
    await nextTick()

    const row = () => el.querySelector<HTMLButtonElement>('.tool-row')
    expect(row()?.getAttribute('aria-expanded')).toBe(String(initiallyOpen))

    row()?.click()
    await nextTick()
    expect(onToggleToolItem).toHaveBeenLastCalledWith(
      `${JSON.stringify(['session-a', 'assistant-0'])}:shared-tool`,
      !initiallyOpen,
    )
    expect(row()?.getAttribute('aria-expanded')).toBe(String(!initiallyOpen))

    sessionKey.value = 'session-b'
    await nextTick()
    expect(row()?.getAttribute('aria-expanded')).toBe(String(initiallyOpen))

    row()?.click()
    await nextTick()
    expect(onToggleToolItem).toHaveBeenLastCalledWith(
      `${JSON.stringify(['session-b', 'assistant-0'])}:shared-tool`,
      !initiallyOpen,
    )
    expect(itemToggles.size).toBe(2)
  })
})
