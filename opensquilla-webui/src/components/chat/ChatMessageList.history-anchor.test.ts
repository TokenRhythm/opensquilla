// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ChatMessageList history anchors', () => {
  it('renders the same stable user-message anchor consumed by the minimap', () => {
    const userMessage: ChatRenderedMessage = {
      id: 'rendered-user-1',
      messageId: 'message-user-1',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'user',
      text: 'Remember this requirement',
      timeStr: '',
      showHeader: false,
    }
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatMessageList, {
      messages: [userMessage],
      shareMode: false,
      selectedMessageIds: new Set<string>(),
      stripTimePrefix: (value: string) => value,
      renderMarkdown: (value: string) => value,
      fmtTok: (value: number) => String(value),
      subagentSummary: (value: string) => value,
      subagentBody: (value: string) => value,
      toolCallGroups: () => [],
      isToolGroupOpen: () => false,
      isToolItemOpen: () => false,
      toolGroupStatusText: () => '',
      toolStatusText: () => '',
      toolSecondaryText: () => '',
      copyMessage: async () => true,
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)

    const anchor = host.querySelector<HTMLElement>('.msg-user')
    expect(anchor?.id).toBe('chat-turn-0')
    expect(anchor?.dataset.chatTurnKey).toBe('message-user-1')
    expect(anchor?.tabIndex).toBe(-1)
  })

  it('renders a durable manual compaction as a neutral transcript event', () => {
    const maintenanceMessage: ChatRenderedMessage = {
      id: 'maintenance-1',
      messageId: 'maintenance:context-compaction:summary:7',
      role: 'maintenance',
      displayRole: 'maintenance',
      roleLabel: 'Maintenance',
      text: '',
      timeStr: 'just now',
      showHeader: false,
      maintenance: {
        kind: 'context_compaction',
        compactionId: 'cmp-7',
        source: 'manual',
        state: 'completed',
        durability: 'durable',
      },
    }
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatMessageList, {
      messages: [maintenanceMessage],
      shareMode: false,
      selectedMessageIds: new Set<string>(),
      stripTimePrefix: (value: string) => value,
      renderMarkdown: (value: string) => value,
      fmtTok: (value: number) => String(value),
      subagentSummary: (value: string) => value,
      subagentBody: (value: string) => value,
      toolCallGroups: () => [],
      isToolGroupOpen: () => false,
      isToolItemOpen: () => false,
      toolGroupStatusText: () => '',
      toolStatusText: () => '',
      toolSecondaryText: () => '',
      copyMessage: async () => true,
      downloadAttachment: async () => true,
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)

    const event = host.querySelector<HTMLElement>('[data-testid="compaction-event"]')
    expect(event?.dataset).toMatchObject({
      compactionId: 'cmp-7',
      status: 'completed',
      source: 'manual',
      durability: 'durable',
      placement: 'transcript',
    })
    expect(event?.textContent).toContain('Context organized')
    expect(host.querySelector('.msg-system')).toBeNull()
    expect(host.querySelector('.msg-ai')).toBeNull()
  })
})
