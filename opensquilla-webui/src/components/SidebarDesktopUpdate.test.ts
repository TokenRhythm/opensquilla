// @vitest-environment happy-dom
import { createApp, nextTick, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import type { DesktopUpdateState } from '@/platform'
const mocks = vi.hoisted(() => ({ controller: null as unknown }))
vi.mock('@/composables/useDesktopUpdate', () => ({ useDesktopUpdate: () => mocks.controller }))
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
  it('uses real download percentage and distinguishes indeterminate progress', async () => {
    state.value = { ...state.value, status: 'downloading', progress: 37 }
    const { host } = await mountEntry()
    expect(host.querySelector('button')?.title).toContain('37')
    state.value = { ...state.value, progress: null }; await nextTick()
    expect(host.querySelector('button')?.title).not.toContain('37')
    expect(host.querySelector('button')?.title).not.toContain('null')
  })
  it.each(['native', 'manual'] as const)('uses shared presentation for %s downloaded state', async installMode => {
    state.value = { ...state.value, status: 'downloaded', installMode }
    const { host } = await mountEntry()
    expect(host.querySelector('button')?.title).toBe(i18n.global.t(installMode === 'manual'
      ? 'updates.desktop.indicatorInstallerReady' : 'updates.desktop.indicatorDownloaded'))
    expect(host.querySelector('button')?.dataset.state).toBe('info')
  })
  it('keeps a manual installer retry error visible as an error', async () => {
    state.value = { ...state.value, status: 'downloaded', installMode: 'manual', errorCode: 'install_failed' }
    const { host } = await mountEntry()
    expect(host.querySelector('button')?.dataset.state).toBe('danger')
    expect(host.querySelector('button')?.title).toBe(i18n.global.t('updates.desktop.indicatorError'))
  })
  it('uses localized presentation for applying and failures', async () => {
    state.value = { ...state.value, status: 'applying', installMode: 'manual' }
    const { host } = await mountEntry()
    expect(host.querySelector('button')?.title).toBe(i18n.global.t('updates.desktop.manualApplyingStatus'))
    state.value = { ...state.value, status: 'error' }; await nextTick()
    expect(host.querySelector('button')?.dataset.state).toBe('danger')
    i18n.global.locale.value = 'zh-Hans'; await nextTick()
    expect(host.querySelector('button')?.title).toBe(i18n.global.t('updates.desktop.indicatorError'))
  })
})
