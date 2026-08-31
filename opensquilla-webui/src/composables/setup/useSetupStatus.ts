import { onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import type { SetupStatusPort } from '@/modules/setupWorkflow'

const cachedStatus = new WeakMap<SetupStatusPort, object>()
const pendingStatus = new WeakMap<SetupStatusPort, Promise<object>>()

export interface SetupStatusOptions {
  /** Prevent optional setup reads from overtaking critical session recovery. */
  allowed?: Readonly<Ref<boolean>>
}

export interface SetupStatusState<T extends object> {
  data: Ref<T | null>
  loading: Ref<boolean>
  error: Ref<string | null>
  execute(): Promise<void>
}

/** Reactive consumer state over the setup-status domain Interface. */
export function useSetupStatus<T extends object = Record<string, unknown>>(
  setup: SetupStatusPort,
  options: SetupStatusOptions = {},
): SetupStatusState<T> {
  const data = ref<T | null>(null) as Ref<T | null>
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function load(refresh: boolean): Promise<void> {
    const cached = cachedStatus.get(setup)
    if (!refresh && cached) {
      data.value = cached as T
      return
    }
    loading.value = true
    error.value = null
    try {
      let pending = pendingStatus.get(setup)
      if (!pending) {
        pending = setup.status().then((value) => {
          cachedStatus.set(setup, value)
          return value
        })
        pendingStatus.set(setup, pending)
        void pending.finally(() => {
          if (pendingStatus.get(setup) === pending) pendingStatus.delete(setup)
        }).catch(() => {
          // Each consumer projects the shared rejection into its own error ref.
        })
      }
      data.value = await pending as T
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : String(reason)
      throw reason
    } finally {
      loading.value = false
    }
  }

  function execute(): Promise<void> {
    return load(true)
  }

  function executeWhenAllowed(): void {
    if (options.allowed && !options.allowed.value) return
    void load(false).catch(() => {
      // The error ref is the consumer-facing failure projection.
    })
  }

  let stopAdmissionWatch: (() => void) | null = null
  onMounted(() => {
    executeWhenAllowed()
    if (options.allowed) {
      stopAdmissionWatch = watch(options.allowed, (allowed) => {
        if (allowed) executeWhenAllowed()
      })
    }
  })
  onUnmounted(() => stopAdmissionWatch?.())

  return { data, loading, error, execute }
}
