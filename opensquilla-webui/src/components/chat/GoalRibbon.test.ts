// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
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

afterEach(() => {
  for (const app of mountedApps.splice(0)) app.unmount()
  document.body.innerHTML = ''
})

describe('GoalRibbon', () => {
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

  it('keeps routine accounting out of the compact active row', () => {
    const host = mountRibbon()
    const meta = host.querySelector('.goal-ribbon__meta')

    expect(meta?.textContent).toContain('2 turns')
    expect(meta?.textContent).toContain('170 tokens')
    expect(meta?.classList.contains('goal-ribbon__meta--visible')).toBe(false)
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

  it('uses touch-sized actions on narrow layouts', () => {
    expect(goalRibbonSource).toContain('min-height: 44px')
    expect(goalRibbonSource).toContain('min-width: 44px')
  })
})
