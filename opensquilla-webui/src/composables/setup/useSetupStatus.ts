import { onMounted, ref, type Ref } from 'vue'
import type { SetupStatusPort } from '@/modules/setupWorkflow'

export interface SetupStatusState<T extends object> {
  data: Ref<T | null>
  loading: Ref<boolean>
  error: Ref<string | null>
  execute(): Promise<void>
}

/** Reactive consumer state over the setup-status domain Interface. */
export function useSetupStatus<T extends object = Record<string, unknown>>(
  setup: SetupStatusPort,
): SetupStatusState<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function execute(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      data.value = await setup.status() as T
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason)
      throw reason
    } finally {
      loading.value = false
    }
  }

  onMounted(() => {
    void execute().catch(() => {
      // The error ref is the consumer-facing failure projection.
    })
  })

  return { data, loading, error, execute }
}
