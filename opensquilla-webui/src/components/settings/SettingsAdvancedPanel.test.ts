// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

const mounted: App[] = []

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
  localStorage.clear()
  vi.doUnmock('@/components/settings/MemoryLearningGroup.vue')
})

describe('SettingsAdvancedPanel data maintenance entry', () => {
  it('keeps maintenance low in Advanced and emits navigation only after activation', async () => {
    vi.resetModules()
    vi.doMock('@/components/settings/MemoryLearningGroup.vue', () => ({
      default: { template: '<div />' },
    }))
    const { createApp, nextTick } = await import('vue')
    const i18n = (await import('@/i18n')).default
    i18n.global.locale.value = 'en'
    const Component = (await import('./SettingsAdvancedPanel.vue')).default
    const openDataMaintenance = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(Component, { onOpenDataMaintenance: openDataMaintenance })
    app.use(i18n)
    app.mount(el)
    mounted.push(app)
    await nextTick()

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
