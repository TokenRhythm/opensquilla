import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import {
  ensureSandboxReady,
  normalizeSandboxSetupStatus,
  type SandboxSetupOutcome,
} from '@/composables/sandboxSetupCoordinator'
import type {
  SandboxRunMode,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

const SETUP_POLL_MS = 2000

type SandboxSetupRpc = {
  call: (method: string, params?: Record<string, unknown>) => Promise<unknown>
  waitForConnection?: () => Promise<unknown>
}

export interface UseSandboxSetupRecoveryOptions {
  rpc: SandboxSetupRpc
  connectionState: Ref<string>
  runMode: Ref<SandboxRunMode>
  autoRefresh?: boolean
  onUnavailable?: (
    status: SandboxSetupStatusPayload & { state: 'failed' | 'unavailable' },
  ) => void | Promise<void>
}

export function useSandboxSetupRecovery(options: UseSandboxSetupRecoveryOptions) {
  const status = ref<SandboxSetupStatusPayload | null>(null)
  const resolved = ref(false)
  const loading = ref(false)
  const ensuring = ref(false)
  const dismissed = ref(false)
  const error = ref('')
  const outcome = ref<SandboxSetupOutcome>('idle')
  let requestGeneration = 0
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let lastState = ''
  let lastUnavailableFingerprint = ''

  const active = computed(() => options.connectionState.value === 'connected')
  const visible = computed(() =>
    active.value
    && options.runMode.value !== 'full'
    && !dismissed.value
    && status.value !== null
    && status.value.state !== 'ready')
  const isWindows = computed(() => status.value?.platform.toLowerCase().startsWith('win') === true)
  const canSetup = computed(() =>
    isWindows.value && (status.value?.state === 'not_setup' || status.value?.state === 'failed'))

  function clearPoll() {
    if (pollTimer) clearTimeout(pollTimer)
    pollTimer = null
  }

  function schedulePoll() {
    clearPoll()
    if (!active.value || status.value?.state !== 'setting_up') return
    pollTimer = setTimeout(() => { void refresh() }, SETUP_POLL_MS)
  }

  function applyStatus(next: SandboxSetupStatusPayload) {
    if (lastState && lastState !== next.state) dismissed.value = false
    lastState = next.state
    status.value = next
    if (next.state !== 'failed') error.value = ''
    if (next.state === 'ready') {
      lastUnavailableFingerprint = ''
    } else if (next.state === 'failed' || next.state === 'unavailable') {
      const fingerprint = `${next.state}\0${next.message}\0${next.detail || ''}`
      if (fingerprint !== lastUnavailableFingerprint) {
        lastUnavailableFingerprint = fingerprint
        void Promise.resolve(options.onUnavailable?.({
          ...next,
          state: next.state,
        })).catch((cause) => {
          console.warn(
            'Failed to report unavailable sandbox:',
            cause instanceof Error ? cause.message : String(cause),
          )
        })
      }
    }
    schedulePoll()
  }

  async function refresh() {
    if (!active.value) return
    const generation = ++requestGeneration
    loading.value = status.value === null
    clearPoll()
    try {
      const payload = normalizeSandboxSetupStatus(await options.rpc.call('sandbox.setup.status'))
      if (generation !== requestGeneration) return
      if (!payload) {
        // Keep following an already-authoritative setting_up state when a
        // transient/malformed response cannot advance it. schedulePoll remains
        // a no-op for old Gateways that never established a setup status.
        schedulePoll()
        return
      }
      // Any authoritative status supersedes a prior transport failure,
      // including a terminal failed payload with its own server-side state.
      error.value = ''
      applyStatus(payload)
    } catch (cause) {
      if (generation !== requestGeneration) return
      // Old/unavailable Gateways do not get guessed into a setup state. With no
      // authoritative payload the recovery surface stays hidden and
      // schedulePoll remains a no-op; an established setting_up state retries.
      error.value = cause instanceof Error ? cause.message : String(cause)
      schedulePoll()
    } finally {
      if (generation === requestGeneration) {
        resolved.value = true
        loading.value = false
      }
    }
  }

  async function ensureSetup(): Promise<boolean> {
    if (!canSetup.value || ensuring.value) return false
    const generation = ++requestGeneration
    ensuring.value = true
    error.value = ''
    clearPoll()
    try {
      const result = await ensureSandboxReady(
        options.rpc.call,
        null,
        options.rpc.waitForConnection ?? null,
      )
      if (generation !== requestGeneration) return false
      if (result.status) applyStatus(result.status)
      outcome.value = result.outcome
      return result.ready
    } finally {
      if (generation === requestGeneration) ensuring.value = false
    }
  }

  function dismiss() {
    dismissed.value = true
  }

  watch(
    () => options.connectionState.value,
    (connection) => {
      requestGeneration++
      clearPoll()
      if (options.autoRefresh !== false && connection === 'connected') {
        void refresh()
      }
      else {
        status.value = null
        resolved.value = false
        lastState = ''
        lastUnavailableFingerprint = ''
        loading.value = false
        ensuring.value = false
      }
    },
    { immediate: true },
  )

  watch(
    () => options.runMode.value,
    () => {
      dismissed.value = false
    },
  )

  onScopeDispose(() => {
    requestGeneration++
    clearPoll()
  })

  return {
    status,
    resolved,
    loading,
    ensuring,
    dismissed,
    error,
    outcome,
    visible,
    canSetup,
    refresh,
    ensureSetup,
    dismiss,
  }
}
