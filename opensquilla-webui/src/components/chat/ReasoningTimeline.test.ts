// @vitest-environment happy-dom
import { createApp, h, nextTick, ref, type Ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ReasoningTimeline from './ReasoningTimeline.vue'
import type { ReasoningBlock } from '@/types/turnlog'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        thinkingForSeconds: 'Thinking · {seconds}s',
        thoughtProcess: 'Thinking',
        thoughtForSeconds: 'Thought for {seconds}s',
        thoughtForMinutes: 'Thought for {minutes}m {seconds}s',
      },
    },
  },
})

function block(
  id: string,
  index: number,
  text: string,
  status: ReasoningBlock['status'] = 'streaming',
): ReasoningBlock {
  return {
    id,
    index,
    text,
    status,
    startedAt: 1_000,
    ...(status === 'streaming' ? {} : { endedAt: 2_000 }),
    contentKind: 'reasoning',
  }
}

function mount(blocks: Ref<ReasoningBlock[]>, collapseActive: Ref<boolean> = ref(false), options: {
  sessionKey?: string
  executing?: boolean
} = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ReasoningTimeline, {
    blocks: blocks.value,
    collapseActive: collapseActive.value,
    executing: true,
    ...options,
  }) })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => clearAssistantActivityExpansionState())

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  vi.useRealTimers()
  clearAssistantActivityExpansionState()
})

describe('ReasoningTimeline disclosure ownership', () => {
  it('starts compact and keeps manual open and close stable across deltas', async () => {
    const blocks = ref([block('r1', 0, 'plan')])
    const host = mount(blocks)

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    blocks.value = [block('r1', 0, 'plan update')]
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)

    blocks.value = [block('r1', 0, 'plan more')]
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
  })

  it('keeps the manually opened previous block when a new block starts', async () => {
    const blocks = ref([block('r1', 0, 'plan')])
    const host = mount(blocks)

    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()

    blocks.value = [
      block('r1', 0, 'plan', 'completed'),
      block('r2', 1, 'review'),
    ]
    await nextTick()

    const folds = host.querySelectorAll<HTMLDetailsElement>('details')
    expect(folds).toHaveLength(2)
    expect(folds[0]?.open).toBe(true)
    expect(folds[1]?.open).toBe(false)
  })

  it('keeps an untouched block collapsed when it completes', async () => {
    const blocks = ref([block('r1', 0, 'plan')])
    const host = mount(blocks)

    blocks.value = [block('r1', 0, 'plan', 'completed')]
    await nextTick()

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
    expect(host.textContent).toContain('Thought for 1s')

    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    expect(host.textContent).toContain('plan')
  })

  it('retains the manual opening and freezes duration when answer organization begins', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(6_000)
    const blocks = ref([block('r1', 0, 'plan')])
    const collapseActive = ref(false)
    const host = mount(blocks, collapseActive)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()
    collapseActive.value = true
    await nextTick()

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    expect(host.textContent).toContain('Thinking · 5s')
    expect(host.querySelector('.activity-motion--active')).toBeNull()

    vi.advanceTimersByTime(4_000)
    await nextTick()
    expect(host.textContent).toContain('Thinking · 5s')
    vi.useRealTimers()
  })

  it('animates only the current streaming summary and never replays settled reasoning', async () => {
    const blocks = ref([
      block('r1', 0, 'earlier reasoning', 'completed'),
      block('r2', 1, 'live reasoning'),
    ])
    const host = mount(blocks)
    const folds = () => [...host.querySelectorAll<HTMLDetailsElement>('details')]

    expect(folds()[0]?.querySelector('.activity-motion--active')).toBeNull()
    expect(folds()[1]?.querySelector('.activity-motion--active')).not.toBeNull()
    folds()[1]?.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    blocks.value = blocks.value.map(current => ({ ...current, status: 'completed', endedAt: 2_000 }))
    await nextTick()

    expect(host.querySelector('.activity-motion--active')).toBeNull()
    expect(folds()[1]?.open).toBe(true)
    folds()[0]?.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(folds()[0]?.open).toBe(true)
    expect(host.querySelector('.activity-motion--active')).toBeNull()
  })

  it.each([undefined, false])('keeps a streaming snapshot static when execution is %s', async executing => {
    const host = mount(ref([block('r1', 0, 'restored or waiting reasoning')]), ref(false), { executing })
    expect(host.querySelector('.activity-motion--active')).toBeNull()
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    expect(host.querySelector('.activity-motion--active')).toBeNull()
  })

  it('renders a coarse arrived burst immediately even when the legacy pacing prop is set', async () => {
    const frame = vi.fn()
    vi.stubGlobal('requestAnimationFrame', frame)
    const fullText = 'reasoning '.repeat(80)
    const blocks = ref([block('r1', 0, 'reasoning ')])
    const host = mount(blocks)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    blocks.value = [block('r1', 0, fullText)]
    await nextTick()

    const body = () => host.querySelector<HTMLElement>('.thinking-fold__body')?.textContent || ''
    expect(body()).toBe(fullText)
    expect(blocks.value[0]?.text).toBe(fullText)
    expect(frame).not.toHaveBeenCalled()
  })

  it('flushes a completed burst immediately and reports presentation complete', async () => {
    const callbacks: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callbacks.push(callback)
      return callbacks.length
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const fullText = 'completed reasoning '.repeat(80)
    const blocks = ref([block('r1', 0, 'start')])
    const host = mount(blocks)

    blocks.value = [block('r1', 0, fullText, 'completed')]
    await nextTick()

    const fold = () => host.querySelector<HTMLDetailsElement>('details')
    const body = () => host.querySelector<HTMLElement>('.thinking-fold__body')?.textContent || ''
    expect(fold()?.open).toBe(false)
    expect(body()).toBe(fullText)
    expect(callbacks).toHaveLength(0)
    expect(fold()?.open).toBe(false)
  })

  it('shows an initially completed snapshot immediately without opening it', async () => {
    const callbacks: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callbacks.push(callback)
      return callbacks.length
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const fullText = 'snapshot reasoning '.repeat(80)
    const blocks = ref([block('r1', 0, fullText, 'completed')])
    const host = mount(blocks)
    await nextTick()

    const fold = () => host.querySelector<HTMLDetailsElement>('details')
    const body = () => host.querySelector<HTMLElement>('.thinking-fold__body')?.textContent || ''
    expect(fold()?.open).toBe(false)
    expect(callbacks).toHaveLength(0)
    expect(body()).toBe(fullText)
    expect(fold()?.open).toBe(false)
  })

  it('does not pace hidden text and uses the canonical last line for the compact preview', async () => {
    const frame = vi.fn()
    vi.stubGlobal('requestAnimationFrame', frame)
    const fullText = 'large burst '.repeat(80) + '\nlatest original line\n'
    const blocks = ref([block('r1', 0, fullText)])
    const host = mount(blocks)
    await nextTick()
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe(fullText)
    expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('latest original line')
    expect(frame).not.toHaveBeenCalled()
  })

  it.each(['close', 'complete'] as const)('keeps the full arrived content on %s', async action => {
    const frame = vi.fn()
    vi.stubGlobal('requestAnimationFrame', frame)
    const blocks = ref([block('r1', 0, 'start')])
    const host = mount(blocks)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    const fullText = 'start\n' + 'arrived reasoning '.repeat(80) + '\ncanonical last line'
    blocks.value = [block('r1', 0, fullText)]
    await nextTick()
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe(fullText)
    expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('canonical last line')
    if (action === 'close') host.querySelector<HTMLElement>('summary')?.click()
    else blocks.value = [block('r1', 0, fullText, 'completed')]
    await nextTick()
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe(fullText)
    expect(frame).not.toHaveBeenCalled()
  })

  it('shows same-length canonical corrections immediately', async () => {
    const blocks = ref([block('r1', 0, 'first')])
    const host = mount(blocks)
    blocks.value = [block('r1', 0, 'other')]
    await nextTick()
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe('other')
    expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('other')
  })

  it('preserves manual open and close through live-to-history remounts', async () => {
    const live = ref([block('r1', 0, 'reasoning')])
    const first = mount(live, ref(false), { sessionKey: 'session-a' })
    first.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    mountedApps.pop()?.unmount()
    const settled = ref([block('r1', 0, 'reasoning', 'completed')])
    const second = mount(settled, ref(false), { sessionKey: 'session-a' })
    expect(second.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    second.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    mountedApps.pop()?.unmount()
    const third = mount(settled, ref(false), { sessionKey: 'session-a' })
    expect(third.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
  })

  it('isolates reused block ids by start and session and obeys existing session cleanup', async () => {
    const original = block('r1', 0, 'first')
    const first = mount(ref([original]), ref(false), { sessionKey: 'session-a' })
    first.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    mountedApps.pop()?.unmount()
    const nextTurn = mount(ref([{ ...original, startedAt: 3_000 }]), ref(false), { sessionKey: 'session-a' })
    expect(nextTurn.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
    const nextSession = mount(ref([original]), ref(false), { sessionKey: 'session-b' })
    expect(nextSession.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
    clearAssistantActivityExpansionState()
    const cleared = mount(ref([original]), ref(false), { sessionKey: 'session-a' })
    expect(cleared.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
  })

  it('shows the full burst immediately when reduced motion is requested', async () => {
    vi.stubGlobal('requestAnimationFrame', vi.fn())
    vi.stubGlobal('matchMedia', vi.fn().mockReturnValue({ matches: true }))
    const fullText = 'reasoning '.repeat(80)
    const blocks = ref([block('r1', 0, fullText)])
    const host = mount(blocks)
    await nextTick()

    expect(host.querySelector<HTMLElement>('.thinking-fold__body')?.textContent).toBe(fullText)
  })

  it('keeps an opened live trace pinned to its newest content', async () => {
    const blocks = ref([block('r1', 0, 'first')])
    const host = mount(blocks)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    const body = host.querySelector<HTMLElement>('.thinking-fold__body')!
    let scrollHeight = 500
    Object.defineProperty(body, 'scrollHeight', { configurable: true, get: () => scrollHeight })
    Object.defineProperty(body, 'clientHeight', { configurable: true, get: () => 100 })

    blocks.value = [block('r1', 0, 'first\nsecond')]
    await nextTick()
    await nextTick()

    expect(body.scrollTop).toBe(500)

    scrollHeight = 700
    blocks.value = [block('r1', 0, 'first\nsecond\nthird')]
    await nextTick()
    await nextTick()
    expect(body.scrollTop).toBe(700)
  })

  it('pauses tail following after user scroll-up and resumes at the bottom', async () => {
    const blocks = ref([block('r1', 0, 'first')])
    const host = mount(blocks)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    await nextTick()
    const body = host.querySelector<HTMLElement>('.thinking-fold__body')!
    let scrollHeight = 500
    Object.defineProperty(body, 'scrollHeight', { configurable: true, get: () => scrollHeight })
    Object.defineProperty(body, 'clientHeight', { configurable: true, get: () => 100 })

    body.scrollTop = 120
    body.dispatchEvent(new Event('scroll'))
    await nextTick()
    scrollHeight = 700
    blocks.value = [block('r1', 0, 'first\nsecond')]
    await nextTick()
    await nextTick()
    expect(body.scrollTop).toBe(120)

    body.scrollTop = 600
    body.dispatchEvent(new Event('scroll'))
    await nextTick()
    scrollHeight = 900
    blocks.value = [block('r1', 0, 'first\nsecond\nthird')]
    await nextTick()
    await nextTick()
    expect(body.scrollTop).toBe(900)
  })
})
