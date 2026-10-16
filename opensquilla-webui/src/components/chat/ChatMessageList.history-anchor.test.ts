// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ChatMessageList history anchors', () => {
  it('keeps simultaneous session anchors unique while preserving scoped message keys', () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(defineComponent({ setup: () => () => h('div', ['parent', 'child'].map(session =>
      h(ChatMessageList, {
        sessionKey: `agent:main:webchat:${session}`,
        messages: [{ id: 'same-row', messageId: 'same-message', role: 'user', displayRole: 'user',
          roleLabel: 'user', text: session, timeStr: '', showHeader: false }],
        shareMode: false, selectedMessageIds: new Set<string>(), stripTimePrefix: value => value,
        renderMarkdown: value => value, fmtTok: value => String(value), subagentSummary: value => value,
        subagentBody: value => value, toolCallGroups: () => [], isToolGroupOpen: () => false,
        isToolItemOpen: () => false, toolGroupStatusText: () => '', toolStatusText: () => '',
        toolSecondaryText: () => '', copyMessage: async () => true, downloadAttachment: async () => true,
      }),
    )) })).use(i18n)
    apps.push(app)
    app.mount(host)
    const anchors = [...host.querySelectorAll<HTMLElement>('[data-chat-turn-key]')]
    expect(anchors).toHaveLength(2)
    expect(new Set(anchors.map(anchor => anchor.id)).size).toBe(2)
    expect(anchors[0].id).toContain(encodeURIComponent('agent:main:webchat:parent'))
    expect(anchors[1].id).toContain(encodeURIComponent('agent:main:webchat:child'))
    expect(anchors.map(anchor => anchor.dataset.chatTurnKey)).toEqual(['same-message', 'same-message'])
  })

  it('adds an applied result only after an authoritative mutation outcome arrives', async () => {
    const userMessage: ChatRenderedMessage = {
      id: 'rendered-user-annotation',
      messageId: 'message-user-annotation',
      turnId: 'turn-annotation',
      turnKey: 'turn:turn-annotation',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'user',
      text: '',
      timeStr: '',
      showHeader: false,
      turnOutcome: { turnId: 'turn-annotation', status: 'succeeded' },
      promptAnnotations: [{
        annotationId: 'annotation-1',
        documentId: 'document-1',
        documentName: 'page.html',
        revisionId: 'revision-1',
        generation: 1,
        anchorId: 'anchor-1',
        body: 'Make the button red.',
        tagName: 'button',
        locator: {},
        quote: '<button>',
        sourceExcerpt: null,
        sentOrder: 0,
      }],
    }
    const messages = ref<ChatRenderedMessage[]>([userMessage])
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(defineComponent({
      setup: () => () => h(ChatMessageList, {
        messages: messages.value,
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
      }),
    }))
    app.use(i18n)
    app.mount(host)
    apps.push(app)

    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')).toBeNull()

    messages.value = [{
      ...userMessage,
      turnOutcome: {
        turnId: 'turn-annotation',
        status: 'succeeded',
        documentMutationOutcome: { version: 1, status: 'applied' },
      },
    }]
    await nextTick()

    expect(host.querySelector('[data-testid="prompt-annotation-turn-status"]')
      ?.getAttribute('data-status')).toBe('applied')
  })

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
    expect(anchor?.id).toMatch(/^chat-turn-unbound-.+-0$/)
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
        historyArchived: true,
        canonicalComplete: true,
      },
    }
    const incompleteMessage: ChatRenderedMessage = {
      ...maintenanceMessage,
      id: 'maintenance-2',
      messageId: 'maintenance:context-compaction:summary:8',
      maintenance: {
        ...maintenanceMessage.maintenance!,
        compactionId: 'cmp-8',
        canonicalComplete: false,
      },
    }
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatMessageList, {
      messages: [maintenanceMessage, incompleteMessage],
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
    expect(event?.textContent).toBe('Context organizedjust now')
    expect(event?.querySelector('details')).toBeNull()
    const events = host.querySelectorAll<HTMLElement>('[data-testid="compaction-event"]')
    expect(events[1]?.textContent).toContain(
      'Earlier context summarized; some original messages are unavailable',
    )
    expect(host.querySelector('.msg-system')).toBeNull()
    expect(host.querySelector('.msg-ai')).toBeNull()
  })

  it('renders skipped compaction reasons truthfully in the transcript', () => {
    const makeMessage = (id: string, reason: string): ChatRenderedMessage => ({
      id,
      messageId: `maintenance:context-compaction:${id}`,
      role: 'maintenance',
      displayRole: 'maintenance',
      roleLabel: 'Maintenance',
      text: '',
      timeStr: '',
      showHeader: false,
      maintenance: {
        kind: 'context_compaction',
        compactionId: id,
        source: 'manual',
        state: 'skipped',
        durability: 'none',
        reason,
      },
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatMessageList, {
      messages: [
        makeMessage('cmp-within', 'within_compaction_budget'),
        makeMessage('cmp-no-history', 'no_safe_turn_boundary'),
        makeMessage('cmp-vetoed', 'non_history_envelope_exhausts_budget'),
      ],
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

    const events = host.querySelectorAll<HTMLElement>('[data-testid="compaction-event"]')
    expect(events[0]?.textContent).toContain('No organization needed; context has enough space')
    expect(events[1]?.textContent).toContain('There is currently no history that can be safely organized')
    expect(events[2]?.textContent).toContain('Context organization was not applied')
  })
})
