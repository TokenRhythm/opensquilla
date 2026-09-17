// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ChatComposerGoalMode from './ChatComposerGoalMode.vue'
import chatComposerSource from './ChatComposer.vue?raw'

const mountedApps: ReturnType<typeof createApp>[] = []
const i18n = createI18n({
  legacy: false,
  locale: 'en',
  messages: {
    en: {
      chat: {
        goal: {
          modeLabel: 'Goal mode',
          modeReady: 'Ready for a goal',
          turnOffMode: 'Turn goal mode off',
        },
      },
    },
  },
})

function mountMode(active: boolean, onDisarm = vi.fn()) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ChatComposerGoalMode, { active, onDisarm }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return { host, onDisarm }
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ChatComposerGoalMode', () => {
  it('shows the armed Goal input mode and exposes an explicit exit control', async () => {
    const { host, onDisarm } = mountMode(true)
    await nextTick()

    const group = host.querySelector<HTMLElement>('[role="group"]')
    const button = host.querySelector<HTMLButtonElement>('button')
    expect(group?.getAttribute('aria-label')).toBe('Goal mode')
    expect(group?.textContent).toContain('Goal mode')
    expect(button?.getAttribute('aria-pressed')).toBe('true')
    expect(button?.getAttribute('aria-label')).toBe('Turn goal mode off')

    button?.click()
    expect(onDisarm).toHaveBeenCalledOnce()
  })

  it('stays absent until Goal mode is armed', async () => {
    const { host } = mountMode(false)
    await nextTick()

    expect(host.querySelector('.composer-goal-mode')).toBeNull()
  })

  it('shares the composer mode slot with Plan mode', () => {
    const footerIndex = chatComposerSource.indexOf('<div class="chat-input-footer">')
    const goalModeIndex = chatComposerSource.indexOf('<ChatComposerGoalMode', footerIndex)
    const planModeIndex = chatComposerSource.indexOf('<ChatComposerPlanMode', footerIndex)
    const rightActionsIndex = chatComposerSource.indexOf(
      '<div class="chat-input-actions chat-input-actions--right">',
      footerIndex,
    )

    expect(goalModeIndex).toBeGreaterThan(footerIndex)
    expect(planModeIndex).toBeGreaterThan(goalModeIndex)
    expect(planModeIndex).toBeLessThan(rightActionsIndex)
  })
})
