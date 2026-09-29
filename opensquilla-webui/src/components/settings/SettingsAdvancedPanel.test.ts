// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: App[] = []

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  localStorage.clear()
  vi.doUnmock('@/components/settings/MemoryDreamSettings.vue')
})

describe('SettingsAdvancedPanel data maintenance entry', () => {
  it('keeps memory controls in Advanced and maintenance last', async () => {
    localStorage.setItem('opensquilla.logs.runTrace', '1')
    vi.resetModules()
    vi.doMock('@/components/settings/MemoryDreamSettings.vue', () => ({
      default: { template: '<div data-testid="memory-dream-settings" />' },
    }))
    const { createApp, nextTick } = await import('vue')
    const i18n = (await import('@/i18n')).default
    i18n.global.locale.value = 'en'
    const Component = (await import('./SettingsAdvancedPanel.vue')).default
    const openDataMaintenance = vi.fn()
    const updateAutoCapture = vi.fn()
    const copyConfigPath = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(Component, {
      autoCapture: true,
      loaded: true,
      configPath: '/example/config.toml',
      onCopyConfigPath: copyConfigPath,
      onOpenDataMaintenance: openDataMaintenance,
      onUpdateAutoCapture: updateAutoCapture,
    })
    app.use(i18n)
    app.mount(el)
    mounted.push(app)
    await nextTick()

    const configFile = el.querySelector('[data-testid="advanced-config-file"]')!
    expect(configFile.querySelector('code')?.textContent).toBe('/example/config.toml')
    configFile.querySelector<HTMLButtonElement>('button')!.click()
    expect(copyConfigPath).toHaveBeenCalledOnce()

    const memoryGroup = el.querySelector<HTMLElement>('[data-testid="advanced-memory-group"]')!
    const capture = memoryGroup.querySelector<HTMLInputElement>('input[name="memory_auto_capture"]')!
    expect(memoryGroup.textContent).toContain('Memory')
    expect(memoryGroup.querySelector('[data-testid="memory-dream-settings"]')).toBeTruthy()
    expect(capture.checked).toBe(true)
    capture.checked = false
    capture.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(updateAutoCapture).toHaveBeenCalledWith(false)

    expect(el.textContent).not.toContain('Agent configuration')
    expect(el.querySelector('[name="labs_run_trace"]')).toBeNull()
    expect(el.textContent).not.toContain('Run-trace drawer in Logs')

    const approvalPoll = el.querySelector<HTMLInputElement>('input[name="labs_approval_poll"]')!
    expect(approvalPoll.checked).toBe(false)
    approvalPoll.checked = true
    approvalPoll.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(localStorage.getItem('opensquilla.chat.approvalPoll')).toBe('1')

    const rows = el.querySelectorAll('.control-row')
    const maintenance = el.querySelector<HTMLElement>('[data-testid="advanced-data-maintenance"]')!
    expect(rows.item(rows.length - 1)).toBe(maintenance)
    expect(maintenance.textContent).toContain('Data maintenance')
    expect(openDataMaintenance).not.toHaveBeenCalled()

    maintenance.querySelector<HTMLButtonElement>('button')!.click()
    await nextTick()
    expect(openDataMaintenance).toHaveBeenCalledTimes(1)
  })
})
