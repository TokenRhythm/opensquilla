import { ref, watch, type Ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import type { CronRun } from '@/types/cron'

export function useCronRuns(selectedId: Ref<string | null>) {
  const rpc = useRpcStore()
  const runs = ref<CronRun[]>([])
  const runsLoading = ref(false)
  let loadGeneration = 0

  async function loadRuns(jobId: string) {
    if (selectedId.value !== jobId) return
    const generation = ++loadGeneration
    runsLoading.value = true
    try {
      const data = await rpc.call<{ runs?: CronRun[] } | CronRun[]>('cron.runs', { id: jobId, limit: 10 })
      if (generation !== loadGeneration || selectedId.value !== jobId) return
      runs.value = Array.isArray(data) ? data : (data.runs || [])
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
