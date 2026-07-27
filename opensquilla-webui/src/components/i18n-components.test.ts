// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { createApp, nextTick, type Component } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import i18n from '@/i18n'
import { useAppStore } from '@/stores/app'
import SettingsAppearancePanel from '@/components/settings/SettingsAppearancePanel.vue'
import LanguageSwitcher from '@/components/LanguageSwitcher.vue'
import {
  TOOL_DETAIL_DISPLAY_STORAGE_KEY,
  useToolDetailPreference,
} from '@/composables/useToolDetailPreference'

// Mount a component with the real i18n + a fresh pinia into a happy-dom node, so
// the switcher surfaces can be exercised without the SettingsDialog `loaded`
// gate (which needs a live gateway).
async function mount(Comp: Component) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const pinia = createPinia()
  setActivePinia(pinia)
  const app = createApp(Comp)
  app.use(pinia)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { el, app }
}


beforeEach(() => {
  i18n.global.locale.value = 'en'
  useToolDetailPreference().setMode('auto')
  localStorage.clear()
  document.documentElement.removeAttribute('lang')
})

describe('SettingsAppearancePanel — Tool details row', () => {
  it('offers all three defaults and persists the selection immediately', async () => {
    const { el } = await mount(SettingsAppearancePanel)
    const group = el.querySelector('[data-testid="settings-tool-details-group"]')
    const auto = el.querySelector('[data-testid="settings-tool-details-auto"]') as HTMLInputElement
    const compact = el.querySelector('[data-testid="settings-tool-details-compact"]') as HTMLInputElement
    const expanded = el.querySelector('[data-testid="settings-tool-details-expanded"]') as HTMLInputElement

    expect(group?.textContent).toContain('Auto')
    expect(group?.textContent).toContain('Compact')
    expect(group?.textContent).toContain('Expanded')
    expect(auto.checked).toBe(true)

    compact.checked = true
    compact.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(compact.checked).toBe(true)
    expect(expanded.checked).toBe(false)
    expect(useToolDetailPreference().mode.value).toBe('compact')
    expect(localStorage.getItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY)).toBe('compact')
  })
})

describe('SettingsAppearancePanel — Language row', () => {
  it('renders a Language radiogroup with native English / 中文 labels', async () => {
    const { el } = await mount(SettingsAppearancePanel)
    const group = el.querySelector('[data-testid="settings-language-group"]')
    expect(group).toBeTruthy()
    expect(el.querySelector('[data-testid="settings-language-en"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="settings-language-zh-Hans"]')).toBeTruthy()
    expect(group!.textContent).toContain('English')
    expect(group!.textContent).toContain('中文')
  })

  it('switching the radio sets the locale, persists it, and reactively localizes the panel', async () => {
    const { el } = await mount(SettingsAppearancePanel)
    const store = useAppStore()
    expect(el.querySelector('.control-section__title')!.textContent).toContain('Appearance')

    const zh = el.querySelector('[data-testid="settings-language-zh-Hans"]') as HTMLInputElement
    zh.checked = true
    zh.dispatchEvent(new Event('change', { bubbles: true }))
    // setLocale lazy-imports the locale chunk; wait on the outcome, not a tick.
    await vi.waitFor(() => expect(store.locale).toBe('zh-Hans'))
    await nextTick()

    expect(localStorage.getItem('opensquilla-locale')).toBe('zh-Hans')
    expect(document.documentElement.getAttribute('lang')).toBe('zh-Hans')
    // section title re-renders in Chinese (reactive t())
    await vi.waitFor(() =>
      expect(el.querySelector('.control-section__title')!.textContent).toContain('外观'))
  })
})

describe('LanguageSwitcher — topbar dropdown', () => {
  it('shows the active locale label and opens a menu of options', async () => {
    const { el } = await mount(LanguageSwitcher)
    const trigger = el.querySelector('[data-testid="language-switcher-trigger"]') as HTMLButtonElement
    expect(trigger).toBeTruthy()
    expect(trigger.textContent).toContain('English')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')

    trigger.click()
    await nextTick()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('[data-testid="language-option-en"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="language-option-zh-Hans"]')).toBeTruthy()
  })

  it('picking 中文 sets the locale and closes the menu', async () => {
    const { el } = await mount(LanguageSwitcher)
    const store = useAppStore()
    const trigger = el.querySelector('[data-testid="language-switcher-trigger"]') as HTMLButtonElement

    trigger.click()
    await nextTick()
    ;(el.querySelector('[data-testid="language-option-zh-Hans"]') as HTMLButtonElement).click()
    await vi.waitFor(() => expect(store.locale).toBe('zh-Hans'))
    await nextTick()

    await vi.waitFor(() => expect(trigger.textContent).toContain('中文'))
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })
})
