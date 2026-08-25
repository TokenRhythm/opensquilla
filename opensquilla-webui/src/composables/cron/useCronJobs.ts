import { computed, onActivated, onDeactivated, onUnmounted, ref } from 'vue'
import i18n from '@/i18n'
import { useRpcStore } from '@/stores/rpc'
import { useRequest } from '@/composables/useRequest'
import { useToasts } from '@/composables/useToasts'
import type { CronJob } from '@/types/cron'
import { humanCountdown, humanTime } from '@/utils/cron/time'
import { wasCronFinishNotified } from '@/utils/cron/notifications'

interface CronListResponse {
  jobs?: CronJob[]
}

export function useCronJobs() {
  const rpc = useRpcStore()
  const { pushToast } = useToasts()
  const t = i18n.global.t
  const searchText = ref('')
  const viewMode = ref<'cards' | 'table'>('cards')
  const runningJobIds = ref<Set<string>>(new Set())
  const sortCol = ref('next_run')
  const sortAsc = ref(true)
  const now = ref(Date.now())

  const { data: cronData, loading, error, refresh } = useRequest<CronListResponse | CronJob[]>(
    'cron.list',
    undefined,
    { errorLabel: t('cronSkills.jobs.errLoad') },
  )

  const jobs = computed<CronJob[]>(() => {
    const d = cronData.value
    if (!d) return []
    return Array.isArray(d) ? d : (d.jobs || [])
  })
  const hasLoaded = computed(() => cronData.value !== null)

  let tickInterval: ReturnType<typeof setInterval> | null = null
  let reloadTimer: ReturnType<typeof setTimeout> | null = null
  let unsubRunFinished: (() => void) | null = null

  const enabledCount = computed(() => jobs.value.filter(j => j.enabled).length)
  const pausedCount = computed(() => jobs.value.length - enabledCount.value)
  const reminderCount = computed(() => jobs.value.filter(j => (j.payloadKind || j.payload_kind) === 'reminder').length)
  const agentTaskCount = computed(() => jobs.value.filter(j => (j.payloadKind || j.payload_kind) === 'agent_turn').length)

  const upcomingJobs = computed(() => jobs.value
    .filter(j => isUpcomingRun(j, now.value))
    .map(j => ({ job: j, ts: new Date(j.next_run!).getTime() }))
    .sort((a, b) => a.ts - b.ts))

  const nextJob = computed(() => upcomingJobs.value[0] || null)
  const nextCountdown = computed(() => nextJob.value ? humanCountdown(new Date(nextJob.value.ts), now.value) : '—')
  const nextRunHint = computed(() => nextJob.value
    ? `${nextJob.value.job.name || nextJob.value.job.id} · ${humanTime(new Date(nextJob.value.ts))}`
    : t('cronSkills.jobs.noUpcomingRuns'))

  const last24h = computed(() => jobs.value.reduce((acc, job) => {
    const ts = job.last_run ? new Date(job.last_run) : null
    if (ts && !isNaN(ts.getTime()) && now.value - ts.getTime() < 24 * 3600 * 1000) {
      acc.runs += 1
      const lastStatus = job.lastStatus || job.last_status
      if (lastStatus === 'ok' || lastStatus === 'success') acc.ok += 1
      if (lastStatus === 'error' || lastStatus === 'fail') acc.err += 1
    }
    return acc
  }, { runs: 0, ok: 0, err: 0 }))

  const upcomingHorizon = computed(() => jobs.value
    .filter(j => isUpcomingRun(j, now.value))
    .map(j => ({ job: j, ts: new Date(j.next_run!).getTime() }))
    .filter(o => o.ts > now.value && (o.ts - now.value) < 12 * 3600 * 1000)
    .sort((a, b) => a.ts - b.ts))

  const filteredSortedJobs = computed(() => {
    const st = searchText.value.toLowerCase()
    const filtered = jobs.value.filter(j => {
      if (!st) return true
      return (j.name || '').toLowerCase().includes(st) ||
        (j.message || j.prompt || '').toLowerCase().includes(st) ||
        (j.payloadKind || '').toLowerCase().includes(st) ||
        String(j.sessionTarget || j.session_target || '').toLowerCase().includes(st) ||
        (j.expression || j.schedule || '').toLowerCase().includes(st)
    })
    return [...filtered].sort((a, b) => {
      let va: unknown = a[sortCol.value as keyof CronJob] ?? ''
      let vb: unknown = b[sortCol.value as keyof CronJob] ?? ''
      if (sortCol.value === 'next_run' || sortCol.value === 'last_run') {
        va = va ? new Date(va as string).getTime() : (sortAsc.value ? Infinity : -Infinity)
        vb = vb ? new Date(vb as string).getTime() : (sortAsc.value ? Infinity : -Infinity)
      } else {
        va = String(va).toLowerCase()
        vb = String(vb).toLowerCase()
      }
      const cmp = (va as number | string) < (vb as number | string) ? -1 : (va as number | string) > (vb as number | string) ? 1 : 0
      return sortAsc.value ? cmp : -cmp
    })
  })

  const loadData = refresh

  function scheduleReload() {
    void refresh()
    if (reloadTimer) clearTimeout(reloadTimer)
    reloadTimer = setTimeout(() => { void refresh() }, 750)
  }

  async function recoverAfterConnectionRecycle(): Promise<void> {
    try {
      await rpc.waitForConnection(10_000)
      await refresh()
      pushToast(t('cronSkills.jobs.toastConnectionRecovered'), { tone: 'info' })
    } catch {
      pushToast(t('cronSkills.jobs.toastConnectionRetry'), { tone: 'warn' })
    }
  }

  function onRunFinished() {
    scheduleReload()
  }

  function onSort(col: string) {
    if (sortCol.value === col) {
      sortAsc.value = !sortAsc.value
    } else {
      sortCol.value = col
      sortAsc.value = true
    }
  }

  async function toggleJob(job: CronJob) {
    try {
      await rpc.call('cron.update', { id: job.id, enabled: !job.enabled })
      pushToast(job.enabled ? t('cronSkills.jobs.toastPaused') : t('cronSkills.jobs.toastResumed'), { tone: 'ok' })
      void refresh()
    } catch (err) {
      pushToast(t('cronSkills.jobs.toastUpdateFailed', { error: err instanceof Error ? err.message : String(err) }), { tone: 'danger' })
    }
  }

  function isJobRunning(id: string): boolean {
    return runningJobIds.value.has(id)
  }

  async function runJob(id: string) {
    runningJobIds.value = new Set(runningJobIds.value).add(id)
    try {
      const res = await rpc.call<{ runId?: string; reply?: string; error?: string }>('cron.run', { id })
      if (!res?.runId || !wasCronFinishNotified(res.runId)) {
        if (res?.error) pushToast(t('cronSkills.jobs.toastRunFailed', { error: res.error }), { tone: 'danger' })
        else pushToast(res?.reply ? t('cronSkills.jobs.toastRunComplete', { reply: res.reply.substring(0, 120) }) : t('cronSkills.jobs.toastTriggered'), { tone: 'ok' })
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      if (isConnectionRecycleError(message)) {
        await recoverAfterConnectionRecycle()
      } else {
        pushToast(t('cronSkills.jobs.toastRunFailed', { error: message }), { tone: 'danger' })
      }
    } finally {
      const next = new Set(runningJobIds.value)
      next.delete(id)
      runningJobIds.value = next
    }
  }

  async function removeJob(id: string) {
    await rpc.call('cron.remove', { id })
    pushToast(t('cronSkills.jobs.toastDeleted'), { tone: 'ok' })
    void refresh()
  }

  // CronView is kept-alive (route meta.keepAlive), so the clock tick, the cron
  // subscription, and the run-finished listener are bound on activation and
  // released on deactivation — they must not keep firing while the view is
  // cached off-screen. onActivated also runs on first display; loadData() runs
  // the silent background refresh on every (re)entry so a revisit is never
  // stale (the initial loading skeleton is driven by useRequest's own onMounted
  // execute(), so this silent refresh never flashes a spinner). onUnmounted is a
  // final safety net for the rare case the KeepAlive cache evicts this instance.
  function teardownLive() {
    if (tickInterval) { clearInterval(tickInterval); tickInterval = null }
    if (reloadTimer) { clearTimeout(reloadTimer); reloadTimer = null }
    if (unsubRunFinished) { unsubRunFinished(); unsubRunFinished = null }
  }

  onActivated(() => {
    void loadData()
    tickInterval = setInterval(() => { now.value = Date.now() }, 1000)
    unsubRunFinished = rpc.on('cron.run.finished', onRunFinished)
  })

  onDeactivated(teardownLive)
  onUnmounted(teardownLive)

  return {
    jobs,
    hasLoaded,
    loading,
    error,
    searchText,
    viewMode,
    runningJobIds,
    sortCol,
    sortAsc,
    now,
    enabledCount,
    pausedCount,
    reminderCount,
    agentTaskCount,
    nextCountdown,
    nextRunHint,
    last24h,
    upcomingHorizon,
    filteredSortedJobs,
    loadData,
    onSort,
    toggleJob,
    runJob,
    removeJob,
    isJobRunning,
  }
}

export function isUpcomingRun(job: CronJob, now = Date.now()): boolean {
  if (!job.enabled || !job.next_run || job.status === 'running') return false
  const ts = new Date(job.next_run)
  return !isNaN(ts.getTime()) && ts.getTime() > now
}

export function nextRunText(job: CronJob, now = Date.now()): string {
  if (!job.enabled) return '—'
  if (job.status === 'running') return i18n.global.t('cronSkills.jobs.running')
  if (!job.next_run) return '—'
  const ts = new Date(job.next_run)
  if (isNaN(ts.getTime())) return '—'
  if (ts.getTime() <= now) return i18n.global.t('cronSkills.jobs.awaitingUpdate')
  return humanCountdown(ts, now)
}

export function nextRunAbs(job: CronJob, now = Date.now()): string {
  if (!job.enabled || job.status === 'running' || !job.next_run) return ''
  const ts = new Date(job.next_run)
  if (isNaN(ts.getTime()) || ts.getTime() <= now) return ''
  return humanTime(ts)
}

export function dotClass(job: CronJob): string {
  if (!job.enabled) return 'is-off'
  const lastStatus = job.lastStatus || job.last_status || (job.last_run ? 'ok' : null)
  if (lastStatus === 'error' || lastStatus === 'fail') return 'is-error'
  return 'is-on'
}

export function jobKindLabel(job: CronJob): string {
  const kind = job.payloadKind || job.payload_kind
  if (kind === 'reminder') return i18n.global.t('cronSkills.jobs.kindReminder')
  if (kind === 'system_event') return i18n.global.t('cronSkills.jobs.kindSystemEvent')
  return i18n.global.t('cronSkills.jobs.kindAgentTask')
}

export function jobKindClass(job: CronJob): string {
  const kind = job.payloadKind || job.payload_kind
  return kind === 'reminder' ? 'is-reminder' : 'is-agent'
}

export function isImminent(job: CronJob, now = Date.now()): boolean {
  if (!job.next_run) return false
  const left = new Date(job.next_run).getTime() - now
  return left > 0 && left < 60_000
}

export function isConnectionRecycleError(message: string): boolean {
  const normalized = message.toLowerCase()
  return normalized.includes('connection recycled') ||
    normalized.includes('connection closed') ||
    normalized.includes('not connected')
}

export function isJobFailed(job: CronJob): boolean {
  const status = job.lastStatus || job.last_status
  return status === 'error' || status === 'fail'
}
