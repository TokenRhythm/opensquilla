// @vitest-environment happy-dom

import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CronJobList from './CronJobList.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

async function mountList(viewMode: 'cards' | 'table') {
  const onSelect = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    setup: () => () => h(CronJobList, {
      jobs: [{
        id: 'job-1',
        name: 'Daily digest',
        enabled: true,
        expression: '0 9 * * *',
      }],
      totalJobs: 1,
      searchText: '',
      viewMode,
      selectedId: null,
      sortCol: 'name',
      sortAsc: true,
      now: Date.now(),
      runningJobIds: new Set<string>(),
      bulkMode: false,
      selectedJobIds: new Set<string>(),
      projectWorkspaces: [],
      projectWorkspacesLoaded: true,
      onSelect,
    }),
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    missingWarn: false,
    fallbackWarn: false,
    messages: { en: {} },
  }))
  app.mount(host)
  apps.push(app)
  await nextTick()
  return { host, onSelect }
}

describe('CronJobList run history access', () => {
  it.each([
    ['cards', '.cron-card__actions'],
    ['table', '.cron-table__actions-content'],
  ] as const)('exposes a dedicated history action in %s view', async (viewMode, scope) => {
    const { host, onSelect } = await mountList(viewMode)
    const button = host.querySelector<HTMLButtonElement>(
      `${scope} button[title="cronSkills.list.showRunHistory"]`,
    )

    expect(button).not.toBeNull()
    button?.click()
    await nextTick()
    expect(onSelect).toHaveBeenCalledWith('job-1')
  })
})
