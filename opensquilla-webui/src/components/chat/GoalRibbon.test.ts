// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'

import GoalRibbon from './GoalRibbon.vue'
import goalRibbonSource from './GoalRibbon.vue?raw'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'
import en from '@/locales/en.json'

const mountedApps: ReturnType<typeof createApp>[] = []
const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })

function goal(overrides: Partial<GoalSnapshot> = {}): GoalSnapshot {
  return {
    goalId: 'goal-1',
    sessionKey: 'agent:main:webchat:test',
    sessionId: 'session-1',
    epoch: 1,
    objective: 'Ship the Goal refactor',
    status: 'active',
    stateRevision: 4,
    objectiveRevision: 1,
    progressRevision: 2,
    progress: {
      explanation: 'Backend is complete; reconnect UI remains.',
      steps: [
        { text: 'Implement storage', status: 'completed' },
        { text: 'Wire hydration', status: 'in_progress' },
      ],
    },
    continuationSeq: 2,
    activeTaskId: 'task-1',
    executionState: 'working',
    continuationDeferredReason: null,
    turnsStarted: 3,
    turnsSettled: 2,
    windowTurnsStarted: 3,
    activeTimeMs: 12_000,
    windowActiveTimeMs: 12_000,
    usage: {
      inputTokens: 100,
      outputTokens: 50,
      reasoningTokens: 20,
      cacheReadTokens: 0,
      cacheWriteTokens: 0,
      totalTokens: 170,
    },
    pauseReason: null,
    blockedReason: null,
    terminalReason: null,
    createdAt: 1,
    updatedAt: 2,
    finishedAt: null,
    usageCoverage: 'complete',
    executionPolicy: 'foreground',
    ...overrides,
  }
}

function mountRibbon(props: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(GoalRibbon, {
      goal: goal(),
      elapsed: '12s',
      tokenBudgetSupported: true,
      backgroundExecutionSupported: true,
      ...props,
    }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

async function openActions(host: HTMLElement) {
  host.querySelector<HTMLButtonElement>('button[aria-label="Goal actions"]')?.click()
  await nextTick()
}

function openMenu(host: HTMLElement) {
  host.querySelector<HTMLButtonElement>('button[aria-label="Goal actions"]')?.click()
}

afterEach(() => {
  for (const app of mountedApps.splice(0)) app.unmount()
  document.body.innerHTML = ''
})

describe('GoalRibbon', () => {
  it('keeps the legacy objective editor usable without sending unsupported settings', async () => {
    const onEdit = vi.fn()
    const onEditOpen = vi.fn()
    const host = mountRibbon({ goal: goal({ usageCoverage: undefined, executionPolicy: undefined }),
      tokenBudgetSupported: false, backgroundExecutionSupported: false, onEdit, onEditOpen })
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    expect(onEditOpen).toHaveBeenCalledOnce()
    expect(host.querySelector('input[type="number"]')).toBeNull()
    expect(host.querySelector('select')).toBeNull()
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).toEqual({})
  })

  it('keeps existing settings while capabilities load instead of writing defaults', async () => {
    const onEdit = vi.fn()
    const props = reactive({ goal: goal({ tokenBudget: 5000, executionPolicy: 'background' }),
      tokenBudgetSupported: false, backgroundExecutionSupported: false, onEdit })
    const host = mountRibbon(props)
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    props.tokenBudgetSupported = true
    props.backgroundExecutionSupported = true
    await nextTick()
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe('5000')
    expect(host.querySelector<HTMLSelectElement>('select')?.value).toBe('background')
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).toEqual({})
  })

  it('keeps a dirty budget in the edit delta when reconnect hides optional controls', async () => {
    const onEdit = vi.fn((_objective: string, settle: (accepted: boolean) => void, _options?: import('@/modules/goalCenter').GoalExecutionOptions) => settle(false))
    const props = reactive({ goal: goal({ tokenBudget: 5000 }),
      tokenBudgetSupported: true, backgroundExecutionSupported: true, onEdit })
    const host = mountRibbon(props)
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    const budget = host.querySelector<HTMLInputElement>('input[type="number"]')!
    budget.value = '9000'
    budget.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    props.tokenBudgetSupported = false
    props.backgroundExecutionSupported = false
    await nextTick()
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).toEqual({ tokenBudget: 9000 })
    await nextTick()
    expect(host.querySelector('textarea')).not.toBeNull()
    props.tokenBudgetSupported = true
    await nextTick()
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.value).toBe('9000')
  })

  it('does not permit a budget or claim receipt recovery when existing coverage is unknown', async () => {
    const onEdit = vi.fn()
    const host = mountRibbon({ goal: goal({ status: 'paused', pauseReason: 'usage_unknown', usageCoverage: undefined }), onEdit })
    expect(host.textContent).not.toContain('Usage receipts are complete')
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.disabled).toBe(true)
    expect(host.textContent).toContain('Usage accounting is unavailable. A token budget cannot be set.')
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).not.toHaveProperty('tokenBudget')
  })

  it('explains missing usage receipts and disables budget edits until accounting is complete', async () => {
    const host = mountRibbon({ goal: goal({ status: 'paused', usageCoverage: 'partial_usage', pauseReason: 'usage_unknown' }) })
    expect(host.textContent).toContain('Waiting for usage receipts')
    await openActions(host)
    Array.from(host.querySelectorAll('button')).find(button => button.textContent?.trim() === 'Edit goal')?.click()
    await nextTick()
    expect(host.querySelector<HTMLInputElement>('input[type="number"]')?.disabled).toBe(true)
  })

  it('allows removing an existing budget while usage receipts are incomplete', async () => {
    const onEdit = vi.fn()
    const host = mountRibbon({ goal: goal({ usageCoverage: 'partial_usage', tokenBudget: 8000 }), onEdit })
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    Array.from(host.querySelectorAll('button')).find(button => button.textContent?.trim() === 'Remove token budget')?.click()
    await nextTick()
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).toEqual({ tokenBudget: null })
  })

  it.each(['complete', 'partial_history'] as const)('offers manual resume after late receipts restore %s accounting', usageCoverage => {
    const host = mountRibbon({ goal: goal({ status: 'paused', usageCoverage, pauseReason: 'usage_unknown' }) })
    expect(host.textContent).toContain('Usage receipts are complete. Resume the Goal when ready.')
    expect(host.textContent).not.toContain('Waiting for usage receipts')
    expect(host.querySelector('[data-action="resume"]')).not.toBeNull()
  })

  it('shows why repeated empty automatic turns paused the Goal', () => {
    const host = mountRibbon({ goal: goal({ status: 'paused', pauseReason: 'empty_continuations' }) })
    expect(host.querySelector('.goal-ribbon__meta')?.textContent).toContain('Paused after repeated turns with no output or tool activity')
    expect(host.querySelector('[data-action="resume"]')).not.toBeNull()
  })

  it('shows Goal progress, accounting, and Plan deferral', () => {
    const host = mountRibbon({ planModeActive: true })

    expect(host.textContent).toContain('Ship the Goal refactor')
    expect(host.textContent).toContain('waiting for Plan mode')
    expect(host.textContent).toContain('2 turns')
    expect(host.textContent).toContain('170 tokens')
    expect(host.textContent).toContain('Progress 1/2')
    expect(host.textContent).toContain('Backend is complete; reconnect UI remains.')
    expect(host.textContent).toContain('Implement storage')
    expect(host.textContent).toContain('Wire hydration')
    expect(host.querySelector('.goal-ribbon__progress')).not.toBeNull()
    const status = host.querySelector('[role="status"]')
    expect(status).not.toBeNull()
    expect(status?.querySelector('button')).not.toBeNull()
  })

  it('expands and collapses the complete objective without changing Goal state', async () => {
    const host = mountRibbon({
      goal: goal({ objective: 'Ship the first line\nwith all acceptance details visible' }),
    })
    const objective = host.querySelector<HTMLButtonElement>('.goal-ribbon__text')
    expect(objective?.getAttribute('aria-expanded')).toBe('false')
    expect(objective?.classList.contains('goal-ribbon__text--expanded')).toBe(false)

    objective?.click()
    await nextTick()
    expect(objective?.getAttribute('aria-expanded')).toBe('true')
    expect(objective?.classList.contains('goal-ribbon__text--expanded')).toBe(true)
    expect(objective?.textContent).toContain('with all acceptance details visible')
    expect(host.querySelector('.goal-ribbon__meta')?.classList)
      .toContain('goal-ribbon__meta--visible')

    objective?.click()
    await nextTick()
    expect(objective?.getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps authoritative elapsed time and accounting visible in the compact active row', () => {
    const host = mountRibbon()
    const meta = host.querySelector('.goal-ribbon__meta')

    expect(meta?.textContent).toContain('12s active')
    expect(meta?.textContent).toContain('2 turns')
    expect(meta?.textContent).toContain('170 tokens')
    expect(meta?.classList.contains('goal-ribbon__meta--visible')).toBe(true)
  })

  it('omits zero Goal accounting values from the compact active row', () => {
    const host = mountRibbon({
      goal: goal({
        turnsStarted: 0,
        turnsSettled: 0,
        usage: {
          inputTokens: 0,
          outputTokens: 0,
          reasoningTokens: 0,
          cacheReadTokens: 0,
          cacheWriteTokens: 0,
          totalTokens: 0,
        },
      }),
    })
    const meta = host.querySelector('.goal-ribbon__meta')

    expect(meta?.textContent).toContain('12s active')
    expect(meta?.textContent).not.toContain('turns')
    expect(meta?.textContent).not.toContain('tokens')
  })

  it('keeps lifecycle reasons visible without requiring expansion', () => {
    const host = mountRibbon({
      goal: goal({
        status: 'paused',
        activeTaskId: null,
        executionState: 'idle',
        pauseReason: 'user',
      }),
    })

    expect(host.querySelector('.goal-ribbon__meta')?.classList)
      .toContain('goal-ribbon__meta--visible')
  })

  it('offers one labeled lifecycle action and only Edit and Remove in overflow', async () => {
    const onResume = vi.fn()
    const onClear = vi.fn()
    const host = mountRibbon({
      goal: goal({
        status: 'blocked',
        activeTaskId: null,
        executionState: 'idle',
        blockedReason: 'Needs operator input',
      }),
      onResume,
      onClear,
    })

    const lifecycle = host.querySelector<HTMLButtonElement>('[data-testid="goal-lifecycle-action"]')
    expect(lifecycle?.textContent).toContain('Continue goal')
    expect(lifecycle?.dataset.action).toBe('resume')
    lifecycle?.click()
    expect(onResume).toHaveBeenCalledOnce()

    await openActions(host)
    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    expect(items.map(item => item.textContent?.trim())).toEqual(['Edit goal', 'Remove goal'])
    expect(host.querySelector('button[aria-label="Hide goal status"]')).toBeNull()
    expect(host.querySelector('button[aria-label="Clear goal"]')).toBeNull()
    items[1]?.click()
    expect(onClear).toHaveBeenCalledOnce()
    expect(host.textContent).toContain('Needs operator input')
  })

  it('shows the canonical user pause reason', () => {
    const host = mountRibbon({
      goal: goal({
        status: 'paused',
        activeTaskId: null,
        executionState: 'idle',
        pauseReason: 'user',
      }),
    })

    expect(host.textContent).toContain('paused by you')
    expect(host.querySelector('[data-testid="goal-lifecycle-action"]')?.textContent)
      .toContain('Resume goal')
  })

  it('prioritizes a missing delivery checkpoint before accounting metadata', () => {
    const host = mountRibbon({
      goal: goal({
        status: 'paused',
        activeTaskId: null,
        executionState: 'idle',
        pauseReason: 'goal_checkpoint_required',
      }),
    })

    const meta = host.querySelector('.goal-ribbon__meta')
    expect(meta?.textContent?.trim()).toMatch(
      /^artifact delivered, but Goal status was not confirmed/,
    )
    expect(meta?.textContent).toContain('2 turns')
    expect(host.querySelector('details')).not.toBeNull()
  })

  it('keeps a disconnected Goal active and offers explicit continuation', () => {
    const onTakeover = vi.fn()
    const host = mountRibbon({
      goal: goal({
        status: 'active',
        activeTaskId: null,
        executionState: 'idle',
        continuationDeferredReason: 'owner_disconnected',
      }),
      connectionTakeoverAvailable: true,
      onTakeover,
    })

    expect(host.textContent).toContain('Goal still active')
    expect(host.textContent).toContain('automatic progress is waiting for this tab to reconnect')
    const continueButton = host.querySelector<HTMLButtonElement>(
      '[data-testid="goal-lifecycle-action"]',
    )
    expect(continueButton).toBeDefined()
    continueButton?.click()
    expect(onTakeover).toHaveBeenCalledOnce()
    expect(host.textContent).not.toContain('Goal paused')
  })

  it.each(['complete', 'partial_history'] as const)('edits an existing %s budget and execution policy through the same goal mutation', async usageCoverage => {
    const onEdit = vi.fn((_objective: string, settle: (accepted: boolean) => void, _options?: import('@/modules/goalCenter').GoalExecutionOptions) => settle(true))
    const host = mountRibbon({ goal: goal({ usageCoverage, tokenBudget: 5000, budgetTokensUsed: 1200, usageAccountingStartedAtMs: 1800000000000 }), onEdit })
    expect(host.textContent).toContain('1,200 / 5,000 tokens')
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    const budget = host.querySelector<HTMLInputElement>('input[type="number"]')!
    expect(budget.value).toBe('5000')
    budget.value = '9000'
    budget.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    const policy = host.querySelector<HTMLSelectElement>('select')!
    policy.value = 'background'
    policy.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).toEqual({ tokenBudget: 9000, executionPolicy: 'background' })
  })

  it.each([null, 1800000000000])('sets a budget for recorded usage with accounting start %s', async usageAccountingStartedAtMs => {
    const onEdit = vi.fn()
    const host = mountRibbon({ goal: goal({ usageCoverage: 'partial_history', usageAccountingStartedAtMs }), onEdit })
    await openActions(host)
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')?.click()
    await nextTick()
    const budget = host.querySelector<HTMLInputElement>('input[type="number"]')!
    expect(budget.disabled).toBe(false)
    expect(host.querySelector<HTMLSelectElement>('select')?.disabled).toBe(false)
    expect(host.textContent).toContain('Earlier usage is incomplete and is not counted toward the token budget.')
    if (usageAccountingStartedAtMs === null) {
      expect(host.textContent).toContain('Budget accounting starts with the next model request.')
    } else {
      expect(host.textContent).toContain(`The token budget counts usage recorded since ${new Date(usageAccountingStartedAtMs).toLocaleString()}.`)
    }
    budget.value = '8000'
    budget.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    expect(onEdit.mock.calls[0]?.[2]).toEqual({ tokenBudget: 8000 })
  })

  it('explains token-budget pauses before usage and background metadata', () => {
    const host = mountRibbon({ goal: goal({ status: 'paused', pauseReason: 'token_budget', executionPolicy: 'background' }) })
    expect(host.querySelector('.goal-ribbon__meta')?.textContent).toContain('Token budget reached')
    expect(host.querySelector('.goal-ribbon__meta')?.textContent).toContain('Background')
  })

  it('edits the objective with a labeled multiline field', async () => {
    const onEdit = vi.fn((
      _objective: string,
      settle: (accepted: boolean) => void,
    ) => settle(true))
    const host = mountRibbon({ onEdit })
    await openActions(host)
    const editButton = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit goal'))
    editButton?.click()
    await nextTick()

    const input = host.querySelector<HTMLTextAreaElement>('textarea')
    expect(input?.hasAttribute('maxlength')).toBe(false)
    expect(input?.labels?.[0]?.textContent).toContain('Goal objective')
    expect(input?.getAttribute('rows')).toBe('3')
    expect(goalRibbonSource).toContain('resize: none')
    expect(input?.style.height).toBe('64px')
    if (!input) throw new Error('edit textarea was not rendered')
    input.value = 'Ship the safer Goal refactor\nwith multiline acceptance details'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    await nextTick()

    expect(onEdit.mock.calls[0]?.[0]).toBe(
      'Ship the safer Goal refactor\nwith multiline acceptance details',
    )
    expect(document.activeElement).toBe(
      host.querySelector<HTMLButtonElement>('button[aria-label="Goal actions"]'),
    )
  })

  it('keeps the edit draft and focus when an asynchronous edit is rejected', async () => {
    let settleEdit: ((accepted: boolean) => void) | undefined
    const onEdit = vi.fn((
      _objective: string,
      settle: (accepted: boolean) => void,
    ) => {
      settleEdit = settle
    })
    const host = mountRibbon({ onEdit })
    await openActions(host)
    ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit goal'))?.click()
    await nextTick()

    const input = host.querySelector<HTMLTextAreaElement>('textarea')
    if (!input) throw new Error('edit textarea was not rendered')
    input.value = 'Keep this rejected draft'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    await nextTick()

    expect(input.disabled).toBe(true)
    settleEdit?.(false)
    await nextTick()
    expect(host.querySelector<HTMLTextAreaElement>('textarea')?.value)
      .toBe('Keep this rejected draft')
    expect(document.activeElement).toBe(input)
  })

  it('returns focus to the actions trigger after cancelling an edit', async () => {
    const host = mountRibbon()
    await openActions(host)
    ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit goal'))?.click()
    await nextTick()

    const cancel = [...host.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.trim() === 'Cancel')
    cancel?.click()
    await nextTick()

    expect(document.activeElement).toBe(
      host.querySelector<HTMLButtonElement>('button[aria-label="Goal actions"]'),
    )
  })

  it('auto-grows the editor to a bounded height instead of exposing drag resize', async () => {
    const host = mountRibbon()
    await openActions(host)
    ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit goal'))?.click()
    await nextTick()

    const input = host.querySelector<HTMLTextAreaElement>('textarea')
    if (!input) throw new Error('edit textarea was not rendered')
    Object.defineProperty(input, 'scrollHeight', { configurable: true, value: 260 })
    input.dispatchEvent(new Event('input', { bubbles: true }))

    expect(input.style.height).toBe('180px')
    expect(input.style.overflowY).toBe('auto')
  })

  it('does not truncate a valid astral objective in the edit input', async () => {
    const onEdit = vi.fn()
    const host = mountRibbon({ onEdit })
    await openActions(host)
    ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(button => button.textContent?.includes('Edit goal'))?.click()
    await nextTick()

    const input = host.querySelector<HTMLTextAreaElement>('textarea')
    if (!input) throw new Error('edit textarea was not rendered')
    const astralObjective = '🦑'.repeat(3000)
    expect(astralObjective.length).toBe(6000)
    input.value = astralObjective
    input.dispatchEvent(new Event('input', { bubbles: true }))
    host.querySelector<HTMLButtonElement>('button[type="submit"]')?.click()
    await nextTick()

    expect(onEdit.mock.calls[0]?.[0]).toBe(astralObjective)
  })

  it.each([
    {
      label: 'active working',
      snapshot: { status: 'active', activeTaskId: 'task-1', executionState: 'working' },
      action: 'pause',
      text: 'Pause after this turn',
    },
    {
      label: 'paused working',
      snapshot: { status: 'paused', activeTaskId: 'task-1', executionState: 'working' },
      action: 'resume',
      text: 'Resume automatic continuation',
    },
    {
      label: 'paused idle',
      snapshot: { status: 'paused', activeTaskId: null, executionState: 'idle' },
      action: 'resume',
      text: 'Resume goal',
    },
    {
      label: 'blocked',
      snapshot: { status: 'blocked', activeTaskId: null, executionState: 'idle' },
      action: 'resume',
      text: 'Continue goal',
    },
    {
      label: 'usage limited',
      snapshot: { status: 'usage_limited', activeTaskId: null, executionState: 'idle' },
      action: 'resume',
      text: 'Retry goal',
    },
  ])('maps $label to the authoritative lifecycle action', ({ snapshot, action, text }) => {
    const host = mountRibbon({ goal: goal(snapshot as Partial<GoalSnapshot>) })
    const button = host.querySelector<HTMLButtonElement>('[data-testid="goal-lifecycle-action"]')
    expect(button?.dataset.action).toBe(action)
    expect(button?.textContent).toContain(text)
  })

  it('renders an unsettled complete Goal as non-interactive finalization', () => {
    const host = mountRibbon({
      goal: goal({ status: 'complete', activeTaskId: 'task-1', executionState: 'working' }),
    })

    expect(host.textContent).toContain('Finalizing result')
    expect(host.querySelector('[data-testid="goal-lifecycle-action"]')).toBeNull()
    expect(host.querySelector('[aria-haspopup="menu"]')).toBeNull()
  })

  it('keeps the overflow menu reachable on a settled complete Goal', async () => {
    const onClear = vi.fn()
    const host = mountRibbon({
      goal: goal({ status: 'complete', activeTaskId: null, executionState: 'idle' }),
      onClear,
    })

    expect(host.textContent).not.toContain('Finalizing result')
    const trigger = host.querySelector<HTMLButtonElement>('button[aria-label="Goal actions"]')
    expect(trigger).not.toBeNull()
    await openActions(host)
    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    expect(items.map(item => item.textContent?.trim())).toEqual(['Edit goal', 'Remove goal'])
    items[1]?.click()
    expect(onClear).toHaveBeenCalledOnce()
  })

  it('does not reopen the overflow menu when a complete Goal is still settling', async () => {
    const host = mountRibbon({
      goal: goal({ status: 'complete', activeTaskId: 'task-1', executionState: 'working' }),
    })

    expect(host.querySelector('button[aria-label="Goal actions"]')).toBeNull()
    openMenu(host)
    await nextTick()
    expect(host.querySelector('[role="menu"]')).toBeNull()
  })

  it('uses touch-sized actions on narrow layouts', () => {
    expect(goalRibbonSource).toContain('min-height: 44px')
    expect(goalRibbonSource).toContain('min-width: 44px')
  })
})
