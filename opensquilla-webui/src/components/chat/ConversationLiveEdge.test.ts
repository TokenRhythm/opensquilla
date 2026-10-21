// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import ConversationLiveEdge from './ConversationLiveEdge.vue'
import AssistantMessage from './AssistantMessage.vue'
import { projectAssistantActivity } from '@/utils/chat/assistantActivity'
import type { ChatRenderedMessage, ChatStreamTimelineItem } from '@/types/chat'

const apps: App[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; localStorage.clear(); vi.restoreAllMocks() })
const flush = async () => { for (let i = 0; i < 6; i++) await nextTick() }
const renderMarkdown = (text: string) => text
const toolUi = { isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
  toolGroupStatusText: () => 'Done', toolStatusText: () => 'Done', toolSecondaryText: () => 'README.md' }

function fixture(): ChatRenderedMessage {
  const timelineItems: ChatStreamTimelineItem[] = [{ type: 'tool-group', key: 'read', activityOrder: 3,
    group: { groupId: 'read', operationKey: 'file.read', label: 'Read file', iconName: 'fileText', secondary: 'README.md',
      isRunning: false, isError: false, status: 'success', calls: [{ toolId: 'read', renderKey: 'read', name: 'read_file',
        displayName: 'Read file', inputRaw: '{"path":"README.md"}', inputPreview: 'README.md',
        isRunning: false, isError: false, status: 'success', result: 'hello', resultPreview: 'hello', isOpen: false, activityOrder: 3 }] } }]
  return { role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant', text: 'Answer', messageId: 'answer', turnKey: 'turn',
    timeStr: '', showHeader: false, isStreaming: true, timelineItems,
    reasoningBlocks: [{ id: 'think', index: 0, text: 'Inspect the file first.', status: 'completed', startedAt: 1000, endedAt: 2000, contentKind: 'reasoning', activityOrder: 2 }],
    statusHistory: [
      { action: 'provider:requesting', label: '', at: 1000, endedAt: 2000, activityOrder: 1 },
      { action: 'provider:retry_wait:12', label: '', at: 3000, activityOrder: 4,
        providerRecovery: { phase: 'retry_wait', reason: 'transport_transient', startedAt: 3000, retryAfterMs: 12000 } },
      { action: 'context_compaction', label: '', at: 3500, id: 'compact', category: 'maintenance', state: 'completed', durability: 'durable', activityOrder: 5 },
    ],
  }
}

async function mountHost(mode: 'main-live' | 'transcript-live') {
  i18n.global.locale.value = 'en'
  const message = reactive(fixture())
  const toggle = vi.fn()
  const host = document.createElement('div'); document.body.appendChild(host)
  const app = createApp({ render() {
    const projection = projectAssistantActivity(message, renderMarkdown, [], { lifecycle: 'working', statusHistory: message.statusHistory })
    return mode === 'main-live'
      ? h(ConversationLiveEdge, { ...toolUi, visible: true, sessionKey: 'session', continuityKey: 'turn', executing: true,
        stepCount: 4, failureCount: 0, phaseLabel: '', elapsedLabel: '', stale: false, hasUnifiedActivityOrder: true,
        displayProjection: projection, timelineItems: projection.activityItems, reasoningBlocks: message.reasoningBlocks,
        renderMarkdown, onToggleItem: toggle })
      : h(AssistantMessage, { ...toolUi, message, index: 0, shareMode: false, shareSelected: false, shareMessageId: 'answer',
        sessionKey: 'session', renderMarkdown, fmtTok: String, toolCallGroups: () => [], copyMessage: async () => true,
        onToggleToolItem: toggle })
  } }).use(i18n).use(createPinia())
  apps.push(app); app.mount(host); await flush()
  return { host, message, toggle }
}

describe('ConversationLiveEdge mounted parity', () => {
  it.each(['main-live', 'transcript-live'] as const)('keeps running compaction visible after collapse (%s)', async mode => {
    vi.spyOn(Date, 'now').mockReturnValue(4000)
    const { host, message } = await mountHost(mode)
    message.statusHistory = [{ action: 'context_compaction', label: '', at: 3500,
      id: 'compact', category: 'maintenance', state: 'running', activityOrder: 5 }]
    await flush()

    const toggle = host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')!
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    toggle.click()
    await flush()

    const hint = host.querySelector('[data-testid="collapsed-compaction-status"]')
    expect(hint).not.toBeNull()
    expect(hint!.textContent).toContain(i18n.global.t('chat.compact.compacting'))
    const body = host.querySelector('.assistant-activity__body')!
    expect(body.getAttribute('aria-hidden')).toBe('true')
    expect(body.contains(hint)).toBe(false)

    message.statusHistory = [{ action: 'context_compaction', label: '', at: 3500,
      id: 'compact', category: 'maintenance', state: 'completed', durability: 'request_scoped', activityOrder: 5 }]
    await flush()
    expect(host.querySelector('[data-testid="collapsed-compaction-status"]')).toBeNull()
  })

  it('renders main and transcript live reasoning, tools, provider wait and compaction in the same order', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(4000)
    const main = await mountHost('main-live')
    const side = await mountHost('transcript-live')
    for (const surface of [main, side]) {
      expect(surface.host.querySelectorAll('.conversation-live-edge')).toHaveLength(1)
      expect(surface.host.querySelectorAll('.assistant-activity--live')).toHaveLength(1)
      expect(surface.host.textContent).toContain('README.md')
      expect(surface.host.textContent).toMatch(/retry|Retry/)
      expect(surface.host.textContent).toContain(i18n.global.t('chat.compact.compacted'))
      expect(surface.host.querySelectorAll('.reasoning-timeline')).toHaveLength(1)
    }
    const content = (host: HTMLElement) => host.querySelector('.assistant-activity__body')!.textContent!.replace(/\s+/g, ' ').trim()
    expect(content(side.host)).toBe(content(main.host))
    side.message.isStreaming = false; await flush()
    expect(side.host.querySelector('.conversation-live-edge')).toBeNull()
    expect(side.host.querySelectorAll('.assistant-activity--settled')).toHaveLength(1)
    expect(side.host.textContent).toContain('Answer')
  })
})
