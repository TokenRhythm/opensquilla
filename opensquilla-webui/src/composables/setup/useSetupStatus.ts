import { onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import type { SetupStatusPort } from '@/modules/setupWorkflow'
import { onReadinessInvalidated } from './readinessInvalidation'

let cachedStatus = new WeakMap<SetupStatusPort, object>()
let pendingStatus = new WeakMap<SetupStatusPort, Promise<object>>()
let revision = 0

// Invalidate once for all consumers, including ones mounted after the save.
onReadinessInvalidated(() => {
  revision += 1
  cachedStatus = new WeakMap()
  pendingStatus = new WeakMap()
})

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
  let requestId = 0

  async function load(refresh: boolean): Promise<void> {
    const currentRequest = ++requestId
    const currentRevision = revision
    const cached = cachedStatus.get(setup)
    if (!refresh && cached) {
      data.value = cached as T
      loading.value = false
      error.value = null
      return
    }
    loading.value = true
    error.value = null
    try {
      let pending = pendingStatus.get(setup)
      if (!pending) {
        pending = setup.status().then((value) => {
          if (currentRevision === revision) cachedStatus.set(setup, value)
          return value
        })
        pendingStatus.set(setup, pending)
        void pending.finally(() => {
          if (pendingStatus.get(setup) === pending) pendingStatus.delete(setup)
        }).catch(() => {
          // Each consumer projects the shared rejection into its own error ref.
        })
      }
      const value = await pending as T
      if (currentRequest === requestId && currentRevision === revision) data.value = value
    } catch (reason) {
      if (currentRequest === requestId && currentRevision === revision) {
        error.value = reason instanceof Error ? reason.message : String(reason)
      }
      throw reason
    } finally {
      if (currentRequest === requestId) loading.value = false
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
  let stopInvalidation: (() => void) | null = null
  onMounted(() => {
    stopInvalidation = onReadinessInvalidated(executeWhenAllowed)
    executeWhenAllowed()
    if (options.allowed) {
      stopAdmissionWatch = watch(options.allowed, (allowed) => {
        if (allowed) executeWhenAllowed()
      })
    }
  })
  onUnmounted(() => {
    requestId += 1
    stopAdmissionWatch?.()
    stopInvalidation?.()
  })

  return { data, loading, error, execute }
}
