import { ref, watch, type Ref } from 'vue'
import type { CronRun } from '@/types/cron'
import type { CronScheduler } from '@/modules/cronScheduler'

export function useCronRuns(scheduler: CronScheduler, selectedId: Ref<string | null>) {
  const runs = ref<CronRun[]>([])
  const runsLoading = ref(false)
  let loadGeneration = 0

  async function loadRuns(jobId: string) {
    if (selectedId.value !== jobId) return
    const generation = ++loadGeneration
    runsLoading.value = true
    try {
      const data = await scheduler.listRuns(jobId, 10)
      if (generation !== loadGeneration || selectedId.value !== jobId) return
      runs.value = [...data]
    } catch {
      if (generation !== loadGeneration || selectedId.value !== jobId) return
      runs.value = []
    } finally {
      if (generation === loadGeneration) runsLoading.value = false
    }
  }

  watch(selectedId, (id) => {
    if (id) void loadRuns(id)
    else {
      loadGeneration += 1
      runs.value = []
      runsLoading.value = false
    }
  })

  return { runs, runsLoading, loadRuns }
}
