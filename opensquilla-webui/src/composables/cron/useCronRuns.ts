import { onActivated, onDeactivated, onUnmounted, ref, watch, type Ref } from 'vue'
import type { CronRun } from '@/types/cron'
import type { CronScheduler } from '@/modules/cronScheduler'
import { CronReadUnavailableError } from '@/modules/cronScheduler'

export function useCronRuns(scheduler: CronScheduler, selectedId: Ref<string | null>) {
  const runs = ref<CronRun[]>([])
  const runsLoading = ref(false)
  const waitingForCapacity = ref(false)
  const error = ref<string | null>(null)
  let loadGeneration = 0
  let active = true
  let readController: AbortController | null = null
  let readWork: Promise<void> | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let retryDelay = 1000
  let reloadNeeded = false

  function loadRuns(jobId: string): Promise<void> {
    if (!active || selectedId.value !== jobId) return Promise.resolve()
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
    if (readWork) { reloadNeeded = true; return readWork }
    const generation = ++loadGeneration
    const controller = new AbortController()
    readController = controller
    const current = () => active && generation === loadGeneration && selectedId.value === jobId && !controller.signal.aborted
    runsLoading.value = true
    error.value = null
    const work = Promise.resolve().then(async () => {
      if (!current()) return
      try {
        const data = await scheduler.listRuns(jobId, 10, { signal: controller.signal })
        if (!current()) return
        runs.value = [...data]
        waitingForCapacity.value = false
        retryDelay = 1000
      } catch (err) {
        if (!current()) return
        if (err instanceof CronReadUnavailableError) {
          waitingForCapacity.value = true
          const delay = Math.max(retryDelay, err.retryAfterMs)
          retryDelay = Math.min(retryDelay * 2, 10_000)
          retryTimer = setTimeout(() => { retryTimer = null; void loadRuns(jobId) }, delay)
        } else {
          waitingForCapacity.value = false
          error.value = err instanceof Error ? err.message : String(err)
        }
      } finally {
        if (current()) {
          readWork = null
          readController = null
          runsLoading.value = false
          const again = reloadNeeded && !waitingForCapacity.value && !error.value
          reloadNeeded = false
          if (again) void loadRuns(jobId)
        }
      }
    })
    readWork = work
    return work
  }

  function cancelRead() {
    loadGeneration += 1
    readController?.abort()
    readController = null
    readWork = null
    reloadNeeded = false
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
    runsLoading.value = false
    waitingForCapacity.value = false
    retryDelay = 1000
  }

  watch(selectedId, (id) => {
    cancelRead()
    runs.value = []
    error.value = null
    if (id) void loadRuns(id)
  }, { flush: 'sync' })

  onActivated(() => { active = true; if (selectedId.value) void loadRuns(selectedId.value) })
  onDeactivated(() => { active = false; cancelRead() })
  onUnmounted(() => { active = false; cancelRead() })

  return { runs, runsLoading, waitingForCapacity, error, loadRuns }
}
