// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { CronJob } from '@/types/cron'
import CronView from './CronView.vue'

const testState = vi.hoisted(() => ({
  jobs: [] as CronJob[],
  filteredJobs: null as CronJob[] | null,
  hasLoaded: false,
  loading: false,
  error: null as string | null,
  openPanel: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

vi.mock('@/composables/cron/useCronJobs', async () => {
  const { computed, ref } = await import('vue')
  return { useCronJobs: () => {
    const jobs = ref(testState.jobs)
    return {
      jobs,
      hasLoaded: ref(testState.hasLoaded),
      loading: ref(testState.loading),
      error: ref(testState.error),
      searchText: ref(''),
      viewMode: ref<'cards' | 'table'>('cards'),
      runningJobIds: ref(new Set<string>()),
      sortCol: ref('next_run'),
      sortAsc: ref(true),
      now: ref(Date.now()),
      enabledCount: computed(() => jobs.value.filter(job => job.enabled).length),
      pausedCount: computed(() => jobs.value.filter(job => !job.enabled).length),
      reminderCount: ref(0),
      agentTaskCount: ref(0),
      nextCountdown: ref('18 min'),
      nextRunHint: ref('Daily summary'),
      last24h: ref({ runs: 7, ok: 7, err: 0 }),
      upcomingHorizon: ref([]),
      filteredSortedJobs: computed(() => testState.filteredJobs ?? jobs.value),
      loadData: vi.fn(async () => null),
      onSort: vi.fn(),
      toggleJob: vi.fn(),
      runJob: vi.fn(),
      removeJob: vi.fn(),
      isJobRunning: vi.fn(() => false),
    }
  } }
})

vi.mock('@/composables/cron/useCronRuns', async () => {
  const { ref } = await import('vue')
  return { useCronRuns: () => ({
    runs: ref([]),
    runsLoading: ref(false),
    loadRuns: vi.fn(async () => null),
  }) }
})

vi.mock('@/composables/cron/useCronForm', async () => {
  const { ref } = await import('vue')
  return { useCronForm: () => ({
    form: { cron: '' },
    panelOpen: ref(false),
    editingJob: ref(null),
    cronExplainHuman: ref(''),
    cronExplainValid: ref(false),
    cronExplainInvalid: ref(false),
    cronExplainUpcoming: ref([]),
    jobModeHint: ref(''),
    sessionTargetHint: ref(''),
    showTargetSessionRow: ref(false),
    targetSessionLabel: ref(''),
    targetSessionHint: ref(''),
    messageLabel: ref(''),
    projectWorkspaces: ref([]),
    projectWorkspacesLoaded: ref(true),
    projectWorkspacesLoading: ref(false),
    openPanel: testState.openPanel,
    closePanel: vi.fn(),
    saveJob: vi.fn(),
    renderCronExplain: vi.fn(),
    applyPreset: vi.fn(),
    onPayloadKindChange: vi.fn(),
    onSessionTargetChange: vi.fn(),
    loadProjectWorkspaces: vi.fn(async () => []),
  }) }
})

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast: vi.fn() }),
}))

vi.mock('@/components/Icon.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({
    name: 'IconStub',
    setup: () => () => h('span', { 'data-testid': 'icon' }),
  }) }
})
vi.mock('@/components/LoadingSpinner.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'LoadingSpinnerStub', setup: () => () => h('div', { 'data-testid': 'loading-spinner' }) }) }
})
vi.mock('@/components/ErrorState.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'ErrorStateStub', setup: () => () => h('div', { 'data-testid': 'error-state' }) }) }
})
vi.mock('@/components/cron/CronJobList.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'CronJobListStub', setup: () => () => h('div', { 'data-testid': 'cron-job-list' }) }) }
})
vi.mock('@/components/cron/CronDeleteDialog.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'CronDeleteDialogStub', setup: () => () => h('div', { 'data-testid': 'delete-dialog' }) }) }
})
vi.mock('@/components/cron/CronJobPanel.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'CronJobPanelStub', setup: () => () => h('div', { 'data-testid': 'job-panel' }) }) }
})
vi.mock('@/components/cron/CronRunHistory.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { default: defineComponent({ name: 'CronRunHistoryStub', setup: () => () => h('div', { 'data-testid': 'run-history' }) }) }
})

const apps: Array<ReturnType<typeof createApp>> = []

function mountCronView(): HTMLElement {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(CronView)
  apps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  testState.jobs = []
  testState.filteredJobs = null
  testState.hasLoaded = false
  testState.loading = false
  testState.error = null
  testState.openPanel.mockReset()
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('CronView automation hero states', () => {
  it('shows a neutral loading state before the first list response', () => {
    const host = mountCronView()

    expect(host.querySelector('[data-testid="loading-spinner"]')).not.toBeNull()
    expect(host.querySelector('.automation-launch')).toBeNull()
    expect(host.querySelector('[data-testid="cron-job-list"]')).toBeNull()
  })

  it('keeps the full animated create prompt for a successfully loaded empty list', async () => {
    testState.hasLoaded = true
    const host = mountCronView()
    const launch = host.querySelector<HTMLElement>('.automation-launch')

    expect(launch).not.toBeNull()
    expect(launch?.classList.contains('automation-launch--compact')).toBe(false)
    expect(launch?.textContent).toContain('Start your first automation')
    expect(launch?.querySelector('.automation-launch__status-copy')).toBeNull()
    expect(host.querySelector('[data-testid="cron-job-list"]')).toBeNull()

    launch?.querySelector<HTMLButtonElement>('.automation-launch__button')?.click()
    await nextTick()
    expect(testState.openPanel).toHaveBeenCalledWith(null)
  })

  it('reuses the clock in a compact status rail when jobs exist, even with no search matches', () => {
    testState.hasLoaded = true
    testState.jobs = [
      { id: 'daily', name: 'Daily summary', enabled: true },
      { id: 'review', name: 'Weekly review', enabled: false },
    ]
    testState.filteredJobs = []
    const host = mountCronView()
    const launch = host.querySelector<HTMLElement>('.automation-launch')

    expect(launch?.classList.contains('automation-launch--compact')).toBe(true)
    expect(launch?.querySelector('.automation-launch__clock')).not.toBeNull()
    expect(launch?.textContent).toContain('Automations are running')
    expect(launch?.textContent).toContain('1 of 2 jobs enabled')
    expect(launch?.textContent).toContain('18 min')
    expect(launch?.textContent).toContain('1 / 2')
    expect(launch?.querySelector('.automation-launch__button')).toBeNull()
    expect(host.querySelector('[data-testid="cron-job-list"]')).not.toBeNull()
  })

  it('prioritizes the load error over both automation states', () => {
    testState.error = 'cron list failed'
    const host = mountCronView()

    expect(host.querySelector('[data-testid="error-state"]')).not.toBeNull()
    expect(host.querySelector('[data-testid="loading-spinner"]')).toBeNull()
    expect(host.querySelector('.automation-launch')).toBeNull()
    expect(host.querySelector('[data-testid="cron-job-list"]')).toBeNull()
  })
})
