// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ReasoningPart from './ReasoningPart.vue'
import reasoningPartSource from './ReasoningPart.vue?raw'
import type { ChatPart } from '@/types/parts'
import { provideActivityDisclosureVisibility } from '@/composables/chat/useActivityDisclosureVisibility'

type ReasoningChatPart = Extract<ChatPart, { type: 'reasoning' }>

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        thinking: 'Thinking',
        thinkingForSeconds: 'Thinking · {seconds}s',
        thoughtProcess: 'Thinking',
        thoughtForSeconds: 'Thought for {seconds}s',
        thoughtForMinutes: 'Thought for {minutes}m {seconds}s',
        activityDurationSeconds: '{seconds}s',
        activityDurationMinutes: '{minutes}m {seconds}s',
        activity: { provider: { reasoning: 'Thinking' } },
      },
    },
  },
})

function part(text: string, seconds: number): ReasoningChatPart {
  return { type: 'reasoning', key: `reasoning:${seconds}`, text, seconds }
}

function mount(props: {
  part: ReasoningChatPart
  embedded?: boolean
  live?: boolean
  motionActive?: boolean
  previewText?: string
  nested?: boolean
  controlled?: boolean
  open?: boolean
  timelinePhase?: boolean
}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ReasoningPart, props) })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.useRealTimers()
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('ReasoningPart embedded body height cap', () => {
  it('caps the embedded body like the fold body so long traces scroll', () => {
    const selectorStart = reasoningPartSource.indexOf('.thinking-block__body {')
    const blockEnd = reasoningPartSource.indexOf('}', selectorStart)
    const rule = reasoningPartSource.slice(selectorStart, blockEnd)

    expect(selectorStart).toBeGreaterThanOrEqual(0)
    expect(rule).toContain('max-height: 16rem;')
    expect(rule).toContain('overflow-y: auto;')
    // Keep the embedded block flat — no card chrome around the trace.
    expect(rule).not.toContain('border')
    expect(rule).not.toContain('background')
  })
})

describe('ReasoningPart summary wording', () => {
  it('keeps the nested live label fixed and restores phase duration on completion', async () => {
    const props = reactive({ part: part('trace', 4), live: true, nested: true, timelinePhase: true })
    const host = mount(props)
    expect(host.querySelector('.thinking-fold__label')?.textContent).toBe('Thinking')
    props.part.seconds = 35
    await nextTick()
    expect(host.querySelector('.thinking-fold__label')?.textContent).toBe('Thinking')
    props.live = false
    await nextTick()
    expect(host.querySelector('.thinking-fold__label')?.textContent).toBe('Thinking · 35s')
  })
  it('renders the settled wording for sub-second, seconds, and minutes', () => {
    expect(mount({ part: part('t', 0), embedded: true }).textContent)
      .toContain('Thinking')
    expect(mount({ part: part('t', 4), embedded: true }).textContent)
      .toContain('Thought for 4s')
    expect(mount({ part: part('t', 75), embedded: true }).textContent)
      .toContain('Thought for 1m 15s')
  })

  it('renders the live in-progress wording through the localized template', () => {
    const host = mount({ part: part('streaming trace', 4), live: true })
    const summary = host.querySelector('.thinking-fold__summary')
    expect(summary?.textContent).toContain('Thinking · 4s')
    expect(summary?.textContent).not.toContain('Thought for')
    // A concatenation fallback would surface the missing-key name instead.
    expect(summary?.textContent).not.toContain('chat.thinking')
  })
})

describe('ReasoningPart compact preview stability', () => {
  it('grows the same line immediately and replaces fast new lines with only the latest after 500ms', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(10_000)
    const props = reactive({ part: part('first', 1), live: true, controlled: true, open: false })
    const host = mount(props)
    const preview = () => host.querySelector('.thinking-fold__preview')?.textContent
    const body = () => host.querySelector('.thinking-fold__body')?.textContent
    props.part.text = 'first growing'
    await nextTick()
    expect(preview()).toBe('first growing')
    await vi.advanceTimersByTimeAsync(100)
    props.part.text += '\nsecond'
    await nextTick()
    expect(preview()).toBe('first growing')
    expect(body()).toBe('first growing\nsecond')
    await vi.advanceTimersByTimeAsync(100)
    props.part.text += '\nthird growing'
    await nextTick()
    expect(body()).toBe(props.part.text)
    await vi.advanceTimersByTimeAsync(299)
    expect(preview()).toBe('first growing')
    await vi.advanceTimersByTimeAsync(1)
    expect(preview()).toBe('third growing')
    expect(vi.getTimerCount()).toBe(0)
    props.part.text += ' in place'
    await nextTick()
    expect(preview()).toBe('third growing in place')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('opens the full canonical text immediately and clears a pending preview update', async () => {
    vi.useFakeTimers()
    const props = reactive({ part: part('first', 1), live: true, controlled: true, open: false })
    const host = mount(props)
    props.part.text += '\nlatest'
    await nextTick()
    expect(vi.getTimerCount()).toBe(1)
    props.open = true
    await nextTick()
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe('first\nlatest')
    expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('latest')
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each(['complete', 'stop-motion', 'replace-block', 'unmount'] as const)('clears pending line updates on %s', async action => {
    vi.useFakeTimers()
    const props = reactive({ part: part('first', 1), live: true, motionActive: true, controlled: true, open: false })
    const host = mount(props)
    props.part.text += '\nold pending'
    await nextTick()
    expect(vi.getTimerCount()).toBe(1)
    if (action === 'complete') props.live = false
    else if (action === 'stop-motion') props.motionActive = false
    else if (action === 'replace-block') props.part = { ...part('new block', 1), key: 'reasoning:new' }
    else mountedApps.pop()?.unmount()
    await nextTick()
    expect(vi.getTimerCount()).toBe(0)
    await vi.advanceTimersByTimeAsync(1_000)
    if (action === 'replace-block') expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('new block')
    if (action === 'complete') expect(host.querySelector('.thinking-fold__preview')).toBeNull()
  })
})

describe('ReasoningPart nested-in-activity variant', () => {
  it('marks the live fold as in-activity so the doubled left rule is dropped', () => {
    const host = mount({ part: part('t', 4), live: true })
    expect(host.querySelector('details.thinking-fold--in-activity')).not.toBeNull()
  })

  it('marks a settled nested fold without using live wording', () => {
    const host = mount({ part: part('t', 4), nested: true })
    expect(host.querySelector('details.thinking-fold--in-activity')).not.toBeNull()
    expect(host.querySelector('.thinking-fold__summary')?.textContent)
      .toContain('Thought for 4s')
  })

  it.each([[0, 'Thinking'], [2, 'Thinking · 2s'], [75, 'Thinking · 1m 15s']])('keeps the same reasoning label at %s seconds', (seconds, label) => {
    const host = mount({ part: part('t', Number(seconds)), nested: true, timelinePhase: true })
    expect(host.querySelector('.thinking-fold__summary')?.textContent)
      .toBe(label)
    expect(host.querySelector('.thinking-fold--timeline-phase')).not.toBeNull()
  })

  it('keeps the standalone compat fold free of the in-activity modifier', () => {
    const host = mount({ part: part('t', 4) })
    expect(host.querySelector('details.thinking-fold')).not.toBeNull()
    expect(host.querySelector('.thinking-fold--in-activity')).toBeNull()
  })

  it('keeps standalone and nested reasoning flat and aligned', () => {
    const baseStart = reasoningPartSource.indexOf('.thinking-fold__body {')
    const baseRule = reasoningPartSource.slice(baseStart, reasoningPartSource.indexOf('}', baseStart))
    expect(baseRule).not.toContain('border-left:')
    expect(baseRule).not.toContain('background:')
    expect(baseRule).toContain('padding: 0.125rem 0;')
  })
})

describe('ReasoningPart branches', () => {
  it.each([false, true])('stops hidden reasoning feedback without changing its content (embedded=%s)', async embedded => {
    const visible = ref(true)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup() {
        provideActivityDisclosureVisibility(visible)
        return () => h(ReasoningPart, {
          part: part('arrived reasoning', 4),
          live: true,
          embedded,
        })
      },
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()

    visible.value = false
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).toBeNull()
    expect(host.textContent).toContain('arrived reasoning')

    visible.value = true
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()
  })

  it('applies feedback to the live summary while keeping the preview and body outside it', () => {
    const host = mount({ part: part('arrived reasoning', 4), live: true })
    const motion = host.querySelector('.activity-motion--active')
    expect(motion).not.toBeNull()
    expect(motion?.querySelector('.thinking-fold__label')).not.toBeNull()
    expect(motion?.querySelector('.thinking-fold__chevron')).not.toBeNull()
    expect(motion?.querySelector('.thinking-fold__preview')).toBeNull()
    expect(motion?.querySelector('.thinking-fold__body')).toBeNull()

    const history = mount({ part: part('completed reasoning', 4) })
    expect(history.querySelector('.activity-motion--active')).toBeNull()
    const frozen = mount({ part: part('arrived reasoning', 4), live: true, motionActive: false })
    expect(frozen.querySelector('.activity-motion--active')).toBeNull()
  })

  it('uses the same live feedback for an embedded summary without animating its body', () => {
    const host = mount({ part: part('embedded reasoning', 4), embedded: true, live: true })
    expect(host.querySelector('.thinking-block__header .activity-motion--active')).not.toBeNull()
    expect(host.querySelector('.activity-motion--active .thinking-block__body')).toBeNull()
  })

  it('uses the original last nonempty line for the live compact preview only', () => {
    const host = mount({ part: part('first\nlatest detail\n', 4), live: true, controlled: true, open: false })
    expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('latest detail')
    expect(host.querySelector('.thinking-fold__label')?.nextElementSibling?.classList.contains('thinking-fold__chevron')).toBe(true)
    expect(host.querySelector('.thinking-fold__motion')?.nextElementSibling?.classList.contains('thinking-fold__preview')).toBe(true)
    const history = mount({ part: part('completed detail', 4) })
    expect(history.querySelector('.thinking-fold__preview')).toBeNull()
  })
  it('keeps the canonical preview beside the title independently of the body', () => {
    const host = mount({ part: part('body text', 4), previewText: 'first\nlast arrived line', live: true })
    expect(host.querySelector('.thinking-fold__preview')?.textContent).toBe('last arrived line')
    expect(host.querySelector('.thinking-fold__body')?.textContent).toBe('body text')
    const start = reasoningPartSource.indexOf('.thinking-fold__preview {')
    const rule = reasoningPartSource.slice(start, reasoningPartSource.indexOf('}', start))
    expect(rule).toContain('flex: 1 1 auto;')
    expect(rule).toContain('min-width: 0;')
    expect(rule).not.toContain('100%')
    expect(reasoningPartSource).not.toContain('flex-wrap: wrap')
  })
  it('renders a flat block with the trace body when embedded', () => {
    const host = mount({ part: part('embedded trace', 4), embedded: true })
    expect(host.querySelector('details')).toBeNull()
    expect(host.querySelector('.thinking-block__body')?.textContent)
      .toBe('embedded trace')
  })

  it('renders a self-sufficient details fold when not embedded', () => {
    const host = mount({ part: part('fold trace', 4) })
    const fold = host.querySelector<HTMLDetailsElement>('details.thinking-fold')
    expect(fold).not.toBeNull()
    expect(fold?.open).toBe(false)
    expect(fold?.querySelector('.thinking-fold__chevron')).not.toBeNull()
    expect(fold?.querySelector('.thinking-fold__body')?.textContent)
      .toBe('fold trace')
  })

  it('lets a settled native fold reopen after the outer activity is expanded', async () => {
    const host = mount({ part: part('settled trace', 4), nested: true })
    const fold = host.querySelector<HTMLDetailsElement>('details.thinking-fold')!
    const summary = fold.querySelector<HTMLElement>('.thinking-fold__summary')!

    expect(fold.open).toBe(false)
    summary.click()
    await nextTick()

    expect(fold.open).toBe(true)
    expect(fold.querySelector('.thinking-fold__body')?.textContent)
      .toBe('settled trace')
  })

  it('keeps live timeline folds explicitly controlled', async () => {
    const host = mount({
      part: part('live trace', 4),
      live: true,
      controlled: true,
      open: false,
    })
    const fold = host.querySelector<HTMLDetailsElement>('details.thinking-fold')!
    const summary = fold.querySelector<HTMLElement>('.thinking-fold__summary')!

    summary.click()
    await nextTick()

    expect(fold.open).toBe(false)
  })
})
