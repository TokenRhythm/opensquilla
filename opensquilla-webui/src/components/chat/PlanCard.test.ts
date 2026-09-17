// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PlanRevisionSnapshot } from '@/types/plans'
import { clearPlanDisclosureExpansionState } from '@/utils/chat/planDisclosureState'
import PlanCard from './PlanCard.vue'
import planCardSource from './PlanCard.vue?raw'

const mountedApps: ReturnType<typeof createApp>[] = []

const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        plan: {
          label: 'Plan',
          revision: 'Revision {id}',
          current: 'Current',
          superseded: 'Updated version available',
          steps: 'Plan steps',
          expand: 'Show full plan',
          collapse: 'Collapse plan',
          implementCurrent: 'Implement in this task',
          implementNew: 'Implement in a new task',
          replan: 'Revise plan',
          working: 'Working…',
        },
      },
    },
  },
})

function plan(overrides: Partial<PlanRevisionSnapshot> = {}): PlanRevisionSnapshot {
  return {
    revisionId: 'revision-2',
    planId: 'plan-1',
    title: 'Ship plan mode',
    markdown: '**Outcome:** a safe plan card.',
    current: true,
    steps: [
      { stepId: 'inspect', title: 'Inspect the runtime', details: 'Confirm boundaries.' },
      { stepId: 'build', title: 'Build the feature' },
    ],
    ...overrides,
  }
}

function mountPlanCard(
  snapshot: PlanRevisionSnapshot,
  props: Record<string, unknown> = {},
) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(PlanCard, { plan: snapshot, ...props }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
  clearPlanDisclosureExpansionState()
})

describe('PlanCard', () => {
  it('renders plan content as a numbered plan without execution controls', async () => {
    const host = mountPlanCard(plan())
    await nextTick()

    expect(host.querySelector('.plan-card__title')?.textContent).toBe('Ship plan mode')
    expect(host.querySelector('.plan-card__markdown strong')?.textContent).toBe('Outcome:')
    expect(host.querySelectorAll('.plan-card__step')).toHaveLength(2)
    expect(host.querySelector('.plan-card__step-details')?.textContent).toBe('Confirm boundaries.')
    expect(host.querySelector('.plan-card__step-list')?.tagName).toBe('OL')
    expect(host.querySelector('input')).toBeNull()
    expect(host.querySelector('[role="progressbar"]')).toBeNull()
    expect(host.textContent).not.toMatch(/Step \d+\/\d+/)
    expect(planCardSource).not.toContain('spinner')
  })

  it('clips a long plan by default and exposes an accessible full-plan disclosure', async () => {
    const host = mountPlanCard(plan({
      markdown: `# Outcome\n\n${'A detailed implementation section. '.repeat(24)}`,
      steps: [
        { stepId: 'one', title: 'Inspect' },
        { stepId: 'two', title: 'Design' },
        { stepId: 'three', title: 'Build' },
        { stepId: 'four', title: 'Verify' },
      ],
    }))
    await nextTick()
    await nextTick()

    const body = host.querySelector<HTMLElement>('.plan-card__body')
    const disclosure = host.querySelector<HTMLButtonElement>('.plan-card__disclosure')
    expect(body?.classList.contains('plan-card__body--clipped')).toBe(true)
    expect(body?.hasAttribute('inert')).toBe(true)
    expect(disclosure?.getAttribute('aria-expanded')).toBe('false')
    expect(disclosure?.getAttribute('aria-controls')).toBe(body?.id)
    expect(disclosure?.getAttribute('aria-label')).toBe('Show full plan')
    expect(disclosure?.innerHTML).toContain('15 3 21 3 21 9')

    disclosure?.click()
    await nextTick()

    expect(disclosure?.getAttribute('aria-expanded')).toBe('true')
    expect(body?.hasAttribute('inert')).toBe(false)
    expect(disclosure?.getAttribute('aria-label')).toBe('Collapse plan')
    expect(disclosure?.innerHTML).toContain('4 14 10 14 10 20')
    expect(host.querySelector('.plan-card__actions')).not.toBeNull()
  })

  it('does not add a disclosure to a short plan', async () => {
    const host = mountPlanCard(plan({
      markdown: 'A concise plan.',
      steps: [{ stepId: 'one', title: 'Verify it' }],
    }))
    await nextTick()
    await nextTick()

    expect(host.querySelector('.plan-card__disclosure')).toBeNull()
    expect(host.querySelector('.plan-card__body')?.hasAttribute('inert')).toBe(false)
  })

  it('routes stored markdown through the shared sanitized chat renderer', () => {
    expect(planCardSource).toContain('useChatTextRendering')
    expect(planCardSource).toContain('renderMarkdown(markdown)')
    expect(planCardSource).not.toContain('v-html="plan.markdown"')
  })

  it('uses measured height motion without a fixed expanded max-height or nested scrolling', () => {
    expect(planCardSource).toContain('content.scrollHeight')
    expect(planCardSource).toContain('prefers-reduced-motion: reduce')
    expect(planCardSource).not.toContain('max-height:')
    expect(planCardSource).not.toContain('overflow-y: auto')
  })

  it('removes markdown task-list inputs because a plan is not an execution checklist', async () => {
    const host = mountPlanCard(plan({ markdown: '- [ ] Build the feature' }))
    await nextTick()

    expect(host.querySelector('.plan-card__markdown')?.textContent).toContain('Build the feature')
    expect(host.querySelector('.plan-card__markdown input')).toBeNull()
  })

  it('keeps markdown list markers inside a logical nested-list gutter', async () => {
    const host = mountPlanCard(plan({
      markdown: '- Parent item\n    - Nested item\n\n1. Ordered item',
    }))
    await nextTick()

    expect(host.querySelectorAll('.plan-card__markdown ul')).toHaveLength(1)
    expect(host.querySelectorAll('.plan-card__markdown ol')).toHaveLength(1)
    expect(planCardSource).toContain('padding-inline-start: 1.75rem')
    expect(planCardSource).toContain('list-style-position: outside')
    expect(planCardSource).toContain('.plan-card__markdown :deep(li > ul)')
    expect(planCardSource).toContain('max-inline-size: 100%')
    expect(planCardSource).toContain('overflow-wrap: anywhere')
    expect(planCardSource).toContain('width: 44px')
  })

  it('keeps two-digit structured step numbers inside the card at narrow widths', async () => {
    const host = mountPlanCard(plan({
      steps: Array.from({ length: 10 }, (_, index) => ({
        stepId: `step-${index + 1}`,
        title: `Step ${index + 1}`,
        details: 'A long detail that must wrap inside the available column.',
      })),
    }))
    await nextTick()

    expect(host.querySelectorAll('.plan-card__step-copy')).toHaveLength(10)
    expect(planCardSource).toContain('counter-reset: plan-step')
    expect(planCardSource).toContain('counter-increment: plan-step')
    expect(planCardSource).toContain('grid-template-columns: minmax(2rem, auto) minmax(0, 1fr)')
    expect(planCardSource).not.toContain('.plan-card__step::marker')
  })

  it('emits stable plan identifiers for each current-plan action', async () => {
    const implementCurrent = vi.fn()
    const implementNew = vi.fn()
    const replan = vi.fn()
    const host = mountPlanCard(plan(), {
      onImplementCurrent: implementCurrent,
      onImplementNew: implementNew,
      onReplan: replan,
    })
    await nextTick()

    const buttons = host.querySelectorAll<HTMLButtonElement>('.plan-card__actions button')
    expect(buttons).toHaveLength(3)
    expect(buttons[0]?.classList.contains('btn--primary')).toBe(true)
    buttons[0]?.click()
    buttons[1]?.click()
    buttons[2]?.click()

    const target = { planId: 'plan-1', revisionId: 'revision-2' }
    expect(implementCurrent).toHaveBeenCalledWith(target)
    expect(implementNew).toHaveBeenCalledWith(target)
    expect(replan).toHaveBeenCalledWith(target)
  })

  it('makes superseded revisions visibly historical and removes their actions', async () => {
    const host = mountPlanCard(plan({ current: false }))
    await nextTick()

    expect(host.querySelector('.plan-card--superseded')).not.toBeNull()
    expect(host.textContent).toContain('Updated version available')
    expect(host.querySelector('.plan-card__actions')).toBeNull()
  })

  it('exposes a labelled article and disables all actions while one is pending', async () => {
    const host = mountPlanCard(plan(), { pendingAction: 'implement-new' })
    await nextTick()

    const card = host.querySelector<HTMLElement>('.plan-card')
    const title = host.querySelector<HTMLElement>('.plan-card__title')
    expect(card?.getAttribute('aria-labelledby')).toBe(title?.id)
    expect(Array.from(host.querySelectorAll<HTMLButtonElement>('button')).every(button => button.disabled))
      .toBe(true)
    expect(host.textContent).toContain('Working…')
  })
})
