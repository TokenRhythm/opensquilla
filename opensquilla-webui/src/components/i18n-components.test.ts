// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { createApp, nextTick, type Component } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import i18n from '@/i18n'
import { useAppStore } from '@/stores/app'
import SettingsAppearancePanel from '@/components/settings/SettingsAppearancePanel.vue'
import SettingsLanguageControl from '@/components/settings/SettingsLanguageControl.vue'
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
  useToolDetailPreference().setMode('collapsed')
  localStorage.clear()
  document.documentElement.removeAttribute('lang')
})

describe('SettingsAppearancePanel — Tool details row', () => {
  it('offers only two defaults, starts collapsed, and persists the selection immediately', async () => {
    const { el, app } = await mount(SettingsAppearancePanel)
    const group = el.querySelector('[data-testid="settings-tool-details-group"]')
    const collapsed = el.querySelector('[data-testid="settings-tool-details-collapsed"]') as HTMLInputElement
    const expanded = el.querySelector('[data-testid="settings-tool-details-expanded"]') as HTMLInputElement

    expect(group?.querySelectorAll('input')).toHaveLength(2)
    expect(group?.textContent).toContain('Collapsed by default')
    expect(group?.textContent).toContain('Expanded by default')
    expect(collapsed.checked).toBe(true)
    expect(expanded.checked).toBe(false)

    expanded.checked = true
    expanded.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(collapsed.checked).toBe(false)
    expect(expanded.checked).toBe(true)
    expect(useToolDetailPreference().mode.value).toBe('expanded')
    expect(localStorage.getItem(TOOL_DETAIL_DISPLAY_STORAGE_KEY)).toBe('expanded')
    app.unmount()
  })
})

describe('SettingsLanguageControl — General language row', () => {
  it('renders a Language radiogroup with native English / 中文 labels', async () => {
    const { el } = await mount(SettingsLanguageControl)
    const group = el.querySelector('[data-testid="settings-language-group"]')
    expect(group).toBeTruthy()
    expect(el.querySelector('[data-testid="settings-language-en"]')).toBeTruthy()
    expect(el.querySelector('[data-testid="settings-language-zh-Hans"]')).toBeTruthy()
    expect(group!.textContent).toContain('English')
    expect(group!.textContent).toContain('中文')
  })

  it('switching the radio sets the locale, persists it, and reactively localizes the panel', async () => {
    const { el } = await mount(SettingsLanguageControl)
    const store = useAppStore()
    expect(el.querySelector('.control-row__label')!.textContent).toContain('Language')

    const zh = el.querySelector('[data-testid="settings-language-zh-Hans"]') as HTMLInputElement
    zh.checked = true
    zh.dispatchEvent(new Event('change', { bubbles: true }))
    // setLocale lazy-imports the locale chunk; wait on the outcome, not a tick.
    await vi.waitFor(() => expect(store.locale).toBe('zh-Hans'))
    await nextTick()

    expect(localStorage.getItem('opensquilla-locale')).toBe('zh-Hans')
    expect(document.documentElement.getAttribute('lang')).toBe('zh-Hans')
    // The General row re-renders in Chinese (reactive t()).
    await vi.waitFor(() =>
      expect(el.querySelector('.control-row__label')!.textContent).toContain('语言'))
  })
})
