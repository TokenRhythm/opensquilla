// @vitest-environment happy-dom
import { readFileSync } from 'node:fs'
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ChatComposerAddMenu from './ChatComposerAddMenu.vue'
import addMenuSource from './ChatComposerAddMenu.vue?raw'
import { resolveComposerAddMenuPlacement } from '@/utils/chat/composerAddMenuPlacement'

const chatViewStyles = readFileSync(
  'src/styles/chat-view.css',
  'utf8',
)

const mountedApps: ReturnType<typeof createApp>[] = []
const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        add: 'Add',
        attachFiles: 'Attach files',
        planMode: {
          label: 'Plan mode',
          readOnly: 'Read-only planning',
          turnOn: 'Turn plan mode on',
        },
        goal: {
          modeLabel: 'Goal mode',
          modeReady: 'Ready for a goal',
          modeDescription: 'Start a long-running goal',
          activeTitle: 'Goal in progress',
        },
      },
    },
  },
})

function mountMenu(overrides: Record<string, unknown> = {}) {
  const attachFiles = vi.fn()
  const activatePlanMode = vi.fn()
  const activateGoalMode = vi.fn()
  const close = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ChatComposerAddMenu, {
      attachmentsDisabled: false,
      goalModeActive: false,
      goalModeAvailable: true,
      goalModeBusy: false,
      goalModeExisting: false,
      planModeActive: false,
      planModeAvailable: true,
      planModeBusy: false,
      onActivatePlanMode: activatePlanMode,
      onActivateGoalMode: activateGoalMode,
      onAttachFiles: attachFiles,
      onClose: close,
      ...overrides,
    }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return { activateGoalMode, activatePlanMode, attachFiles, close, host }
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ChatComposerAddMenu', () => {
  it('escapes the Composer stacking context and supports collision-aware placement', () => {
    expect(chatViewStyles).toContain('.chat-composer-dock > .chat-composer')
    expect(chatViewStyles).toContain('z-index: auto')
    expect(chatViewStyles).toContain('.goal-run-dock')
    expect(chatViewStyles).toContain('z-index: 3')
    expect(addMenuSource).toContain('.composer-add-menu')
    expect(addMenuSource).toContain('z-index: 30')
    expect(addMenuSource).toContain('--composer-add-menu-lift')
    expect(addMenuSource).toContain('avoidElement')
  })

  it('lifts above the Goal boundary and remains stable when remeasured', () => {
    const initial = resolveComposerAddMenuPlacement({
      menuBottom: 410,
      currentLift: 0,
      boundaryTop: 330,
    })
    expect(initial).toEqual({ lift: 88, maxHeight: 314 })

    expect(resolveComposerAddMenuPlacement({
      menuBottom: 322,
      currentLift: initial.lift,
      boundaryTop: 330,
    })).toEqual(initial)
  })

  it('does not move a menu that already clears the Goal boundary', () => {
    expect(resolveComposerAddMenuPlacement({
      menuBottom: 280,
      currentLift: 0,
      boundaryTop: 330,
    })).toEqual({ lift: 0, maxHeight: 314 })
  })

  it('offers file attachment plus on-demand Plan and Goal mode entries', async () => {
    const { activateGoalMode, activatePlanMode, attachFiles, close, host } = mountMenu()
    await nextTick()

    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    expect(items.map(item => item.textContent?.trim())).toEqual([
      'Attach files',
      'Plan modeTurn plan mode on',
      'Goal modeStart a long-running goal',
    ])

    items[1].click()
    expect(activatePlanMode).toHaveBeenCalledOnce()
    expect(close).toHaveBeenCalledOnce()

    items[2].click()
    expect(activateGoalMode).toHaveBeenCalledOnce()
    expect(close).toHaveBeenCalledTimes(2)

    items[0].click()
    expect(attachFiles).toHaveBeenCalledOnce()
  })

  it('does not use the Add menu as an exit control for active Plan mode', async () => {
    const { host } = mountMenu({ planModeActive: true })
    await nextTick()

    const planItem = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(item => item.textContent?.includes('Plan mode'))
    expect(planItem?.disabled).toBe(true)
    expect(planItem?.getAttribute('aria-pressed')).toBe('true')
  })

  it('hides Plan mode for gateways without the Plan RPC contract', async () => {
    const { host } = mountMenu({ planModeAvailable: false })
    await nextTick()

    expect(host.querySelectorAll('[role="menuitem"]')).toHaveLength(2)
    expect(host.textContent).not.toContain('Plan mode')
  })

  it('hides Goal mode for gateways without the Goal RPC contract', async () => {
    const { host } = mountMenu({ goalModeAvailable: false })
    await nextTick()

    expect(host.querySelectorAll('[role="menuitem"]')).toHaveLength(2)
    expect(host.textContent).not.toContain('Goal mode')
  })

  it('does not allow Goal draft mode to overwrite an unfinished Goal', async () => {
    const { host } = mountMenu({ goalModeExisting: true })
    await nextTick()

    const goalItem = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
      .find(item => item.textContent?.includes('Goal mode'))
    expect(goalItem?.disabled).toBe(true)
    expect(goalItem?.textContent).toContain('Goal in progress')
  })
})
