// @vitest-environment happy-dom
import { createApp, h, nextTick, ref, type Ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ReasoningTimeline from './ReasoningTimeline.vue'
import type { ReasoningBlock } from '@/types/turnlog'

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        thinkingForSeconds: 'Thinking · {seconds}s',
        thoughtProcess: 'Thought process',
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

function mount(blocks: Ref<ReasoningBlock[]>, collapseActive: Ref<boolean> = ref(false)) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ReasoningTimeline, {
    blocks: blocks.value,
    collapseActive: collapseActive.value,
    paceBursts: true,
  }) })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
})

describe('ReasoningTimeline disclosure ownership', () => {
  it('opens the active block and keeps a user-close stable across deltas', async () => {
    const blocks = ref([block('r1', 0, 'plan')])
    const host = mount(blocks)

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    host.querySelector<HTMLElement>('summary')?.click()
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)

    blocks.value = [block('r1', 0, 'plan more')]
    await nextTick()
    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
  })

  it('collapses the previous block when a new block starts', async () => {
    const blocks = ref([block('r1', 0, 'plan')])
    const host = mount(blocks)

    blocks.value = [
      block('r1', 0, 'plan', 'completed'),
      block('r2', 1, 'review'),
    ]
    await nextTick()

    const folds = host.querySelectorAll<HTMLDetailsElement>('details')
    expect(folds).toHaveLength(2)
    expect(folds[0]?.open).toBe(false)
    expect(folds[1]?.open).toBe(true)
  })

  it('collapses the current block when it completes', async () => {
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

  it('retains but collapses the current trace when answer organization begins', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(6_000)
    const blocks = ref([block('r1', 0, 'plan')])
    const collapseActive = ref(false)
    const host = mount(blocks, collapseActive)

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(true)
    collapseActive.value = true
    await nextTick()

    expect(host.querySelector<HTMLDetailsElement>('details')?.open).toBe(false)
    expect(host.textContent).toContain('Thinking · 5s')

    vi.advanceTimersByTime(4_000)
    await nextTick()
    expect(host.textContent).toContain('Thinking · 5s')
    vi.useRealTimers()
  })

  it('progressively reveals a coarse reasoning burst without changing canonical text', async () => {
    const callbacks: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callbacks.push(callback)
      return callbacks.length
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const fullText = 'reasoning '.repeat(80)
    const blocks = ref([block('r1', 0, fullText)])
    const host = mount(blocks)
    await nextTick()

    const body = () => host.querySelector<HTMLElement>('.thinking-fold__body')?.textContent || ''
    expect(body().length).toBeGreaterThan(0)
    expect(body().length).toBeLessThan(fullText.length)
    expect(blocks.value[0]?.text).toBe(fullText)

    let previousLength = body().length
    while (callbacks.length) {
      callbacks.shift()?.(performance.now())
      await nextTick()
      expect(body().length).toBeGreaterThanOrEqual(previousLength)
      previousLength = body().length
    }
    expect(body()).toBe(fullText)
  })

  it('does not flush a coarse burst when delta and completion share one render batch', async () => {
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
    expect(fold()?.open).toBe(true)
    expect(body().length).toBeGreaterThan(0)
    expect(body().length).toBeLessThan(fullText.length)

    while (callbacks.length) {
      callbacks.shift()?.(performance.now())
      await nextTick()
    }
    expect(body()).toBe(fullText)
    expect(fold()?.open).toBe(false)
  })

  it('paces an initially completed live snapshot before folding it', async () => {
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
    expect(fold()?.open).toBe(true)
    expect(body().length).toBeLessThan(fullText.length)

    while (callbacks.length) {
      callbacks.shift()?.(performance.now())
      await nextTick()
    }
    expect(body()).toBe(fullText)
    expect(fold()?.open).toBe(false)
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

  it('keeps an untouched live trace pinned to its newest content', async () => {
    const blocks = ref([block('r1', 0, 'first')])
    const host = mount(blocks)
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
