import { defineStore } from 'pinia'
import { ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import {
  ensureSandboxReady,
  type SandboxSetupOutcome,
} from '@/composables/sandboxSetupCoordinator'
import { useRpcStore } from '@/stores/rpc'
import type {
  SandboxRunMode,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

export const useSandboxSetupStore = defineStore('sandboxSetup', () => {
  const rpc = useRpcStore()
  const { pushToast } = useToasts()
  const ensuring = ref(false)
  const outcome = ref<SandboxSetupOutcome>('idle')
  const status = ref<SandboxSetupStatusPayload | null>(null)
  const intendedMode = ref<SandboxRunMode>('full')
  let inFlight: Promise<boolean> | null = null

  function noteRunModeSelection(mode: SandboxRunMode): void {
    intendedMode.value = mode
  }

  function resetOutcome(): void {
    if (!ensuring.value) outcome.value = 'idle'
  }

  async function runSetup(): Promise<boolean> {
    try {
      const result = await ensureSandboxReady(
        (method, params) => rpc.call(method, params),
        null,
        () => rpc.waitForConnection(10_000),
      )
      status.value = result.status
      outcome.value = result.outcome
      if (!result.ready) {
        pushToast(String(i18n.global.t('settings.sandbox.setup.failedToast')), {
          tone: 'danger',
        })
        return false
      }
      if (intendedMode.value === 'safe') {
        await rpc.call('sandbox.run_mode.preference.set', { runMode: 'safe' })
      }
      pushToast(String(i18n.global.t('settings.sandbox.setup.readyToast')), {
        tone: 'ok',
      })
      return true
    } catch {
      outcome.value = 'failed'
      pushToast(String(i18n.global.t('settings.sandbox.setup.failedToast')), {
        tone: 'danger',
      })
      return false
    }
  }

  function startSafeSetup(): Promise<boolean> {
    if (inFlight) return inFlight
    intendedMode.value = 'safe'
    ensuring.value = true
    outcome.value = 'idle'
    inFlight = runSetup().finally(() => {
      ensuring.value = false
      inFlight = null
    })
    return inFlight
  }

  return {
    ensuring,
    outcome,
    status,
    intendedMode,
    noteRunModeSelection,
    resetOutcome,
    startSafeSetup,
  }
})
