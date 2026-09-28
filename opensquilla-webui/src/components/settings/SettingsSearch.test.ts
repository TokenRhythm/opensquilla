// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import zh from '@/locales/zh-Hans.json'
import SettingsSearch from './SettingsSearch.vue'

const apps: ReturnType<typeof createApp>[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})
function mountSearch(locale = 'en', isDesktop = false) {
  const select = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(SettingsSearch, { isDesktop, onSelect: select }) })
  app.use(createI18n({ legacy: false, locale, messages: { en, 'zh-Hans': zh } }))
  app.mount(host)
  apps.push(app)
  return { host, select, input: host.querySelector('input')! }
}
async function search(input: HTMLInputElement, value: string) {
  input.focus()
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}
describe('SettingsSearch', () => {
  it('finds a translated setting and supports keyboard selection without changing values', async () => {
    const { input, host, select } = mountSearch()
    await search(input, 'sidebar width')
    expect(host.querySelectorAll('.settings-search__result')).toHaveLength(1)
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    const result = host.querySelector<HTMLButtonElement>('button')!
    expect(document.activeElement).toBe(result)
    result.click()
    await nextTick()
    expect(select).toHaveBeenCalledExactlyOnceWith('interface', 'settings.appearance.sidebarWidthLabel')
    expect(input.value).toBe('')
    expect(host.querySelector('.settings-search__results')).toBeNull()
  })
  it('finds settings in the current Chinese locale', async () => {
    const { input, host, select } = mountSearch('zh-Hans')
    await search(input, zh.settings.appearance.themeLabel)
    expect(host.querySelector('.settings-search__result')?.textContent).toContain(zh.settings.rail.interface)
    host.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    expect(select).toHaveBeenCalledExactlyOnceWith('interface', 'settings.appearance.themeLabel')
  })

  it('keeps a result mounted while pointer focus moves from the input to its button', async () => {
    const { input, host, select } = mountSearch()
    // Chromium reports body as activeElement during focusout, before the
    // related target receives focus. Removing the result here loses its click.
    input.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    input.value = 'sidebar width'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    const result = host.querySelector<HTMLButtonElement>('.settings-search__result')!
    expect(document.activeElement).toBe(document.body)
    input.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: result }))
    await nextTick()
    expect(result.isConnected).toBe(true)
    result.click()
    expect(select).toHaveBeenCalledExactlyOnceWith('interface', 'settings.appearance.sidebarWidthLabel')
  })

  it.each([
    ['setup.memory.title', 'capabilities', en.setup.memory.title],
    ['settings.memoryOverview.autoCaptureLabel', 'advanced', en.settings.memoryOverview.autoCaptureLabel],
  ])('indexes %s under its current settings page', async (labelKey, section, label) => {
    const { input, host, select } = mountSearch()
    await search(input, label)
    expect(host.querySelectorAll('.settings-search__result')).toHaveLength(1)
    host.querySelector<HTMLButtonElement>('.settings-search__result')!.click()
    expect(select).toHaveBeenCalledExactlyOnceWith(section, labelKey)
  })

  it.each([false, true])('only lists controls available on this surface (desktop: %s)', async isDesktop => {
    const { input, host } = mountSearch('en', isDesktop)
    await search(input, en.setup.runtime.title)
    expect(host.querySelectorAll('.settings-search__result')).toHaveLength(isDesktop ? 1 : 0)
    await search(input, en.setup.connection.tokenLabel)
    expect(host.querySelectorAll('.settings-search__result')).toHaveLength(isDesktop ? 0 : 1)
  })
  it('clears a search with Escape before the parent dialog handles Escape', async () => {
    const { input, host } = mountSearch()
    await search(input, 'not-a-setting')
    expect(host.querySelector('[role="status"]')).not.toBeNull()
    const parentKeydown = vi.fn()
    host.addEventListener('keydown', parentKeydown)
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    input.dispatchEvent(event)
    await nextTick()
    expect(event.defaultPrevented).toBe(true)
    expect(parentKeydown).not.toHaveBeenCalled()
    expect(input.value).toBe('')
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(parentKeydown).toHaveBeenCalledOnce()
  })
})
