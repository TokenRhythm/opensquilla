import { computed, ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'
import type { AppSettings } from '@/modules/appSettings'

/** Config slice + status consumed by the Settings › Advanced
 *  "memory & self-learning" group. Dream and self-learning are separate
 *  opt-ins with an asymmetric linkage owned by the backend: enabling
 *  self-learning pulls dream up with it (`linked` in the patch response),
 *  disabling dream never touches self-learning. This composable only
 *  mirrors that contract — it never re-implements the linkage client-side. */

interface MemoryLearningConfig {
  memory?: { dream?: { enabled?: boolean; auto_schedule?: boolean } }
  squilla_router?: { self_learning?: { enabled?: boolean } }
}

interface PatchResponse {
  restartRequired?: boolean
  linked?: string[]
  linkedLive?: boolean
}

export interface SelfLearningStatus {
  enabled?: boolean
  captureEnabled?: boolean
  trainingReachable?: boolean
  dream?: { enabled?: boolean; autoSchedule?: boolean; killSwitchActive?: boolean }
  activeModel?: { kind?: string; version?: string | null; promotedAt?: string | null }
  samples?: {
    total?: number
    highValue?: number
    requiredHighValue?: number
    complaintRate?: number
    lastCapturedAt?: string | null
    feedback?: { up?: number; down?: number; downSingle?: number }
  } | null
  gate?: {
    wouldTrain?: boolean
    reason?: string
    lastAttemptAt?: string | null
    lastTrainedAt?: string | null
    killSwitchActive?: boolean
  } | null
  lastReceipt?: { kind?: string; version?: string | null; reason?: string | null } | null
  error?: string
}

export function useMemoryLearningSettings(appSettings: AppSettings) {
  const rpc = useRpcStore()

  const loaded = ref(false)
  const dreamEnabled = ref(false)
  const dreamAutoSchedule = ref(false)
  const selfLearningEnabled = ref(false)
  // True while the ON state of dream came from the linkage rather than the
  // user's own click — rendered as the "linked" (desaturated) switch state.
  const dreamLinkedOn = ref(false)
  const busy = ref(false)
  const restartRequired = ref(false)
  const status = ref<SelfLearningStatus | null>(null)
  const statusLoading = ref(false)

  // Training rides the dream cadence: on-but-unreachable is the warn state.
  const trainingPaused = computed(
    () => selfLearningEnabled.value && !(dreamEnabled.value && dreamAutoSchedule.value),
  )

  async function load(): Promise<void> {
    try {
      const cfg = await appSettings.readAll() as MemoryLearningConfig
      dreamEnabled.value = cfg?.memory?.dream?.enabled === true
      dreamAutoSchedule.value = cfg?.memory?.dream?.auto_schedule === true
      selfLearningEnabled.value = cfg?.squilla_router?.self_learning?.enabled === true
      loaded.value = true
      if (selfLearningEnabled.value) void refreshStatus()
    } catch {
      // Older gateways without these keys: leave the group at defaults (off).
    }
  }

  async function refreshStatus(): Promise<void> {
    if (statusLoading.value) return
    statusLoading.value = true
    try {
      status.value = await rpc.call<SelfLearningStatus>('router.selflearning.status')
    } catch {
      status.value = null
    } finally {
      statusLoading.value = false
    }
  }

  async function setSelfLearning(on: boolean): Promise<boolean> {
    if (busy.value) return false
    busy.value = true
    const prev = selfLearningEnabled.value
    selfLearningEnabled.value = on
    try {
      const res = await appSettings.patchSafe([
        { path: 'squilla_router.self_learning.enabled', value: on },
      ]) as PatchResponse
      if (res?.linked?.length) {
        // The backend enabled the dream chain alongside; mirror it.
        dreamEnabled.value = true
        dreamAutoSchedule.value = true
        dreamLinkedOn.value = true
        if (res.linkedLive === false) restartRequired.value = true
      }
      if (res?.restartRequired) restartRequired.value = true
      if (on) void refreshStatus()
      else status.value = null
      return true
    } catch {
      selfLearningEnabled.value = prev
      return false
    } finally {
      busy.value = false
    }
  }

  async function setDream(on: boolean): Promise<boolean> {
    if (busy.value) return false
    busy.value = true
    const prevEnabled = dreamEnabled.value
    const prevSchedule = dreamAutoSchedule.value
    dreamEnabled.value = on
    dreamAutoSchedule.value = on
    dreamLinkedOn.value = false // the user has now touched it themselves
    try {
      const res = await appSettings.patchSafe([
        { path: 'memory.dream.enabled', value: on },
        { path: 'memory.dream.auto_schedule', value: on },
      ]) as PatchResponse
      if (res?.restartRequired) restartRequired.value = true
      if (selfLearningEnabled.value) void refreshStatus()
      return true
    } catch {
      dreamEnabled.value = prevEnabled
      dreamAutoSchedule.value = prevSchedule
      return false
    } finally {
      busy.value = false
    }
  }

  return {
    loaded,
    dreamEnabled,
    dreamAutoSchedule,
    dreamLinkedOn,
    selfLearningEnabled,
    trainingPaused,
    busy,
    restartRequired,
    status,
    statusLoading,
    load,
    refreshStatus,
    setSelfLearning,
    setDream,
  }
}
