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
  it('renders queued then running as explicit live lifecycle phases', async () => {
    const state = reactive({ phase: 'Queued' })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: 'working',
        defaultOpen: true,
        stepCount: 0,
        failureCount: 0,
        phaseLabel: state.phase,
        elapsedLabel: '0s',
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const label = host.querySelector('.assistant-activity__live-label')
    expect(label?.textContent).toBe('Queued')
    expect(host.textContent).not.toContain('Waiting for model')

    state.phase = 'Running'
    await nextTick()
    expect(label?.textContent).toBe('Running')
  })

  it('uses AA text tokens and no text shimmer for the live header', () => {
    const rule = cssRule('.assistant-activity__summary')
    expect(rule).toContain('color: var(--text-muted);')

    const elapsedRule = cssRule('.assistant-activity__live-elapsed')
    expect(elapsedRule).toContain('color: var(--text-muted);')
    expect(elapsedRule).toContain('font-size: inherit;')

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

  it('follows the live default open and folds when the turn settles', async () => {
    const state = reactive({ lifecycle: 'working' as 'working' | 'settled' })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: state.lifecycle,
        defaultOpen: state.lifecycle === 'working',
        stepCount: 1,
        failureCount: 0,
        stateKey: 'message-live-to-settled',
        continuityKey: 'turn-live-to-settled',
      }, { default: () => 'Activity details' }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.assistant-activity__live-head')?.getAttribute('aria-expanded'))
      .toBe('true')

    state.lifecycle = 'settled'
    await nextTick()

    expect(host.querySelector('.assistant-activity__summary')?.getAttribute('aria-expanded'))
      .toBe('false')
  })

  it('forces a terminal transition closed and allows reopening afterwards', async () => {
    const state = reactive({ lifecycle: 'working' as 'working' | 'settled' })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: state.lifecycle,
        defaultOpen: state.lifecycle === 'working',
        stepCount: 1,
        failureCount: 0,
        stateKey: 'message-manual-expansion',
        continuityKey: 'turn-manual-expansion',
      }, { default: () => 'Activity details' }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const liveSummary = host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')
    expect(liveSummary?.getAttribute('aria-expanded')).toBe('true')
    liveSummary?.click()
    await nextTick()
    liveSummary?.click()
    await nextTick()
    expect(liveSummary?.getAttribute('aria-expanded')).toBe('true')

    state.lifecycle = 'settled'
    await nextTick()

    const settledSummary = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    expect(settledSummary?.getAttribute('aria-expanded'))
      .toBe('false')
    settledSummary?.click()
    await nextTick()
    expect(settledSummary?.getAttribute('aria-expanded'))
      .toBe('true')
  })

  it('preserves a terminal manual reopen across same-turn canonical identity reconcile', async () => {
    const state = reactive({
      lifecycle: 'working' as 'working' | 'settled',
      stateKey: 'message-live',
      continuityKey: 'turn-stable',
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: state.lifecycle,
        defaultOpen: state.lifecycle === 'working',
        stepCount: 1,
        failureCount: 0,
        stateKey: state.stateKey,
        continuityKey: state.continuityKey,
      }, { default: () => 'Activity details' }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    state.lifecycle = 'settled'
    await nextTick()
    const summary = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    expect(summary?.getAttribute('aria-expanded')).toBe('false')

    state.stateKey = 'message-canonical'
    await nextTick()
    // Canonical history may replace the optimistic message identity before the
    // user reopens the finished turn.
    summary?.click()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    // A canonical-history reconciliation can replace the entire message
    // component with the earlier optimistic identity. That identity still has
    // its terminal auto-collapse persisted, so a new component must choose the
    // newer same-turn continuity write made by the user's explicit reopen.
    mountedApps.pop()
    app.unmount()
    const reconciled = reactive({
      stateKey: 'message-live',
      continuityKey: 'turn-stable',
    })
    const reconciledApp = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: 'settled',
        defaultOpen: false,
        stepCount: 1,
        failureCount: 0,
        stateKey: reconciled.stateKey,
        continuityKey: reconciled.continuityKey,
      }, { default: () => 'Activity details' }),
    })
    mountedApps.push(reconciledApp)
    reconciledApp.use(i18n)
    reconciledApp.mount(host)
    await nextTick()
    const reconciledSummary = host.querySelector<HTMLButtonElement>(
      '.assistant-activity__summary',
    )
    expect(reconciledSummary?.getAttribute('aria-expanded')).toBe('true')

    reconciled.stateKey = 'message-next-turn'
    reconciled.continuityKey = 'turn-next'
    await nextTick()
    expect(reconciledSummary?.getAttribute('aria-expanded')).toBe('false')
  })
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
  it('makes the completed outcome larger than its supporting phase rows', () => {
    expect(cssRule('.assistant-activity--settled > .assistant-activity__summary'))
      .toContain('font-size: 0.875rem;')
    expect(activityDisclosureSource).toContain(
      '.assistant-activity--settled :deep(.assistant-activity-status__row)',
    )
    expect(activityDisclosureSource).toContain('font-size: 0.75rem;')
  })

  it('prefers a supplied summaryLabel without surfacing failure metadata', async () => {
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
    expect(host.querySelector('.assistant-activity__failure')).toBeNull()
    expect(host.textContent).not.toContain('2 failed')
  })

  it('keeps recovered failures out of both summary and detail', async () => {
    const host = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 3,
      failureCount: 1,
      durationSeconds: 12,
      completionConfirmed: true,
    })
    await nextTick()

    // The button's textContent is reused verbatim as the share-export label
    // and the accessible name, so recovered failures move into the detail row.
    const button = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    expect(button?.textContent?.replace(/\s+/g, ' ').trim())
      .toBe('Worked for 12s')
    expect(host.querySelector('.assistant-activity__detail')).toBeNull()
    expect(host.textContent).not.toContain('failure')
    expect(host.textContent).not.toContain('failed')
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
  it('still allows a live disclosure to be collapsed and expanded manually', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 2,
      failureCount: 0,
    })
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')
    const body = host.querySelector<HTMLElement>('.assistant-activity__body')
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(body?.getAttribute('aria-hidden')).toBe('true')
    expect(host.querySelector('.assistant-activity')?.getAttribute('data-share-expanded')).toBe('false')

    summary?.click()
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(body?.getAttribute('aria-hidden')).toBe('false')
    expect(host.querySelector('.assistant-activity')?.getAttribute('data-share-expanded')).toBe('true')
  })

  it('renders live step count without surfacing failures', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 4,
      failureCount: 3,
      elapsedLabel: '12s',
    })
    await nextTick()

    const step = host.querySelector('.assistant-activity__live-step')
    const elapsed = host.querySelector('.assistant-activity__live-elapsed')

    expect(step?.textContent?.trim()).toBe('step 4')
    expect(step?.getAttribute('aria-hidden')).toBeNull()
    expect(elapsed?.getAttribute('aria-hidden')).toBe('true')
    expect(host.querySelector('.assistant-activity__live-failure')).toBeNull()
    expect(host.textContent).not.toContain('3 failed')
  })

  it('shows no step or failure text when the step count is zero', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
      elapsedLabel: '2s',
    })
    await nextTick()

    expect(host.querySelector('.assistant-activity__live-step')).toBeNull()
    expect(host.querySelector('.assistant-activity__sep')).toBeNull()
    expect(host.querySelector('.assistant-activity__live-failure')).toBeNull()
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
  it('reveals its height progressively without drawing a frame', () => {
    const bodyRule = cssRule('.assistant-activity__body')
    expect(bodyRule).toContain('grid-template-rows: 0fr;')
    expect(bodyRule).toContain('grid-template-rows var(--dur-base)')
    expect(bodyRule).toContain('opacity: 0;')

    const openRule = cssRule(
      '.assistant-activity[data-share-expanded="true"] > .assistant-activity__body',
    )
    expect(openRule).toContain('grid-template-rows: 1fr;')
    expect(openRule).toContain('opacity: 1;')

    const innerRule = cssRule('.assistant-activity__body-inner')
    expect(innerRule).toContain('overflow: hidden;')
    expect(innerRule).not.toContain('border-left:')
    expect(innerRule).toContain('padding: 0 0 0 0.75rem;')
    expect(activityDisclosureSource).toContain('assistant-activity-item-enter')
    expect(activityDisclosureSource)
      .not.toContain('.assistant-activity--settled[data-share-expanded="true"]::after')
  })
})

describe('ActivityDisclosure failure visibility', () => {
  it('contains no failure label or failure-specific styling', () => {
    expect(activityDisclosureSource).not.toContain('resolvedFailureLabel')
    expect(activityDisclosureSource).not.toContain('showDetailFailure')
    expect(activityDisclosureSource).not.toContain('showSummaryFailure')
    expect(activityDisclosureSource).not.toContain('assistant-activity__failure')
    expect(activityDisclosureSource).not.toContain('assistant-activity__live-failure')
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
