// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import i18n from '@/i18n'
import { useComposerFloatingPreference } from '@/composables/useComposerFloatingPreference'
import SettingsAppearancePanel from './SettingsAppearancePanel.vue'

const mountedApps: Array<{ app: ReturnType<typeof createApp>, el: HTMLElement }> = []
const composerPreference = useComposerFloatingPreference()

async function mountPanel() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const pinia = createPinia()
  setActivePinia(pinia)
  const app = createApp(SettingsAppearancePanel)
  app.use(pinia)
  app.use(i18n)
  app.mount(el)
  mountedApps.push({ app, el })
  await nextTick()
  return el
}

beforeEach(() => {
  localStorage.clear()
  composerPreference.setEnabled(true)
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  while (mountedApps.length) {
    const mounted = mountedApps.pop()!
    mounted.app.unmount()
    mounted.el.remove()
  }
})

describe('SettingsAppearancePanel — floating composer', () => {
  it('renders the enabled floating-composer preference', async () => {
    const el = await mountPanel()
    const toggle = el.querySelector<HTMLInputElement>('[data-testid="settings-composer-fx-toggle"]')

    expect(toggle).not.toBeNull()
    expect(toggle!.getAttribute('role')).toBe('switch')
    expect(toggle!.getAttribute('aria-checked')).toBe('true')
    expect(toggle!.checked).toBe(true)
    expect(el.textContent).toContain('Floating composer')
  })

  it('flips off and persists the disabled preference', async () => {
    const el = await mountPanel()
    const toggle = el.querySelector<HTMLInputElement>('[data-testid="settings-composer-fx-toggle"]')!

    toggle.checked = false
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(toggle.getAttribute('aria-checked')).toBe('false')
    expect(JSON.parse(localStorage.getItem('opensquilla.composerFx')!)).toEqual({ enabled: false })
  })

  it('flips back on and persists the enabled preference', async () => {
    composerPreference.setEnabled(false)
    const el = await mountPanel()
    const toggle = el.querySelector<HTMLInputElement>('[data-testid="settings-composer-fx-toggle"]')!

    expect(toggle.getAttribute('aria-checked')).toBe('false')

    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(toggle.getAttribute('aria-checked')).toBe('true')
    expect(JSON.parse(localStorage.getItem('opensquilla.composerFx')!)).toEqual({ enabled: true })
  })
})
