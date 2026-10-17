// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import ActivityDisclosure from './ActivityDisclosure.vue'
import activityDisclosureSource from './ActivityDisclosure.vue?raw'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import enMessages from '@/locales/en.json'
import zhMessages from '@/locales/zh-Hans.json'

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
        activityElapsed: 'Elapsed {duration}',
        activityDurationSeconds: '{seconds}s',
        activityDurationMinutes: '{minutes}m {seconds}s',
        status: { queued: 'Queued' },
        workedForSeconds: 'Worked for {seconds}s',
        workedForMinutes: 'Worked for {minutes}m {seconds}s',
        activity: {
          stale: 'Still working — no recent signal',
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
  it('shows queue admission without inventing an elapsed clock, then shows execution time', async () => {
    const state = reactive({ phase: 'Queued', queued: true, elapsed: '' })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: 'working',
        defaultOpen: true,
        stepCount: 0,
        failureCount: 0,
        phaseLabel: state.phase,
        queued: state.queued,
        elapsedLabel: state.elapsed,
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const button = host.querySelector('.assistant-activity__live-head')
    const announcement = host.querySelector('[role="status"]')
    expect(button?.textContent?.trim()).toBe('Queued')
    expect(host.textContent).not.toContain('0s')
    expect(announcement?.textContent?.trim()).toBe('Queued')
    expect(announcement?.closest('button')).toBeNull()
    expect(host.textContent).not.toContain('Waiting for model')

    state.phase = 'Running'
    state.queued = false
    state.elapsed = '1s'
    await nextTick()
    expect(button?.textContent?.trim()).toBe('Elapsed 1s')
    expect(announcement?.textContent?.trim()).toBe('Running')
  })

  it('keeps readable text tokens and delegates motion without a second pulsing dot', () => {
    const rule = cssRule('.assistant-activity__summary')
    expect(rule).toContain('color: var(--text-muted);')

    const elapsedRule = cssRule('.assistant-activity__live-elapsed')
    expect(elapsedRule).toContain('color: var(--text-muted);')
    expect(elapsedRule).toContain('font-size: inherit;')

    // The shared whole-row treatment owns motion; readable color tokens remain
    // the static fallback, with no additional pulsing dot or local text effect.
    expect(activityDisclosureSource).not.toContain('assistant-activity-shimmer')
    expect(activityDisclosureSource).not.toContain('.assistant-activity__live-label.is-active')
    expect(activityDisclosureSource).not.toContain('background-clip: text')
    expect(activityDisclosureSource).not.toContain('assistant-activity-pulse')
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

  it.each([false, true])('keeps explicitly opened details at completion (canonical identity changes: %s)', async reconcileIdentity => {
    const state = reactive({ lifecycle: 'working' as 'working' | 'settled', stateKey: 'message-live' })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: state.lifecycle,
        defaultOpen: state.lifecycle === 'working',
        stepCount: 1,
        failureCount: 0,
        stateKey: state.stateKey,
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
    if (reconcileIdentity) state.stateKey = 'message-canonical'
    await nextTick()

    const settledSummary = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')
    expect(settledSummary?.getAttribute('aria-expanded'))
      .toBe('true')
    settledSummary?.click()
    await nextTick()
    expect(settledSummary?.getAttribute('aria-expanded'))
      .toBe('false')
  })

  it.each([false, true])('preserves the manual %s choice through live remount and completion, but not a new turn', async expanded => {
    const state = reactive({
      lifecycle: 'working' as 'working' | 'settled',
      defaultOpen: true,
      stepCount: 1,
      failureCount: 0,
      stateKey: 'message-live-remount',
      continuityKey: 'turn-remount',
    })
    const firstHost = mountDisclosure(state)
    await nextTick()
    const firstSummary = firstHost.querySelector<HTMLButtonElement>('button')!
    firstSummary.click()
    await nextTick()
    if (expanded) {
      firstSummary.click()
      await nextTick()
    }
    mountedApps.pop()?.unmount()

    state.stateKey = 'message-canonical-remount'
    const host = mountDisclosure(state)
    await nextTick()
    expect(host.querySelector('button')?.getAttribute('aria-expanded')).toBe(String(expanded))

    state.lifecycle = 'settled'
    state.defaultOpen = false
    await nextTick()
    expect(host.querySelector('button')?.getAttribute('aria-expanded')).toBe(String(expanded))
    expect(host.querySelector('.assistant-activity__body')?.getAttribute('aria-hidden')).toBe(String(!expanded))

    state.stateKey = 'message-next-turn'
    state.continuityKey = 'turn-next'
    state.lifecycle = 'working'
    state.defaultOpen = true
    await nextTick()
    expect(host.querySelector('button')?.getAttribute('aria-expanded')).toBe('true')
    state.lifecycle = 'settled'
    state.defaultOpen = false
    await nextTick()
    expect(host.querySelector('button')?.getAttribute('aria-expanded')).toBe('false')
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
  it('keeps the disclosure arrow hidden until hover or keyboard focus', () => {
    const arrowRule = cssRule('.assistant-activity__summary-arrow')
    expect(arrowRule).toContain('opacity: 0;')
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
  it('keeps live and settled headers at the same size and spacing', () => {
    for (const selector of ['.assistant-activity__live-head', '.assistant-activity__summary']) {
      const rule = cssRule(selector)
      expect(rule).toContain('font-size: 0.8125rem;')
      expect(rule).toContain('line-height: 1.5;')
      expect(rule).toContain('min-height: 1.75rem;')
      expect(rule).toContain('padding: 0.1875rem 0;')
      expect(rule).toContain('gap: 0.375rem;')
    }
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
  it('suspends nested live motion when an ancestor closes without resetting the nested choice', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, { stepCount: 1, failureCount: 0 }, {
        default: () => h(ActivityDisclosure, {
          lifecycle: 'working', executing: true, stepCount: 1, failureCount: 0,
        }),
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const outer = host.querySelector<HTMLButtonElement>('.assistant-activity__summary')!
    const inner = host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')!
    expect(host.querySelector('.activity-motion--active')).toBeNull()
    outer.click()
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()

    inner.click()
    await nextTick()
    expect(inner.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('.activity-motion--active')).toBeNull()
    outer.click()
    await nextTick()
    outer.click()
    await nextTick()
    expect(inner.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('.activity-motion--active')).toBeNull()

    inner.click()
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()
    outer.click()
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).toBeNull()
  })

  it.each(['working', 'answering'] as const)(
    'animates only the collapsed executing %s header and preserves manual disclosure',
    async lifecycle => {
      const host = mountDisclosure({
        lifecycle,
        executing: true,
        stepCount: 2,
        failureCount: 0,
      })
      await nextTick()

      const summary = host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')
      expect(host.querySelector('.activity-motion--active')).not.toBeNull()
      expect(summary?.getAttribute('aria-expanded')).toBe('false')

      summary?.click()
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).toBeNull()
      expect(summary?.getAttribute('aria-expanded')).toBe('true')

      summary?.click()
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).not.toBeNull()
      expect(summary?.getAttribute('aria-expanded')).toBe('false')
    },
  )

  it.each(['Queued', 'Waiting for approval'])(
    'keeps the live %s header static until execution is confirmed',
    async phaseLabel => {
      const props = reactive({
        lifecycle: 'working' as const,
        executing: false,
        stepCount: 0,
        failureCount: 0,
        phaseLabel,
      })
      const host = mountDisclosure(props)
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).toBeNull()
      expect(host.querySelector('[role="status"]')?.textContent?.trim()).toBe(phaseLabel)

      props.executing = true
      props.phaseLabel = 'Running commands'
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).not.toBeNull()
      expect(host.querySelector('[role="status"]')?.textContent?.trim()).toBe('Running commands')
    },
  )

  it('stops the collapsed header on stale signals and resumes only after fresh execution', async () => {
    const props = reactive({
      lifecycle: 'working' as const,
      executing: true,
      stepCount: 1,
      failureCount: 0,
      stale: false,
    })
    const host = mountDisclosure(props)
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()

    props.stale = true
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).toBeNull()

    props.stale = false
    props.executing = false
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).toBeNull()

    props.executing = true
    await nextTick()
    expect(host.querySelector('.activity-motion--active')).not.toBeNull()
  })

  it.each(['settled', 'failed', 'interrupted'] as const)(
    'stops immediately on %s and does not restart when history is expanded',
    async terminal => {
      const props = reactive({
        lifecycle: 'working' as 'working' | typeof terminal,
        executing: true,
        stepCount: 1,
        failureCount: 0,
      })
      const host = mountDisclosure(props)
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).not.toBeNull()

      props.lifecycle = terminal
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).toBeNull()

      host.querySelector<HTMLButtonElement>('.assistant-activity__summary')?.click()
      await nextTick()
      expect(host.querySelector('.activity-motion--active')).toBeNull()
      expect(host.querySelector('.assistant-activity__body')?.getAttribute('aria-hidden')).toBe('false')
    },
  )

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

  it('shows only total elapsed time even with steps and failures', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 4,
      failureCount: 3,
      phaseLabel: 'Running commands',
      elapsedLabel: '12s',
    })
    await nextTick()

    const button = host.querySelector('.assistant-activity__live-head')
    expect(button?.textContent?.trim()).toBe('Elapsed 12s')
    expect(button?.querySelector('[role="status"]')).toBeNull()
    expect(host.querySelector('.assistant-activity__live-step')).toBeNull()
    expect(host.querySelector('.assistant-activity__phase-elapsed')).toBeNull()
    expect(button?.textContent).not.toContain('Running commands')
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

  it('keeps total elapsed time through phase changes without announcing every second', async () => {
    const props = reactive({
      lifecycle: 'working' as const,
      stepCount: 2,
      failureCount: 0,
      phaseLabel: 'Running commands',
      elapsedLabel: '42s',
    })
    const host = mountDisclosure(props)
    await nextTick()

    const announcement = host.querySelector('[role="status"]')
    const timer = host.querySelector('.assistant-activity__phase-elapsed')
    const button = host.querySelector('.assistant-activity__live-head')
    expect(button?.textContent?.trim()).toBe('Elapsed 42s')
    expect(timer).toBeNull()
    expect(button?.closest('[aria-live]')).toBeNull()
    expect(announcement?.closest('button')).toBeNull()
    expect(announcement?.textContent?.trim()).toBe('Running commands')

    props.elapsedLabel = '43s'
    await nextTick()
    expect(button?.textContent?.trim()).toBe('Elapsed 43s')
    expect(announcement?.textContent?.trim()).toBe('Running commands')

    props.phaseLabel = 'Waiting for model'
    await nextTick()
    expect(button?.textContent?.trim()).toBe('Elapsed 43s')
    expect(announcement?.textContent?.trim()).toBe('Waiting for model')
    expect(host.querySelectorAll('[aria-live]')).toHaveLength(1)
  })

  it.each([
    { locale: 'en', messages: enMessages, durations: ['59s', '1m 0s', '1m 1s'],
      livePrefix: 'Time spent ', settledPrefix: 'Worked for ', phase: 'Running commands' },
    { locale: 'zh-Hans', messages: zhMessages, durations: ['59 秒', '1 分 0 秒', '1 分 1 秒'],
      livePrefix: '用时 ', settledPrefix: '工作了 ', phase: '执行命令' },
  ])('keeps the same total duration across the minute boundary and completion in $locale', async fixture => {
    const localeI18n = createI18n({
      legacy: false,
      locale: fixture.locale,
      messages: { [fixture.locale]: { chat: {
        activityElapsed: fixture.messages.chat.activityElapsed,
        activityDurationSeconds: fixture.messages.chat.activityDurationSeconds,
        activityDurationMinutes: fixture.messages.chat.activityDurationMinutes,
        workedForSeconds: fixture.messages.chat.workedForSeconds,
        workedForMinutes: fixture.messages.chat.workedForMinutes,
      } } },
    })
    const state = reactive({ seconds: 59, lifecycle: 'working' as 'working' | 'settled' })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, {
        lifecycle: state.lifecycle,
        stepCount: 1,
        failureCount: 0,
        phaseLabel: fixture.phase,
        elapsedLabel: `${state.seconds}s`,
        durationSeconds: state.seconds,
      }),
    })
    mountedApps.push(app)
    app.use(localeI18n)
    app.mount(host)
    await nextTick()

    const announcement = host.querySelector('[role="status"]')
    for (const [index, seconds] of [59, 60, 61].entries()) {
      state.seconds = seconds
      await nextTick()
      expect(host.querySelector('.assistant-activity__live-head')?.textContent?.trim())
        .toBe(`${fixture.livePrefix}${fixture.durations[index]}`)
      expect(announcement?.textContent?.trim()).toBe(fixture.phase)
      expect(announcement?.closest('button')).toBeNull()
      expect(host.querySelectorAll('[aria-live]')).toHaveLength(1)
    }

    state.lifecycle = 'settled'
    await nextTick()
    // The component's settled fallback has a different lifecycle prefix, but
    // its duration must still be the same 61 seconds and use the same units.
    expect(host.querySelector('.assistant-activity__summary')?.textContent?.trim())
      .toBe(`${fixture.settledPrefix}${fixture.durations[2]}`)
    expect(host.querySelector('[role="status"]')).toBeNull()
  })

  it('uses the supplied stable announcement when the visible process countdown changes', async () => {
    const props = reactive({
      lifecycle: 'working' as const,
      stepCount: 1,
      failureCount: 0,
      phaseLabel: 'Rate limited; retrying in about 20s',
      phaseAnnouncement: 'Rate limited; waiting to retry',
      elapsedLabel: '42s',
    })
    const host = mountDisclosure(props)
    await nextTick()

    const announcement = host.querySelector('[role="status"]')
    expect(announcement?.textContent?.trim()).toBe('Rate limited; waiting to retry')
    expect(announcement?.classList.contains('assistant-activity__sr-only')).toBe(true)
    expect(announcement?.closest('button')).toBeNull()
    props.phaseLabel = 'Rate limited; retrying in about 19s'
    props.elapsedLabel = '43s'
    await nextTick()

    expect(announcement?.textContent?.trim()).toBe('Rate limited; waiting to retry')
    expect(host.querySelector('.assistant-activity__live-head')?.textContent?.trim()).toBe('Elapsed 43s')
    expect(host.querySelectorAll('[role="status"]')).toHaveLength(1)
  })
})

describe('ActivityDisclosure stale state', () => {
  it('keeps elapsed time and process content without a visible silence warning', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
      stale: true,
      elapsedLabel: '24s',
    })
    await nextTick()

    const button = host.querySelector('.assistant-activity__live-head')
    const note = host.querySelector('.assistant-activity__stale-note')
    expect(button?.textContent?.trim()).toBe('Elapsed 24s')
    expect(note).toBeNull()
    expect(host.querySelector('.assistant-activity__body')?.textContent)
      .not.toContain('Still working — no recent signal')
    expect(host.querySelector('.assistant-activity__live-dot')).toBeNull()
    expect(host.querySelector('.activity-motion--active')).toBeNull()
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

    expect(host.querySelector('.assistant-activity__live-head')?.textContent?.trim())
      .toBe('Elapsed 0s')
    expect(host.querySelector('[role="status"]')?.textContent)
      .toContain('Running commands')
    expect(host.querySelector('[role="status"]')?.textContent)
      .not.toContain('Still working — no recent signal')
    expect(host.querySelector('.assistant-activity__stale-note')).toBeNull()
  })

  it('recovers from silence without replacing process content or the expanded header', async () => {
    const props = reactive({
      lifecycle: 'working' as const,
      stepCount: 1,
      failureCount: 0,
      phaseLabel: 'Running commands',
      elapsedLabel: '24s',
      stale: true,
      executing: true,
    })
    const host = mountDisclosure(props)
    await nextTick()
    expect(host.querySelector('.assistant-activity__stale-note')).toBeNull()
    host.querySelector<HTMLButtonElement>('.assistant-activity__live-head')?.click()
    await nextTick()
    expect(host.querySelector('.assistant-activity__live-head')?.getAttribute('aria-expanded')).toBe('true')

    props.stale = false
    props.phaseLabel = 'Waiting for model'
    props.elapsedLabel = '25s'
    await nextTick()

    expect(host.querySelector('.assistant-activity__stale-note')).toBeNull()
    expect(host.querySelector('[role="status"]')?.textContent?.trim()).toBe('Waiting for model')
    expect(host.querySelector('.activity-motion--active')).toBeNull()
    expect(host.querySelector('.assistant-activity__live-head')?.textContent?.trim()).toBe('Elapsed 25s')
    expect(host.querySelector('.assistant-activity__live-head')?.getAttribute('aria-expanded')).toBe('true')
  })

  it('starts at zero seconds with a static header when execution is not known', async () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 0,
      failureCount: 0,
    })
    await nextTick()

    expect(host.querySelector('[role="status"]')?.textContent?.trim())
      .toBe('Working')
    expect(host.querySelector('.assistant-activity__live-head')?.textContent?.trim())
      .toBe('Elapsed 0s')
    expect(host.querySelector('.assistant-activity__live-dot')).toBeNull()
    expect(host.querySelector('.activity-motion--active')).toBeNull()
  })
})

describe('ActivityDisclosure expanded boundary', () => {
  it.each(['working', 'settled'] as const)('keeps a subtle divider below the %s header', async lifecycle => {
    const host = mountDisclosure({ lifecycle, stepCount: 1, failureCount: 0 })
    await nextTick()

    const header = host.querySelector('.assistant-activity__header')
    const body = host.querySelector('.assistant-activity__body')
    expect(header?.querySelector('button[aria-controls]')).not.toBeNull()
    expect(header?.contains(body)).toBe(false)
    expect(cssRule('.assistant-activity__header'))
      .toContain('border-bottom: 1px solid var(--hairline);')
  })

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
    expect(innerRule).toContain('padding: 0;')
    expect(activityDisclosureSource).not.toContain('assistant-activity-item-enter')
    expect(activityDisclosureSource).not.toContain('animation-delay:')
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
