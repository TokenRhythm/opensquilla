// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h } from 'vue'
import { createI18n } from 'vue-i18n'
import type { CronRun } from '@/types/cron'
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

function mountHistory(runs: CronRun[] = []) {
  const onRetry = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(CronRunHistory, { job: { id: 'job', name: 'Saved task' }, runs, loading: false, error: null, onRetry }) })
  apps.push(app)
  app.use(createI18n({ legacy: false, locale: 'en', missingWarn: false, fallbackWarn: false, messages: { en: {} } }))
  app.mount(host)
  return { host, onRetry }
}

describe('CronRunHistory request failures', () => {
  it('renders the actual failed execution identity and error for notification visibility', () => {
    const { host } = mountHistory([{ id: 'run-failed', status: 'error', error: 'Could not open workspace', summary: '' }])
    expect(host.querySelector('[data-cron-failed-run="run-failed"]')).not.toBeNull()
    expect(host.textContent).toContain('Could not open workspace')
  })
})
