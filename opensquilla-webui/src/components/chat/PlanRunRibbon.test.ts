// @vitest-environment happy-dom
import { createApp, h, nextTick, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useChatPlans } from '@/composables/chat/useChatPlans'
import type { PlanRunSnapshot, PlanRunStatus } from '@/types/plans'
import executionTodoMarkerSource from './ExecutionTodoMarker.vue?raw'
import PlanRunRibbon from './PlanRunRibbon.vue'
import planRunRibbonSource from './PlanRunRibbon.vue?raw'
import chatViewSource from '@/views/ChatView.vue?raw'

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        planRun: {
          title: 'Plan execution',
          running: 'Executing plan',
          progress: 'Step {current}/{total}',
          finishing: 'Finishing · {completed}/{total}',
          regionRunning: 'Executing plan, step {current} of {total}',
          regionFinishing: 'Finishing plan after {total} completed steps',
          regionStatus: 'Plan execution, {status}',
          cancel: 'Cancel',
          cancelling: 'Cancelling…',
          endPlan: 'End plan',
          endingPlan: 'Ending plan…',
          status: {
            queued: 'Queued',
            running: 'Running',
            paused: 'Paused',
            blocked: 'Blocked',
            completed: 'Completed',
            cancelled: 'Cancelled',
            superseded: 'Replaced by a newer plan',
          },
          stepStatus: {
            pending: 'Pending',
            in_progress: 'In progress',
            completed: 'Completed',
            blocked: 'Blocked',
            skipped: 'Skipped',
          },
        },
      },
    },
  },
})

function run(overrides: Partial<PlanRunSnapshot> = {}): PlanRunSnapshot {
  return {
    runId: 'run-7',
    planRevisionId: 'revision-2',
    status: 'running',
    currentStepId: 'build',
    steps: [
      { stepId: 'inspect', title: 'Inspect the runtime', status: 'completed' },
      { stepId: 'build', title: 'Build the feature', status: 'in_progress' },
      { stepId: 'verify', title: 'Verify behavior', status: 'pending' },
    ],
    ...overrides,
  }
}

function mountRibbon(
  snapshot: PlanRunSnapshot,
  defaultOpen = false,
  props: Record<string, unknown> = {},
) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(PlanRunRibbon, { run: snapshot, defaultOpen, ...props }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

function pointerEvent(type: string, pointerType: string): PointerEvent {
  const event = new Event(type, { bubbles: true })
  Object.defineProperty(event, 'pointerType', {
    configurable: true,
    value: pointerType,
  })
  return event as PointerEvent
}

async function tapSummary(summary: HTMLButtonElement | null): Promise<void> {
  summary?.dispatchEvent(pointerEvent('pointerdown', 'touch'))
  summary?.dispatchEvent(pointerEvent('pointerup', 'touch'))
  summary?.dispatchEvent(new MouseEvent('click', {
    bubbles: true,
    detail: 1,
  }))
  await nextTick()
}

async function keyboardActivate(
  summary: HTMLButtonElement | null,
  key: 'Enter' | ' ',
): Promise<void> {
  summary?.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key }))
  summary?.dispatchEvent(new MouseEvent('click', {
    bubbles: true,
    detail: 0,
  }))
  summary?.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key }))
  await nextTick()
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('PlanRunRibbon', () => {
  it('shows ordinal progress only for a running plan and expands its authoritative steps', async () => {
    const host = mountRibbon(run())
    await nextTick()

    const region = host.querySelector<HTMLElement>('.plan-run')
    const toggle = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    expect(region?.getAttribute('aria-label')).toBe('Plan execution')
    expect(toggle?.getAttribute('aria-label'))
      .toBe('Executing plan, step 2 of 3: Build the feature')
    expect(host.querySelector('.plan-run__summary-label')?.textContent.trim())
      .toBe('Step 2/3')
    expect(host.querySelector('.plan-run__current-title')).toBeNull()
    expect(toggle?.querySelector('[role="status"]')).toBeNull()
    expect(host.querySelector('.plan-run__control > [role="status"]')?.textContent.trim())
      .toBe('Build the feature')
    expect(toggle?.getAttribute('aria-expanded')).toBe('false')
    expect(host.querySelector('.plan-run__popover')).toBeNull()
    expect(host.querySelector('.plan-run__progress-ring')).toBeNull()
    expect(host.querySelector('.execution-todo-marker--in_progress')).not.toBeNull()

    await tapSummary(toggle)

    expect(toggle?.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('.plan-run__popover')).not.toBeNull()
    expect(host.querySelector('.plan-run__disclosure > .plan-run__popover')).not.toBeNull()
    expect(host.querySelectorAll('.plan-run__step')).toHaveLength(3)
    expect(host.querySelector('[aria-current="step"]')?.textContent).toContain('Build the feature')
    expect(host.querySelector('.plan-run__step--completed')?.textContent).toContain('Completed')
    expect(host.querySelector('.plan-run__step-state')?.classList.contains('plan-run__sr-only'))
      .toBe(false)
    expect(host.querySelector('.plan-run__step--completed .execution-todo-marker--completed'))
      .not.toBeNull()
    expect(host.querySelector('.plan-run__step--pending .execution-todo-marker--pending'))
      .not.toBeNull()
  })

  it('uses stable todo geometry instead of a radial percentage ring or font glyph markers', () => {
    expect(planRunRibbonSource).toContain('ExecutionTodoMarker')
    expect(planRunRibbonSource).toContain('<TransitionGroup')
    expect(planRunRibbonSource).not.toContain('conic-gradient')
    expect(planRunRibbonSource).not.toContain('plan-run__progress-ring')
    expect(planRunRibbonSource).not.toContain("pending: '○'")
    expect(executionTodoMarkerSource).toContain('border-radius: 50%')
    expect(executionTodoMarkerSource).not.toContain('execution-todo-breathe')
  })

  it('derives the current ordinal from the in-progress step when the pointer is absent', async () => {
    const host = mountRibbon(run({ currentStepId: undefined }))
    await nextTick()

    expect(host.querySelector('.plan-run__summary-label')?.textContent.trim())
      .toBe('Step 2/3')
  })

  it('opens on desktop pointer hover and closes when the pointer leaves', async () => {
    const host = mountRibbon(run())
    await nextTick()

    const disclosure = host.querySelector<HTMLElement>('.plan-run__disclosure')
    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    disclosure?.dispatchEvent(pointerEvent('pointerenter', 'mouse'))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('.plan-run__popover')).not.toBeNull()

    summary?.dispatchEvent(pointerEvent('pointerdown', 'mouse'))
    summary?.dispatchEvent(pointerEvent('pointerup', 'mouse'))
    summary?.dispatchEvent(new MouseEvent('click', {
      bubbles: true,
      detail: 1,
    }))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    disclosure?.dispatchEvent(pointerEvent('pointerleave', 'mouse'))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(host.querySelector('.plan-run-popover-leave-active')).not.toBeNull()
  })

  it('opens for keyboard focus and closes after focus leaves the disclosure', async () => {
    const host = mountRibbon(run())
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    summary?.focus()
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('.plan-run__popover')).not.toBeNull()

    outside.focus()
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(host.querySelector('.plan-run-popover-leave-active')).not.toBeNull()
  })

  it('keeps real keyboard focus authoritative across mouse enter and leave', async () => {
    const host = mountRibbon(run({ status: 'paused' }))
    await nextTick()

    const disclosure = host.querySelector<HTMLElement>('.plan-run__disclosure')
    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    summary?.focus()
    await nextTick()

    const endPlan = host.querySelector<HTMLButtonElement>('.plan-run__end')
    endPlan?.focus()
    disclosure?.dispatchEvent(pointerEvent('pointerenter', 'mouse'))
    disclosure?.dispatchEvent(pointerEvent('pointerleave', 'mouse'))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(endPlan)

    endPlan?.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      cancelable: true,
      key: 'Escape',
    }))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(summary)
  })

  it('ignores touch hover and preserves tap-to-toggle disclosure', async () => {
    const host = mountRibbon(run())
    await nextTick()

    const disclosure = host.querySelector<HTMLElement>('.plan-run__disclosure')
    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    disclosure?.dispatchEvent(pointerEvent('pointerenter', 'touch'))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('false')

    await tapSummary(summary)
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    await tapSummary(summary)
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
  })

  it('releases a touch latch when input switches to mouse or keyboard', async () => {
    const host = mountRibbon(run())
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    await nextTick()

    const disclosure = host.querySelector<HTMLElement>('.plan-run__disclosure')
    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    await tapSummary(summary)
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    disclosure?.dispatchEvent(pointerEvent('pointerenter', 'mouse'))
    disclosure?.dispatchEvent(pointerEvent('pointerleave', 'mouse'))
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('false')

    await tapSummary(summary)
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    summary?.focus()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    outside.focus()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
  })

  it.each([
    ['Enter', 'Enter'],
    ['Space', ' '],
  ] as const)('toggles the disclosure with %s activation', async (_label, key) => {
    const host = mountRibbon(run())
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    summary?.focus()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    await keyboardActivate(summary, key)
    expect(summary?.getAttribute('aria-expanded')).toBe('false')

    await keyboardActivate(summary, key)
    expect(summary?.getAttribute('aria-expanded')).toBe('true')
  })

  it('toggles for an assistive-technology synthetic click without pointer events', async () => {
    const host = mountRibbon(run())
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    summary?.dispatchEvent(new MouseEvent('click', {
      bubbles: true,
      detail: 0,
    }))
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    summary?.dispatchEvent(new MouseEvent('click', {
      bubbles: true,
      detail: 0,
    }))
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
  })

  it('closes with Escape, keeps focus on the summary, and reopens on later focus', async () => {
    const host = mountRibbon(run())
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    summary?.focus()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')

    summary?.dispatchEvent(new KeyboardEvent('keydown', {
      bubbles: true,
      cancelable: true,
      key: 'Escape',
    }))
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(summary)

    outside.focus()
    summary?.focus()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')
  })

  it('checks the finished todo and advances the current summary without reordering', async () => {
    const snapshot = ref(run())
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(PlanRunRibbon, {
        run: snapshot.value,
        defaultOpen: true,
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)

    expect(host.querySelector('[aria-current="step"]')?.textContent)
      .toContain('Build the feature')

    snapshot.value = run({
      currentStepId: 'verify',
      steps: [
        { stepId: 'inspect', title: 'Inspect the runtime', status: 'completed' },
        { stepId: 'build', title: 'Build the feature', status: 'completed' },
        { stepId: 'verify', title: 'Verify behavior', status: 'in_progress' },
      ],
    })
    await nextTick()

    expect([...host.querySelectorAll<HTMLElement>('.plan-run__step')]
      .map(step => step.dataset.stepId)).toEqual(['inspect', 'build', 'verify'])
    expect(host.querySelector('[data-step-id="build"]')?.classList)
      .toContain('plan-run__step--completed')
    expect(host.querySelector('[aria-current="step"]')?.textContent)
      .toContain('Verify behavior')
    expect(host.querySelector('.plan-run__summary')?.getAttribute('aria-label'))
      .toBe('Executing plan, step 3 of 3: Verify behavior')
    expect(host.querySelector('.plan-run__summary-label')?.textContent.trim())
      .toBe('Step 3/3')
  })

  it('shows a delivery-ready finishing phase without marking a completed todo current', async () => {
    const host = mountRibbon(run({
      currentStepId: undefined,
      steps: [
        { stepId: 'inspect', title: 'Inspect the runtime', status: 'completed' },
        { stepId: 'build', title: 'Build the feature', status: 'completed' },
        { stepId: 'verify', title: 'Verify behavior', status: 'skipped' },
      ],
    }), true)
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    expect(summary?.textContent).toContain('Finishing · 3/3')
    expect(summary?.getAttribute('aria-label'))
      .toBe('Finishing plan after 3 completed steps: Finishing · 3/3')
    expect(host.querySelector('[aria-current="step"]')).toBeNull()
    expect(host.querySelectorAll('.plan-run__step--completed, .plan-run__step--skipped'))
      .toHaveLength(3)
    expect(summary?.querySelector('.execution-todo-marker--in_progress')).not.toBeNull()
  })

  it('requests a stable focus return when a focused run becomes terminal', async () => {
    const snapshot = ref(run())
    const focusTarget = document.createElement('textarea')
    document.body.appendChild(focusTarget)
    const focusReturn = vi.fn(() => focusTarget.focus())
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(PlanRunRibbon, {
        run: snapshot.value,
        onFocusReturn: focusReturn,
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    host.querySelector<HTMLButtonElement>('.plan-run__summary')?.focus()
    await nextTick()
    snapshot.value = run({
      status: 'completed',
      currentStepId: undefined,
      steps: [
        { stepId: 'inspect', title: 'Inspect the runtime', status: 'completed' },
        { stepId: 'build', title: 'Build the feature', status: 'completed' },
        { stepId: 'verify', title: 'Verify behavior', status: 'completed' },
      ],
    })
    await nextTick()

    expect(focusReturn).toHaveBeenCalledOnce()
    expect(document.activeElement).toBe(focusTarget)
  })

  it.each<PlanRunStatus>([
    'queued',
    'paused',
    'blocked',
  ])('keeps %s steps inspectable as a durable todo list', async status => {
    const host = mountRibbon(run({ status }))
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    expect(summary).not.toBeNull()
    expect(host.querySelector('.plan-run__static')).toBeNull()
    expect(summary?.getAttribute('aria-expanded')).toBe('false')

    await tapSummary(summary)

    expect(host.querySelector('.plan-run__steps')).not.toBeNull()
    expect(host.querySelectorAll('.plan-run__step')).toHaveLength(3)
  })

  it.each<PlanRunStatus>([
    'completed',
    'cancelled',
    'superseded',
  ])('renders terminal %s as a compact todo summary', async status => {
    const host = mountRibbon(run({ status }))
    await nextTick()

    expect(host.querySelector('.plan-run__summary')).toBeNull()
    expect(host.querySelector('.plan-run__static')).not.toBeNull()
    expect(host.querySelector('.plan-run__steps')).toBeNull()
    expect(host.querySelector('.plan-run__static-status')?.textContent.trim()).toBe('1/3')
  })

  it('counts completed and skipped todos in the completed summary', async () => {
    const host = mountRibbon(run({
      status: 'completed',
      currentStepId: undefined,
      steps: [
        { stepId: 'inspect', title: 'Inspect the runtime', status: 'completed' },
        { stepId: 'build', title: 'Build the feature', status: 'skipped' },
        { stepId: 'verify', title: 'Verify behavior', status: 'completed' },
      ],
    }))
    await nextTick()

    expect(host.querySelector('.plan-run__static-status')?.textContent.trim()).toBe('3/3')
    expect(host.querySelector('.execution-todo-marker--completed')).not.toBeNull()
  })

  it('keeps terminal presentation when a stale running event arrives afterward', async () => {
    const sessionKey = ref('agent:main:webchat:ribbon-order')
    const handlers = new Map<string, (...args: unknown[]) => void>()
    const plans = useChatPlans({
      rpc: {
        call: vi.fn(),
        on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
          handlers.set(event, handler)
          return vi.fn()
        }),
      },
      sessionKey,
      currentEpoch: ref(0),
      isStreaming: ref(false),
      inputText: ref(''),
      createSessionKey: () => sessionKey.value,
      agentId: () => 'main',
      switchToSession: vi.fn(),
      focusComposer: vi.fn(),
      notifyError: vi.fn(),
    })
    plans.applyBootstrap({
      key: sessionKey.value,
      currentPlan: {
        revisionId: 'revision-2',
        planId: 'plan-1',
        generation: 2,
        title: 'Ship plan mode',
        markdown: 'A complete plan.',
        steps: [],
        current: true,
      },
      activePlanRun: run({
        stateRevision: 3,
        createdAt: 100,
        updatedAt: 103,
      }) as never,
    })
    plans.subscribe()

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      render: () => plans.activePlanRun.value
        ? h(PlanRunRibbon, { run: plans.activePlanRun.value })
        : null,
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)

    handlers.get('session.event.plan_run')?.({
      session_key: sessionKey.value,
      plan_run: run({
        status: 'completed',
        stateRevision: 4,
        createdAt: 100,
        updatedAt: 104,
      }),
    })
    handlers.get('session.event.plan_run')?.({
      session_key: sessionKey.value,
      plan_run: run({
        status: 'running',
        stateRevision: 3,
        createdAt: 100,
        updatedAt: 103,
      }),
    })
    await nextTick()

    expect(host.querySelector('.plan-run--completed')).not.toBeNull()
    expect(host.querySelector('.plan-run__summary')).toBeNull()
    expect(host.textContent).toContain('Completed')
    expect(host.textContent).not.toMatch(/Step \d+\/\d+/)
  })

  it.each([
    ['paused', 'Waiting for a decision', 'stale terminal text'],
    ['blocked', 'A required dependency is unavailable', 'stale terminal text'],
    ['cancelled', 'Cancelled by the user', 'stale pause text'],
    ['superseded', 'Replaced after the plan was revised', 'stale pause text'],
  ] as const)('shows the authoritative reason for a %s run', async (
    status,
    expectedReason,
    secondaryReason,
  ) => {
    const inspectable = status === 'paused' || status === 'blocked'
    const host = mountRibbon(run({
      status,
      pauseReason: inspectable
        ? expectedReason
        : secondaryReason,
      terminalReason: status === 'cancelled' || status === 'superseded'
        ? expectedReason
        : secondaryReason,
    }), inspectable)
    await nextTick()

    const selector = inspectable ? '.plan-run__popover-reason' : '.plan-run__reason'
    expect(host.querySelector(selector)?.textContent.trim()).toBe(expectedReason)
  })

  it('renders an escaped, whitespace-normalized, bounded status reason', async () => {
    const unsafeReason = `<img src=x onerror=alert(1)>\n\t${'x'.repeat(300)}`
    const host = mountRibbon(run({
      status: 'blocked',
      pauseReason: unsafeReason,
    }), true)
    await nextTick()

    const rendered = host.querySelector('.plan-run__popover-reason')?.textContent || ''
    expect(host.querySelector('.plan-run__popover-reason img')).toBeNull()
    expect(rendered).toContain('<img src=x onerror=alert(1)>')
    expect(rendered).not.toMatch(/[\n\t]/)
    expect([...rendered]).toHaveLength(160)
    expect(rendered.endsWith('…')).toBe(true)
  })

  it('renders an escaped, whitespace-normalized, bounded step reason in the running list', async () => {
    const unsafeReason = `<svg onload=alert(1)>\n\t${'x'.repeat(300)}`
    const host = mountRibbon(run({
      steps: [
        {
          stepId: 'build',
          title: 'Build the feature',
          status: 'blocked',
          reason: unsafeReason,
        },
      ],
    }), true)
    await nextTick()

    const reason = host.querySelector('.plan-run__step-reason')
    const rendered = reason?.textContent || ''
    expect(reason?.querySelector('svg')).toBeNull()
    expect(rendered).toContain('<svg onload=alert(1)>')
    expect(rendered).not.toMatch(/[\n\t]/)
    expect([...rendered]).toHaveLength(160)
    expect(rendered.endsWith('…')).toBe(true)
  })

  it('does not invent zero-based progress for a running plan with no steps', async () => {
    const host = mountRibbon(run({ steps: [], currentStepId: undefined }))
    await nextTick()

    expect(host.textContent).toContain('Executing plan')
    expect(host.querySelector('.plan-run__counter')).toBeNull()
    expect(host.textContent).not.toContain('Step 0/0')
    expect(host.querySelector('.plan-run__summary')?.tagName).toBe('DIV')
    expect(host.querySelector('.plan-run__summary')?.hasAttribute('aria-expanded')).toBe(false)
  })

  it('keeps the composer as the single cancellation action while running', async () => {
    const cancel = vi.fn()
    const host = mountRibbon(run(), false, { onCancel: cancel })
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    expect(host.querySelector('.plan-run__cancel')).toBeNull()

    await tapSummary(summary)

    expect(cancel).not.toHaveBeenCalled()
    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('.plan-run__end')).toBeNull()
  })

  it.each<PlanRunStatus>([
    'paused',
    'blocked',
  ])('keeps ending a %s plan as a low-priority expanded action', async status => {
    const cancel = vi.fn()
    const focusReturn = vi.fn()
    const host = mountRibbon(run({ status }), false, {
      onCancel: cancel,
      onFocusReturn: focusReturn,
    })
    await nextTick()

    expect(host.querySelector('.plan-run__cancel')).toBeNull()
    expect(host.querySelector('.plan-run__end')).toBeNull()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    await tapSummary(summary)

    const endPlan = host.querySelector<HTMLButtonElement>('.plan-run__end')
    expect(endPlan?.textContent.trim()).toBe('End plan')
    endPlan?.click()
    await nextTick()

    expect(cancel).toHaveBeenCalledOnce()
    expect(focusReturn).toHaveBeenCalledOnce()
  })

  it('keeps the popover open from summary to End plan and supports keyboard activation', async () => {
    const cancel = vi.fn()
    const host = mountRibbon(run({ status: 'paused' }), false, { onCancel: cancel })
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    await nextTick()

    const summary = host.querySelector<HTMLButtonElement>('.plan-run__summary')
    summary?.focus()
    await nextTick()

    const endPlan = host.querySelector<HTMLButtonElement>('.plan-run__end')
    endPlan?.focus()
    await nextTick()

    expect(summary?.getAttribute('aria-expanded')).toBe('true')
    expect(document.activeElement).toBe(endPlan)

    endPlan?.dispatchEvent(new MouseEvent('click', {
      bubbles: true,
      detail: 0,
    }))
    await nextTick()
    expect(cancel).toHaveBeenCalledOnce()

    outside.focus()
    await nextTick()
    expect(summary?.getAttribute('aria-expanded')).toBe('false')
  })

  it('disables the expanded end-plan action while cancellation is pending', async () => {
    const host = mountRibbon(
      run({ status: 'paused' }),
      true,
      { cancelBusy: true },
    )
    await nextTick()

    const endPlan = host.querySelector<HTMLButtonElement>('.plan-run__end')
    expect(endPlan?.disabled).toBe(true)
    expect(endPlan?.textContent.trim()).toBe('Ending plan…')
  })

  it('provides a reduced-motion override for disclosure transitions', () => {
    expect(planRunRibbonSource).toContain('@media (prefers-reduced-motion: reduce)')
    expect(planRunRibbonSource).toContain('transition: none;')
    expect(planRunRibbonSource).toContain('plan-run-popover-enter-active')
    expect(planRunRibbonSource).toContain('plan-run-step-move')
    expect(planRunRibbonSource).not.toContain('@keyframes plan-run-pulse')
    expect(planRunRibbonSource).not.toContain('setTimeout(')
  })

  it('keeps the live announcement outside the disclosure and uses real mobile hit targets', () => {
    expect(planRunRibbonSource).toContain('>{{ liveStatusLabel }}</span>')
    expect(planRunRibbonSource).toContain('min-height: 44px;')
    expect(planRunRibbonSource).toContain('class="plan-run__disclosure"')
  })

  it('is mounted in the composer dock instead of the transcript flow', () => {
    const dockIndex = chatViewSource.indexOf('<div class="chat-composer-dock">')
    const progressIndex = chatViewSource.indexOf('<PlanRunRibbon', dockIndex)
    const composerIndex = chatViewSource.indexOf('<ChatComposer', dockIndex)

    expect(dockIndex).toBeGreaterThanOrEqual(0)
    expect(progressIndex).toBeGreaterThan(dockIndex)
    expect(progressIndex).toBeLessThan(composerIndex)
    expect(chatViewSource.slice(0, dockIndex)).not.toContain('<PlanRunRibbon')
    expect(chatViewSource).toContain('<Transition name="plan-run-dock">')
    expect(chatViewSource).toContain('PLAN_RUN_TERMINAL_HOLD_MS')
    expect(chatViewSource).toContain('@stop="onComposerStop"')
    expect(chatViewSource).toContain('@focus-return="focusComposerAfterPlanRun"')
    expect(chatViewSource).toContain(':stop-targets-plan-run="composerStopsPlanRun"')
    expect(chatViewSource).toContain("activePlanRun.value?.status === 'queued'")
    expect(chatViewSource).toContain("activePlanRun.value?.status === 'running'")
  })
})
