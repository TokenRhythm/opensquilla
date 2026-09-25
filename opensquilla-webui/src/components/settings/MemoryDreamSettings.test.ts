// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import { APP_SETTINGS_KEY, type AppSettings } from '@/modules/appSettings'
import MemoryDreamSettings from './MemoryDreamSettings.vue'

let app: App | undefined
afterEach(() => {
  app?.unmount()
  document.body.innerHTML = ''
})

async function mountSettings() {
  const readAll = vi.fn().mockResolvedValue({ memory: { dream: {
    enabled: false, auto_schedule: false, preview_mode: true, interval_h: 7,
  } } })
  const patchSafe = vi.fn().mockResolvedValue({ restartRequired: false })
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(MemoryDreamSettings)
  app.use(i18n)
  app.provide(APP_SETTINGS_KEY, { readAll, patchSafe } as unknown as AppSettings)
  app.mount(el)
  await nextTick()
  await nextTick()
  return { el, readAll, patchSafe, toggle: el.querySelector<HTMLInputElement>('input')! }
}

async function change(toggle: HTMLInputElement, checked: boolean) {
  toggle.checked = checked
  toggle.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
  await nextTick()
}

describe('independent Dream settings', () => {
  it('needs only settings and changes Dream without overriding preview or interval', async () => {
    const { el, readAll, patchSafe, toggle } = await mountSettings()
    expect(el.querySelectorAll('input')).toHaveLength(1)
    expect(toggle.disabled).toBe(false)
    expect(readAll).toHaveBeenCalledOnce()
    await change(toggle, true)
    expect(patchSafe).toHaveBeenCalledExactlyOnceWith([
      { path: 'memory.dream.enabled', value: true },
      { path: 'memory.dream.auto_schedule', value: true },
    ])
    expect(toggle.checked).toBe(true)
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('shows restart guidance when saved schedules cannot reconcile live', async () => {
    const { el, patchSafe, toggle } = await mountSettings()
    patchSafe.mockResolvedValue({ restartRequired: true })
    await change(toggle, true)
    expect(el.querySelector('[role="status"]')?.textContent).toContain(
      i18n.global.t('setup.memoryDream.restartRequired'),
    )
    expect(toggle.checked).toBe(true)
  })

  it('restores the saved state when an explicit save fails', async () => {
    const { patchSafe, toggle } = await mountSettings()
    patchSafe.mockRejectedValue(new Error('synthetic save failure'))
    await change(toggle, true)
    expect(toggle.checked).toBe(false)
    expect(toggle.disabled).toBe(false)
  })
})
