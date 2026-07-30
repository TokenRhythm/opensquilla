// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ChatComposerAddMenu from './ChatComposerAddMenu.vue'

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
      },
    },
  },
})

function mountMenu(overrides: Record<string, unknown> = {}) {
  const attachFiles = vi.fn()
  const activatePlanMode = vi.fn()
  const close = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ChatComposerAddMenu, {
      attachmentsDisabled: false,
      planModeActive: false,
      planModeAvailable: true,
      planModeBusy: false,
      onActivatePlanMode: activatePlanMode,
      onAttachFiles: attachFiles,
      onClose: close,
      ...overrides,
    }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return { activatePlanMode, attachFiles, close, host }
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ChatComposerAddMenu', () => {
  it('offers file attachment and an on-demand Plan mode entry', async () => {
    const { activatePlanMode, attachFiles, close, host } = mountMenu()
    await nextTick()

    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    expect(items.map(item => item.textContent?.trim())).toEqual([
      'Attach files',
      'Plan modeTurn plan mode on',
    ])

    items[1].click()
    expect(activatePlanMode).toHaveBeenCalledOnce()
    expect(close).toHaveBeenCalledOnce()

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

    expect(host.querySelectorAll('[role="menuitem"]')).toHaveLength(1)
    expect(host.textContent).not.toContain('Plan mode')
  })
})
