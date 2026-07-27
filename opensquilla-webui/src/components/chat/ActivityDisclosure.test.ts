// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import ActivityDisclosure from './ActivityDisclosure.vue'
import activityDisclosureSource from './ActivityDisclosure.vue?raw'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        activityWorking: 'Working',
        activityItems: 'Activity · {count}',
        activityCompletedItems: 'Completed · {count}',
        activityFailures: '{count} failed',
        activityFailuresRecovered: '{count} failure recovered',
        workedForSeconds: 'Worked for {seconds}s',
        workedForMinutes: 'Worked for {minutes}m {seconds}s',
        activity: {
          liveStep: 'step {n}',
        },
      },
    },
  },
})

type DisclosureProps = InstanceType<typeof ActivityDisclosure>['$props']

function mountDisclosure(props: DisclosureProps) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ActivityDisclosure, props, { default: () => 'Activity details' }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

function cssRule(selector: string) {
  const start = activityDisclosureSource.indexOf(`${selector} {`)
  expect(start).toBeGreaterThanOrEqual(0)
  const end = activityDisclosureSource.indexOf('}', start)
  return activityDisclosureSource.slice(start, end)
}

beforeEach(() => {
  clearAssistantActivityExpansionState()
  document.body.innerHTML = ''
})

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ActivityDisclosure lifecycle transitions', () => {
  it('uses AA text tokens and no text shimmer for the live header', () => {
    const rule = cssRule('.assistant-activity__summary')
    expect(rule).toContain('color: var(--text-muted);')

    const elapsedRule = cssRule('.assistant-activity__live-elapsed')
    expect(elapsedRule).toContain('color: var(--text-muted);')

    // The pulsing dot is the single "working" signal: the shimmer treatment
    // (gradient text + keyframes + its reduced-motion undo) must stay gone.
    expect(activityDisclosureSource).not.toContain('assistant-activity-shimmer')
    expect(activityDisclosureSource).not.toContain('.assistant-activity__live-label.is-active')
    expect(activityDisclosureSource).not.toContain('background-clip: text')
    expect(activityDisclosureSource).toContain('assistant-activity-pulse')
  })

  it.each(['failed', 'interrupted'] as const)(
    'opens a mounted disclosure when its lifecycle becomes %s',
    async lifecycle => {
      const state = reactive({
        lifecycle: 'settled' as 'settled' | 'failed' | 'interrupted',
        defaultOpen: false,
      })
      const host = document.createElement('div')
      document.body.appendChild(host)
      const app = createApp({
        render: () => h(ActivityDisclosure, {
          lifecycle: state.lifecycle,
          defaultOpen: state.defaultOpen,
          stepCount: 1,
          failureCount: 0,
          stateKey: `message-${lifecycle}`,
          continuityKey: `turn-${lifecycle}`,
        }, { default: () => 'Activity details' }),
      })
      mountedApps.push(app)
      app.use(i18n)
      app.mount(host)
      await nextTick()

      const summary = host.querySelector<HTMLButtonElement>(
        '.assistant-activity__summary',
      )
      expect(summary?.getAttribute('aria-expanded')).toBe('false')

      state.lifecycle = lifecycle
      state.defaultOpen = true
      await nextTick()

      expect(summary?.getAttribute('aria-expanded')).toBe('true')
    },
  )
})

describe('ActivityDisclosure resting affordance', () => {
  it('keeps the disclosure arrow visible at rest with no transform offset', () => {
    const arrowRule = cssRule('.assistant-activity__summary-arrow')
    expect(arrowRule).toContain('opacity: 0.34;')
    expect(arrowRule).not.toContain('opacity: 0;')
    expect(arrowRule).not.toContain('translateX(-')
  })

  it('keeps the house focus ring on the summary button', () => {
    const focusRule = cssRule('.assistant-activity__summary:focus-visible')
    expect(focusRule).toContain('box-shadow: var(--focus-ring);')
    expect(focusRule).not.toContain('box-shadow: none')
  })

  it('raises the resting arrow opacity on hoverless devices', () => {
    const mediaStart = activityDisclosureSource.indexOf('@media (hover: none)')
    expect(mediaStart).toBeGreaterThanOrEqual(0)
    const ruleEnd = activityDisclosureSource.indexOf('}', mediaStart)
    const mediaRule = activityDisclosureSource.slice(mediaStart, ruleEnd)
    expect(mediaRule).toContain('.assistant-activity__summary-arrow')
    expect(mediaRule).toContain('opacity: 0.55;')
  })
})

describe('ActivityDisclosure summary label', () => {
  it('prefers a supplied summaryLabel and composes it with the failure chip', async () => {
    const host = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 9,
      failureCount: 2,
      durationSeconds: 12,
      summaryLabel: 'Searched the web, edited 2 files',
    })
    await nextTick()

    expect(host.querySelector('.assistant-activity__label')?.textContent?.trim())
      .toBe('Searched the web, edited 2 files')
    expect(host.querySelector('.assistant-activity__failure')?.textContent?.trim())
      .toBe('2 failed')
  })

  it('separates the label and failure chip with real text, not CSS content', async () => {
    const host = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 3,
      failureCount: 1,
      durationSeconds: 12,
      completionConfirmed: true,
    })
    await nextTick()

    // The button's textContent is reused verbatim as the share-export label
    // and the accessible name, so the separator must live in the DOM.
    const button = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    expect(button?.textContent?.replace(/\s+/g, ' ').trim())
      .toBe('Worked for 12s · 1 failure recovered')
    expect(activityDisclosureSource).not.toContain('.assistant-activity__failure::before')
    expect(activityDisclosureSource).not.toContain('.assistant-activity__live-failure::before')
  })

  it('keeps the duration/count fallback chain when summaryLabel is empty', async () => {
    const withDuration = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 9,
      failureCount: 0,
      durationSeconds: 12,
    })
    const withoutDuration = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 9,
      failureCount: 0,
      durationSeconds: 0,
    })
    await nextTick()

    expect(withDuration.querySelector('.assistant-activity__label')?.textContent?.trim())
      .toBe('Worked for 12s')
    expect(withoutDuration.querySelector('.assistant-activity__label')?.textContent?.trim())
      .toBe('Activity · 9')
  })

  it('wraps the label text instead of truncating it', () => {
    const labelRule = cssRule('.assistant-activity__label')
    expect(labelRule).toContain('overflow-wrap: anywhere;')
  })
})

describe('ActivityDisclosure live header', () => {
  it('renders step and failure counts during a live turn', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 4,
      failureCount: 3,
      elapsedLabel: '12s',
    })
    await nextTick()

    const step = host.querySelector('.assistant-activity__live-step')
    const failure = host.querySelector('.assistant-activity__live-failure')
    const elapsed = host.querySelector('.assistant-activity__live-elapsed')

    expect(step?.textContent?.trim()).toBe('step 4')
    expect(failure?.textContent?.trim()).toBe('3 failed')

    // The failure count is real information — it must stay readable, while
    // the per-second elapsed label stays hidden from screen readers.
    expect(failure?.getAttribute('aria-hidden')).toBeNull()
    expect(step?.getAttribute('aria-hidden')).toBeNull()
    expect(elapsed?.getAttribute('aria-hidden')).toBe('true')

    const failureRule = cssRule('.assistant-activity__live-failure')
    expect(failureRule).toContain('color: var(--danger);')
    expect(failureRule).toContain('flex: 0 0 auto;')
    expect(failureRule).toContain('font-size: 0.75rem;')
  })

  it('shows no step or failure text when their counts are zero', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
      elapsedLabel: '2s',
    })
    await nextTick()

    expect(host.querySelector('.assistant-activity__live-step')).toBeNull()
    expect(host.querySelector('.assistant-activity__sep')).toBeNull()
    // The failure region stays mounted (it is a live region) but must stay
    // visually and textually empty at zero.
    const failure = host.querySelector('.assistant-activity__live-failure')
    expect(failure).not.toBeNull()
    expect(failure?.textContent?.trim()).toBe('')
  })

  it('announces failure count changes through an always-mounted polite region', async () => {
    const state = reactive({ failureCount: 0 })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: 'working',
        stepCount: 2,
        failureCount: state.failureCount,
      }, { default: () => 'Activity details' }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const region = host.querySelector('.assistant-activity__live-failure')
    expect(region?.getAttribute('role')).toBe('status')
    expect(region?.getAttribute('aria-live')).toBe('polite')
    expect(region?.getAttribute('aria-atomic')).toBe('true')
    expect(region?.textContent?.trim()).toBe('')

    state.failureCount = 2
    await nextTick()

    // The same element must gain the text — a live region only announces
    // content changes inside a node that was already in the tree.
    expect(host.querySelector('.assistant-activity__live-failure')).toBe(region)
    expect(region?.textContent?.trim()).toBe('2 failed')
  })
})

describe('ActivityDisclosure stale state', () => {
  it('swaps the dot and label colour tokens when stale', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
      stale: true,
    })
    await nextTick()

    const dot = host.querySelector('.assistant-activity__live-dot')
    const label = host.querySelector('.assistant-activity__live-label')

    // The stale copy itself is owned upstream (the stream module passes it in
    // as phaseLabel); this component only carries the visual half.
    expect(label?.textContent?.trim()).toBe('Working')
    expect(dot?.classList.contains('is-active')).toBe(false)
    expect(dot?.classList.contains('is-stale')).toBe(true)
    expect(label?.classList.contains('is-stale')).toBe(true)

    // Colour, not motion, must carry the state.
    expect(cssRule('.assistant-activity__live-dot.is-stale'))
      .toContain('background: var(--warn-fill);')
    expect(cssRule('.assistant-activity__live-label.is-stale'))
      .toContain('color: var(--warn);')
  })

  it('keeps a supplied phaseLabel even when stale', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
      stale: true,
      phaseLabel: 'Running commands',
    })
    await nextTick()

    expect(host.querySelector('.assistant-activity__live-label')?.textContent?.trim())
      .toBe('Running commands')
  })

  it('shows the working copy with the pulsing dot when live and not stale', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
    })
    await nextTick()

    const dot = host.querySelector('.assistant-activity__live-dot')
    expect(host.querySelector('.assistant-activity__live-label')?.textContent?.trim())
      .toBe('Working')
    expect(dot?.classList.contains('is-active')).toBe(true)
    expect(dot?.classList.contains('is-stale')).toBe(false)
  })
})

describe('ActivityDisclosure expanded boundary', () => {
  it('contains the fold body with a 1px left rule and closes it with a separator', () => {
    const bodyRule = cssRule('.assistant-activity__body')
    expect(bodyRule).toContain('border-left: 1px solid var(--border);')
    expect(bodyRule).toContain('padding: 0 0 0 0.75rem;')

    const separatorRule = cssRule(
      '.assistant-activity--settled[data-share-expanded="true"]::after',
    )
    expect(separatorRule).toContain('height: 1px;')
    expect(separatorRule).toContain('background: var(--border);')
  })
})

describe('ActivityDisclosure aria wiring', () => {
  it('links the summary button to the fold body via aria-controls', async () => {
    const host = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 3,
      failureCount: 0,
      defaultOpen: true,
    })
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    const controls = summary?.getAttribute('aria-controls')
    expect(controls).toBeTruthy()

    const body = host.querySelector<HTMLElement>(`[id="${controls}"]`)
    expect(body).not.toBeNull()
    expect(body?.classList.contains('assistant-activity__body')).toBe(true)
    expect(body?.dataset.shareActivityBody).toBeDefined()
  })
})
