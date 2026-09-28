// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import CompactionEvent from '@/components/chat/CompactionEvent.vue'
import type { ChatRenderedMessage } from '@/types/chat'
import compactionEventSource from '../components/chat/CompactionEvent.vue?raw'
import chatViewSource from './ChatView.vue?raw'
import chatViewStyles from '../styles/chat-view.css?raw'
import { copyTextWithFallback } from '@/utils/browser'

vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn().mockResolvedValue(undefined) }))

const apps: App[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
  vi.clearAllMocks()
})

describe('compaction maintenance presentation', () => {
  it.each([
    ['en', 'The current context is already concise', 'The summary did not pass the completeness check'],
    ['zh-Hans', '当前上下文已足够精简', '摘要未通过完整性校验'],
  ] as const)('distinguishes no further benefit from a real quality rejection in %s', async (locale, concise, rejected) => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    for (const [reason, state, title] of [
      ['no_compression_benefit', 'skipped', concise],
      ['quality_gate_failed', 'failed', rejected],
    ] as const) {
      const message: ChatRenderedMessage = {
        id: reason, role: 'maintenance', displayRole: 'maintenance', roleLabel: '',
        text: '', timeStr: '', showHeader: false,
        maintenance: { kind: 'context_compaction', compactionId: `cmp-${reason}`, source: 'manual', durability: 'none', state, reason },
      }
      const host = document.createElement('div')
      document.body.appendChild(host)
      const app = createApp(CompactionEvent, { message })
      apps.push(app)
      app.use(i18n)
      app.mount(host)
      const event = host.querySelector<HTMLElement>('[data-testid="compaction-event"]')!
      expect(event.querySelector('.chat-compaction-event__title')?.textContent).toBe(title)
      expect(event.getAttribute('role')).toBe(state === 'failed' ? 'alert' : 'status')
      expect(event.classList.contains('chat-compaction-event--failed')).toBe(state === 'failed')
      expect(event.classList.contains('chat-compaction-event--running')).toBe(false)
      expect(event.textContent).not.toContain(reason)
    }
  })

  it.each([
    ['en', 'There is currently no history that can be safely organized'],
    ['zh-Hans', '当前没有可安全整理的历史'],
  ] as const)('presents a safe no-op as a normal maintenance result in %s', async (locale, title) => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    for (const reason of ['no_entries', 'no_safe_turn_boundary', 'protected_tail_exhausts_compaction_window']) {
      const message: ChatRenderedMessage = {
        id: reason, role: 'maintenance', displayRole: 'maintenance', roleLabel: '',
        text: '', timeStr: '', showHeader: false,
        maintenance: {
          kind: 'context_compaction', compactionId: `cmp-${reason}`, source: 'manual',
          state: 'skipped', durability: 'none', reason,
        },
      }
      const host = document.createElement('div')
      document.body.appendChild(host)
      const app = createApp(CompactionEvent, { message })
      apps.push(app)
      app.use(i18n)
      app.mount(host)
      const event = host.querySelector<HTMLElement>('[data-testid="compaction-event"]')!
      expect(event.querySelector('.chat-compaction-event__title')?.textContent).toBe(title)
      expect(event.dataset.status).toBe('skipped')
      expect(event.getAttribute('role')).toBe('status')
      expect(event.getAttribute('aria-live')).toBe('polite')
      expect(event.classList.contains('chat-compaction-event--failed')).toBe(false)
      expect(event.classList.contains('chat-compaction-event--running')).toBe(false)
      expect(event.querySelector('.chat-compaction-event__diagnostic')).toBeNull()
    }
  })

  it.each([
    ['en', 'The summary did not pass the completeness check', 'your original context is preserved'],
    ['zh-Hans', '摘要未通过完整性校验', '原上下文已保留'],
  ] as const)('explains a rejected manual summary and copies its diagnostic ID in %s', async (locale, title, preservation) => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    const message: ChatRenderedMessage = {
      id: 'failed-compact', role: 'maintenance', displayRole: 'maintenance', roleLabel: '',
      text: '', timeStr: '', showHeader: false,
      maintenance: {
        kind: 'context_compaction', compactionId: 'cmp-failed-123', source: 'manual',
        state: 'failed', durability: 'none', reason: 'summary_replay_incomplete',
      },
    }
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(CompactionEvent, { message })
    apps.push(app)
    app.use(i18n)
    app.mount(host)

    expect(host.querySelector('.chat-compaction-event__title')?.textContent).toBe(title)
    expect(host.textContent).toContain(preservation)
    expect(host.textContent).not.toContain('summary_replay_incomplete')
    const copy = host.querySelector<HTMLButtonElement>('.chat-compaction-event__diagnostic')!
    copy.click()
    await nextTick()
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith('cmp-failed-123')
    await vi.waitFor(() => expect(copy.textContent).toBe(i18n.global.t('chat.copiedDiagnosticId')))
  })

  it.each([
    ['en', 'History temporarily reduced; continuing', 'Context organized'],
    ['zh-Hans', '已临时精简历史，继续处理', '上下文已整理'],
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
            historyArchived: true, canonicalComplete: true,
            detail: 'Earlier context summarized; original messages remain available in history',
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
        expect(event.querySelector('.chat-compaction-event__body .chat-compaction-event__detail')).toBeNull()
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
