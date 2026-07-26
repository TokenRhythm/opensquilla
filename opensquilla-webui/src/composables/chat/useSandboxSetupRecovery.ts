import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import type {
  SandboxRunMode,
  SandboxSetupState,
  SandboxSetupStatusPayload,
} from '@/types/sandbox'

const SETUP_POLL_MS = 2000

type SandboxSetupRpc = {
  call: (method: string, params?: Record<string, unknown>) => Promise<unknown>
}

export interface UseSandboxSetupRecoveryOptions {
  rpc: SandboxSetupRpc
  connectionState: Ref<string>
  runMode: Ref<SandboxRunMode>
}

function normalizeStatus(payload: unknown): SandboxSetupStatusPayload | null {
  if (!payload || typeof payload !== 'object') return null
  const raw = payload as Record<string, unknown>
  const state = String(raw.state || '') as SandboxSetupState
  if (!['not_setup', 'setting_up', 'ready', 'failed', 'unavailable'].includes(state)) return null
  return {
    state,
    platform: String(raw.platform || ''),
    message: String(raw.message || ''),
    requiresAdmin: raw.requiresAdmin === true || raw.requires_admin === true,
    detail: typeof raw.detail === 'string' ? raw.detail : undefined,
  }
}

export function useSandboxSetupRecovery(options: UseSandboxSetupRecoveryOptions) {
  const status = ref<SandboxSetupStatusPayload | null>(null)
  const loading = ref(false)
  const ensuring = ref(false)
  const dismissed = ref(false)
  const error = ref('')
  let requestGeneration = 0
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let lastState = ''

  const active = computed(() =>
    options.connectionState.value === 'connected' && options.runMode.value !== 'full')
  const visible = computed(() =>
    active.value && !dismissed.value && status.value !== null && status.value.state !== 'ready')
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
    schedulePoll()
  }

  async function refresh() {
    if (!active.value) return
    const generation = ++requestGeneration
    loading.value = status.value === null
    clearPoll()
    try {
      const payload = normalizeStatus(await options.rpc.call('sandbox.setup.status'))
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
      if (generation === requestGeneration) loading.value = false
    }
  }

  async function ensureSetup() {
    if (!canSetup.value || ensuring.value) return
    const generation = ++requestGeneration
    ensuring.value = true
    error.value = ''
    clearPoll()
    try {
      const payload = normalizeStatus(await options.rpc.call('sandbox.setup.ensure'))
      if (generation !== requestGeneration || !payload) return
      applyStatus(payload)
    } catch (cause) {
      if (generation === requestGeneration) {
        error.value = cause instanceof Error ? cause.message : String(cause)
      }
    } finally {
      if (generation === requestGeneration) ensuring.value = false
    }
  }

  function dismiss() {
    dismissed.value = true
  }

  watch(
    () => [options.connectionState.value, options.runMode.value] as const,
    ([connection, mode], previous) => {
      const changedMode = previous && previous[1] !== mode
      if (changedMode) dismissed.value = false
      requestGeneration++
      clearPoll()
      if (connection === 'connected' && mode !== 'full') void refresh()
      else {
        status.value = null
        lastState = ''
        loading.value = false
        ensuring.value = false
      }
    },
    { immediate: true },
  )

  onScopeDispose(() => {
    requestGeneration++
    clearPoll()
  })

  return {
    status,
    loading,
    ensuring,
    dismissed,
    error,
    visible,
    canSetup,
    refresh,
    ensureSetup,
    dismiss,
  }
}
