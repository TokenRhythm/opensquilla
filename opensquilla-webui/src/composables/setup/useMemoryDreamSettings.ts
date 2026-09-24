import { ref } from 'vue'
import type { AppSettings } from '@/modules/appSettings'

interface MemoryConfig {
  memory?: { dream?: { enabled?: boolean; auto_schedule?: boolean } }
}

export function useMemoryDreamSettings(appSettings: AppSettings) {
  const loaded = ref(false)
  const dreamEnabled = ref(false)
  const dreamAutoSchedule = ref(false)
  const busy = ref(false)
  const restartRequired = ref(false)

  async function load(): Promise<void> {
    const cfg = await appSettings.readAll() as MemoryConfig
    dreamEnabled.value = cfg?.memory?.dream?.enabled === true
    dreamAutoSchedule.value = cfg?.memory?.dream?.auto_schedule === true
    loaded.value = true
  }

  async function setDream(on: boolean): Promise<boolean> {
    if (busy.value) return false
    busy.value = true
    const previousEnabled = dreamEnabled.value
    const previousSchedule = dreamAutoSchedule.value
    dreamEnabled.value = on
    dreamAutoSchedule.value = on
    try {
      const result = await appSettings.patchSafe([
        { path: 'memory.dream.enabled', value: on },
        { path: 'memory.dream.auto_schedule', value: on },
      ]) as { restartRequired?: boolean }
      restartRequired.value = result.restartRequired === true
      return true
    } catch {
      dreamEnabled.value = previousEnabled
      dreamAutoSchedule.value = previousSchedule
      return false
    } finally {
      busy.value = false
    }
  }

  return { loaded, dreamEnabled, dreamAutoSchedule, busy, restartRequired, load, setDream }
}
