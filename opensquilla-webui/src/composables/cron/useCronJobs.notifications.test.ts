// @vitest-environment happy-dom
import { createApp, defineComponent, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CronRunFinished, CronRunOutcome, CronScheduler } from '@/modules/cronScheduler'
import { notifyCronRunFinished } from '@/utils/cron/notifications'
import i18n from '@/i18n'
import { useCronJobs } from './useCronJobs'

const { pushToast } = vi.hoisted(() => ({ pushToast: vi.fn() }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))
const apps: ReturnType<typeof createApp>[] = []
beforeEach(() => { vi.useFakeTimers(); pushToast.mockReset() })
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); vi.useRealTimers() })

function setup(runId: string, reminder = false) {
  let resolve!: (value: CronRunOutcome) => void
  const outcome = new Promise<CronRunOutcome>(done => { resolve = done })
  const openJob = vi.fn(async () => true)
  const scheduler: CronScheduler = {
    listJobs: vi.fn(async () => [{ id: 'daily', name: 'Daily', payloadKind: reminder ? 'reminder' : 'agent_turn' }]),
    runNow: vi.fn(async () => outcome),
    saveJob: vi.fn(), setEnabled: vi.fn(), remove: vi.fn(), listRuns: vi.fn(),
    subscribe: vi.fn(() => ({ close: vi.fn() })),
  }
  let state!: ReturnType<typeof useCronJobs>
  const app = createApp(defineComponent({ setup() {
    state = useCronJobs(scheduler, { openJob })
    return () => null
  } }))
  app.mount(document.createElement('div'))
  apps.push(app)
  const finishEvent = (extra: Partial<CronRunFinished> = {}) => notifyCronRunFinished({
    jobId: 'daily', jobName: 'Daily', runId, success: false, ...extra,
  }, { t: i18n.global.t, pushToast, openJob })
  return { state, scheduler, resolve, finishEvent, openJob }
}

describe('manual cron RPC and global event coordination', () => {
  it.each(['rpc-first', 'event-first'] as const)('notifies one failure when %s, including duplicate events and long delays', async order => {
    const runId = `business-${order}`
    const { state, resolve, finishEvent } = setup(runId)
    await state.loadData()
    const run = state.runJob('daily')
    if (order === 'event-first') finishEvent()
    resolve({ runId, success: false, error: 'execution failed' })
    await run
    await vi.advanceTimersByTimeAsync(60_000)
    finishEvent()
    finishEvent()
    expect(pushToast).toHaveBeenCalledOnce()
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ tone: 'danger', action: expect.any(Object) }))
  })

  it('does not treat explicit failure without error text as successful', async () => {
    const { state, resolve } = setup('failure-no-error')
    await state.loadData()
    const run = state.runJob('daily')
    resolve({ runId: 'failure-no-error', success: false })
    await run
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ tone: 'danger' }))
  })

  it('leaves ordinary completion visible through refreshed data without a global toast', async () => {
    const { state, scheduler, resolve, finishEvent } = setup('silent-success')
    await state.loadData()
    const run = state.runJob('daily')
    resolve({ runId: 'silent-success', success: true, reply: 'completed' })
    await run
    finishEvent({ success: true })
    expect(pushToast).not.toHaveBeenCalled()
    expect(scheduler.listJobs).toHaveBeenCalledTimes(2)
    expect(state.isJobRunning('daily')).toBe(false)
  })

  it.each(['rpc-first', 'event-first'] as const)('keeps one empty reminder when %s', async order => {
    const runId = `reminder-${order}`
    const { state, resolve, finishEvent, openJob } = setup(runId, true)
    await state.loadData()
    const run = state.runJob('daily')
    if (order === 'event-first') finishEvent({ success: true, payloadKind: 'reminder' })
    resolve({ runId, success: true, reply: '' })
    await run
    finishEvent({ success: true, payloadKind: 'reminder' })
    expect(pushToast).toHaveBeenCalledOnce()
    expect(pushToast).toHaveBeenCalledWith(i18n.global.t('cronSkills.jobs.toastReminderDue', { name: 'Daily' }), expect.objectContaining({ tone: 'info' }))
    await pushToast.mock.calls[0][1].action.onClick()
    expect(openJob).toHaveBeenCalledWith('daily')
  })

  it('waits for the explicit reminder event so an earlier RPC cannot lose its session action', async () => {
    const { state, resolve } = setup('session-reminder-rpc-first', true)
    const openSession = vi.fn(async () => true)
    await state.loadData()
    const run = state.runJob('daily')
    resolve({ runId: 'session-reminder-rpc-first', success: true, reply: 'Time for a break' })
    await run
    expect(pushToast).not.toHaveBeenCalled()
    const event = { runId: 'session-reminder-rpc-first', success: true, payloadKind: 'reminder', sessionKey: 'reminder-session', summary: 'Time for a break' }
    notifyCronRunFinished(event, { t: i18n.global.t, pushToast, openSession })
    notifyCronRunFinished(event, { t: i18n.global.t, pushToast, openSession })
    expect(pushToast).toHaveBeenCalledOnce()
    await pushToast.mock.calls[0][1].action.onClick()
    expect(openSession).toHaveBeenCalledWith('reminder-session')
  })

  it('keeps a rejected manual request actionable without inventing run identity', async () => {
    const { state, resolve, openJob } = setup('not-executed')
    const run = state.runJob('daily')
    resolve({ success: false, reason: 'already_running', status: 'blocked' })
    await run
    expect(pushToast).toHaveBeenCalledWith(i18n.global.t('cronSkills.jobs.toastRunFailed', { error: 'already_running' }), expect.objectContaining({ tone: 'danger' }))
    await pushToast.mock.calls[0][1].action.onClick()
    expect(openJob).toHaveBeenCalledWith('daily')
  })

  it('does not launch a second manual execution while the first is pending', async () => {
    const { state, scheduler, resolve } = setup('one-click')
    const run = state.runJob('daily')
    await nextTick()
    await state.runJob('daily')
    expect(scheduler.runNow).toHaveBeenCalledOnce()
    resolve({ success: true })
    await run
  })
})
