// @vitest-environment happy-dom
import { createApp, nextTick, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { DesktopUpdateState } from '@/platform'
const mocks = vi.hoisted(() => ({ controller: null as unknown }))
vi.mock('@/composables/useDesktopUpdate', async importOriginal => ({
  ...await importOriginal<typeof import('@/composables/useDesktopUpdate')>(),
  useDesktopUpdate: () => mocks.controller,
}))
import SidebarDesktopUpdate from './SidebarDesktopUpdate.vue'

let app: App | undefined
const state = ref<DesktopUpdateState>({ status: 'available', currentVersion: '1.0.0', latestVersion: '2.0.0',
  progress: null, checkedAt: null, error: null, errorCode: null, snoozedUntil: null, canCheck: true,
  canNativeInstall: true, installMode: 'native', releaseUrl: null, source: null, fallbackUsed: false })
const visible = ref(true)
const init = vi.fn()
const download = vi.fn()
const relaunch = vi.fn()
async function mountEntry() {
  const host = document.createElement('div'); document.body.appendChild(host)
  const open = vi.fn()
  app = createApp(SidebarDesktopUpdate, { onOpen: open }); app.use(i18n).mount(host)
  await nextTick()
  return { host, open }
}
beforeEach(() => {
  i18n.global.locale.value = 'en'
  visible.value = true
  state.value = { ...state.value, status: 'available', installMode: 'native', progress: null, error: null, errorCode: null }
  init.mockClear(); download.mockClear(); relaunch.mockClear()
  mocks.controller = { state, visible, actionBusy: ref(false), latestVersion: ref('2.0.0'),
    localizedError: ref('Could not download update'), init, download, relaunch }
})
afterEach(() => { app?.unmount(); document.body.innerHTML = '' })
describe('sidebar desktop update entry', () => {
  it('opens the existing settings flow without starting download or install', async () => {
    const { host, open } = await mountEntry()
    const button = host.querySelector('button')!
    expect(button.getAttribute('aria-label')).toContain('2.0.0')
    button.click()
    expect(open).toHaveBeenCalledOnce()
    expect(init).toHaveBeenCalledOnce()
    expect(download).not.toHaveBeenCalled(); expect(relaunch).not.toHaveBeenCalled()
  })
  it('follows the controller visibility for web, unsupported and snoozed states', async () => {
    visible.value = false
    const { host } = await mountEntry()
    expect(host.querySelector('button')).toBeNull()
    visible.value = true; await nextTick()
    expect(host.querySelector('button')).not.toBeNull()
    visible.value = false; await nextTick()
    expect(host.querySelector('button')).toBeNull()
  })
  it.each(['native', 'manual'] as const)('shows one readable ready row for %s without installing', async installMode => {
    state.value = { ...state.value, status: 'downloaded', installMode, canInstall: true }
    const { host, open } = await mountEntry()
    const button = host.querySelector('button')!
    expect(button.textContent).toContain('Update ready')
    expect(button.textContent).toContain('2.0.0')
    expect(button.querySelectorAll('svg')).toHaveLength(2)
    button.click()
    expect(open).toHaveBeenCalledOnce()
    expect(relaunch).not.toHaveBeenCalled()
  })
  it('keeps legacy manual updates actionable without claiming installation readiness', async () => {
    state.value = { ...state.value, status: 'downloaded', installMode: 'manual', canInstall: false }
    const { host } = await mountEntry()
    expect(host.querySelector('button')?.textContent).toContain('Update available')
    expect(host.querySelector('button')?.textContent).not.toContain('Update ready')
  })
  it.each([
    ['en', 'Update ready'], ['zh-Hans', '更新已就绪'], ['de', 'Update bereit'],
    ['es', 'Actualización lista'], ['fr', 'Mise à jour prête'], ['ja', '更新準備完了'],
  ] as const)('renders %s readiness and retains a complete accessible version', async (locale, expected) => {
    state.value = { ...state.value, status: 'downloaded', canInstall: true }
    const { host } = await mountEntry()
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale; await nextTick()
    expect(host.querySelector('button')?.textContent).toContain(expected)
    expect(host.querySelector('button')?.getAttribute('aria-label')).toContain('2.0.0')
  })
})
