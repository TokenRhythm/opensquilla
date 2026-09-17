// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, type App } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import CompactionEvent from '@/components/chat/CompactionEvent.vue'
import type { ChatRenderedMessage } from '@/types/chat'
import compactionEventSource from '../components/chat/CompactionEvent.vue?raw'
import chatViewSource from './ChatView.vue?raw'
import chatViewStyles from '../styles/chat-view.css?raw'

const apps: App[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('compaction maintenance presentation', () => {
  it.each([
    ['en', 'History temporarily reduced; continuing', 'Summary saved'],
    ['zh-Hans', '已临时精简历史，继续处理', '摘要已保存'],
  ] as const)('labels temporary and saved transcript events truthfully in %s after restore', async (locale, temporary, saved) => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    for (const restoredFromHistory of [false, true]) {
      for (const durability of ['request_scoped', 'durable']) {
        const message: ChatRenderedMessage = {
          id: `cmp-${durability}-${restoredFromHistory}`,
          role: 'maintenance', displayRole: 'maintenance', roleLabel: '',
          text: '', timeStr: '', showHeader: false, restoredFromHistory,
          maintenance: {
            kind: 'context_compaction', compactionId: 'cmp-transcript',
            source: 'automatic', state: 'completed', durability,
          },
        }
        const host = document.createElement('div')
        document.body.appendChild(host)
        const app = createApp(CompactionEvent, { message })
        apps.push(app)
        app.use(i18n)
        app.mount(host)

        const event = host.querySelector<HTMLElement>('[data-testid="compaction-event"]')!
        expect(event.dataset.status).toBe('completed')
        expect(event.dataset.durability).toBe(durability)
        expect(event.querySelector('.chat-compaction-event__title')?.textContent)
          .toBe(durability === 'request_scoped' ? temporary : saved)
        expect(event.classList.contains('chat-compaction-event--running')).toBe(false)
        expect(event.getAttribute('aria-live')).toBe(restoredFromHistory ? null : 'polite')
        if (durability === 'request_scoped') expect(event.textContent).not.toContain(saved)
      }
    }
  })

  it('renders one neutral transcript event without a fabricated progress surface', () => {
    expect(chatViewSource).toContain('class="chat-compaction-event"')
    expect(chatViewSource).toContain('data-testid="compaction-event"')
    expect(chatViewSource).toContain('data-placement="turn-boundary"')
    expect(chatViewSource).not.toContain('class="chat-compact-status"')
    expect(chatViewSource).not.toContain('role="progressbar"')
    expect(chatViewStyles).not.toContain('.chat-compact-status__gauge')
    expect(chatViewStyles).not.toContain('compactGaugeIndeterminate')
  })

  it('uses only a small running marker and respects reduced motion', () => {
    // The transcript event is rendered through ChatMessageList, outside
    // ChatView's scoped-style boundary, so the component must own these rules.
    expect(compactionEventSource).toContain('<style scoped>')
    expect(compactionEventSource).toMatch(
      /\.chat-compaction-event\s*\{[^}]*min-height:\s*1\.75rem[^}]*font-size:\s*var\(--fs-xs\)/s,
    )
    expect(compactionEventSource).toMatch(
      /\.chat-compaction-event--running \.chat-compaction-event__marker\s*\{[^}]*animation:\s*compactionEventSpin/s,
    )
    expect(compactionEventSource).toMatch(/prefers-reduced-motion:\s*reduce[\s\S]*chat-compaction-event--running[\s\S]*animation:\s*none/)
    expect(compactionEventSource).not.toMatch(/\.chat-compaction-event\s*\{[^}]*box-shadow/s)
  })
})
