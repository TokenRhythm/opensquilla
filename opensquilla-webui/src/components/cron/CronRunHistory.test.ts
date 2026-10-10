// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, h } from 'vue'
import i18n from '@/i18n'
import CronRunHistory from './CronRunHistory.vue'

const apps: Array<ReturnType<typeof createApp>> = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()) })

describe('Cron history capacity state', () => {
  it.each([false, true])('shows waiting without inventing empty history (cached=%s)', cached => {
    i18n.global.locale.value = 'en'
    const host = document.createElement('div')
    const app = createApp({ render: () => h(CronRunHistory, {
      job: { id: 'A' }, runs: cached ? [{ summary: 'Saved run result' }] : [],
      loading: false, waiting: true,
    }) })
    apps.push(app); app.use(i18n); app.mount(host)
    expect(host.querySelector('[role="status"]')?.textContent).toContain('Retrying automatically')
    expect(host.querySelector('[role="alert"]')).toBeNull()
    expect(host.textContent).not.toContain(i18n.global.t('cronSkills.runHistory.empty'))
    expect(host.textContent?.includes('Saved run result')).toBe(cached)
  })
})
