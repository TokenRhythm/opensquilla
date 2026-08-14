import { computed, ref, watch } from 'vue'

import {
  waitForSessionRpcConnection,
} from '@/composables/chat/sessionBootstrapAdmission'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'
import {
  SANDBOX_RUN_MODES,
  isRecognizedSandboxRunMode,
  normalizeSandboxRunMode,
  type SandboxRunMode,
} from '@/types/sandbox'

export const RUN_MODE_STORAGE_KEY = 'opensquilla.chat.runMode'

export interface RunModePolicy {
  allowedRunModes?: unknown
  defaultRunMode?: unknown
  fullHostAccessDisabledReason?: unknown
}

interface UseChatRunModePreferenceOptions {
  runModePolicy: () => RunModePolicy | null | undefined
  rpc: RunModePreferenceRpc
  hydrateCallOptions?: RpcCallOptions
  writeCallOptions?: RpcCallOptions
}

interface RunModePreferenceRpc {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  call: (
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<unknown>
}

interface RunModeRpc {
  waitForConnection: () => Promise<unknown>
  call: (
    method: string,
    params?: Record<string, unknown>,
  ) => Promise<unknown>
}

interface PersistMaterializedSessionRunModeOptions {
  rpc: RunModeRpc
  sessionKey: string
  isDraft: boolean
  runMode: SandboxRunMode
}

export async function persistMaterializedSessionRunMode(
  options: PersistMaterializedSessionRunModeOptions,
): Promise<void> {
  const sessionKey = options.sessionKey.trim()
  if (options.isDraft || !sessionKey) return
  await options.rpc.waitForConnection()
  await options.rpc.call('sandbox.run_context.set', {
    sessionKey,
    runMode: options.runMode,
  })
}

function availableStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function readStoredRunMode(): SandboxRunMode | null {
  try {
    const value = availableStorage()?.getItem(RUN_MODE_STORAGE_KEY)
    if (!isRecognizedSandboxRunMode(value)) return null
    const normalized = normalizeSandboxRunMode(value)
    if (value !== normalized) availableStorage()?.setItem(RUN_MODE_STORAGE_KEY, normalized)
    return normalized
  } catch {
    return null
  }
}

function writeStoredRunMode(mode: SandboxRunMode) {
  try {
    availableStorage()?.setItem(RUN_MODE_STORAGE_KEY, mode)
  } catch {
    // localStorage can be unavailable in restricted browser contexts.
  }
}

function clearStoredRunMode() {
  try {
    availableStorage()?.removeItem(RUN_MODE_STORAGE_KEY)
  } catch {
    // Ignore unavailable storage; the in-memory ref still reflects this mount.
  }
}

function preferredRunMode(
  modes: SandboxRunMode[],
  preferred: SandboxRunMode,
): SandboxRunMode {
  if (modes.includes(preferred)) return preferred
  if (modes.includes('safe')) return 'safe'
  return modes[0] ?? 'safe'
}

export function useChatRunModePreference(options: UseChatRunModePreferenceOptions) {
  // Full Access is the product default until the principal-specific backend
  // preference arrives. Sandbox readiness is reconciled separately by ChatView.
  const runMode = ref<SandboxRunMode>('full')
  const runModeUserSelected = ref(false)
  const runModeHydrated = ref(false)
  let writeSequence = 0

  const currentRunModePolicy = computed(() => {
    const policy = options.runModePolicy()
    return policy && typeof policy === 'object' ? policy : null
  })

  const runModePolicyDefault = computed<SandboxRunMode>(() => {
    const raw = currentRunModePolicy.value?.defaultRunMode
    return isRecognizedSandboxRunMode(raw) ? normalizeSandboxRunMode(raw) : 'full'
  })

  const allowedRunModes = computed<SandboxRunMode[]>(() => {
    const raw = currentRunModePolicy.value?.allowedRunModes
    if (!Array.isArray(raw)) return [...SANDBOX_RUN_MODES]
    const allowed = raw
      .filter(isRecognizedSandboxRunMode)
      .map(value => normalizeSandboxRunMode(value))
      .filter((value, index, values) => values.indexOf(value) === index)
    return allowed.length > 0 ? allowed : [...SANDBOX_RUN_MODES]
  })

  let initialized = false
  watch([allowedRunModes, runModePolicyDefault], ([modes, defaultMode]) => {
    if (!initialized) {
      initialized = true
      const storedMode = readStoredRunMode()
      if (storedMode && modes.includes(storedMode)) {
        runMode.value = storedMode
        runModeUserSelected.value = true
        return
      }
      if (storedMode) clearStoredRunMode()
      runMode.value = preferredRunMode(modes, defaultMode)
      return
    }
    if (modes.includes(runMode.value)) return
    const fallback = preferredRunMode(modes, defaultMode)
    runMode.value = fallback
    runModeUserSelected.value = false
    if (runModeHydrated.value) writeStoredRunMode(fallback)
  }, { immediate: true })

  function normalizePreference(mode: unknown): SandboxRunMode {
    const candidate = isRecognizedSandboxRunMode(mode)
      ? normalizeSandboxRunMode(mode)
      : runModePolicyDefault.value
    return modesSafeIncludes(allowedRunModes.value, candidate)
      ? candidate
      : preferredRunMode(allowedRunModes.value, runModePolicyDefault.value)
  }

  function applyConfirmedPreference(
    mode: unknown,
    options: { selected: boolean },
  ): SandboxRunMode {
    const next = normalizePreference(mode)
    runMode.value = next
    runModeUserSelected.value = options.selected
    runModeHydrated.value = true
    writeStoredRunMode(next)
    return next
  }

  function modeFromPayload(payload: unknown): unknown {
    if (!payload || typeof payload !== 'object') return undefined
    return (payload as Record<string, unknown>).runMode
  }

  async function hydrateRunModePreference(): Promise<SandboxRunMode> {
    await waitForSessionRpcConnection(options.rpc, options.hydrateCallOptions)
    const payload = options.hydrateCallOptions
      ? await options.rpc.call(
          'sandbox.run_mode.preference.get',
          undefined,
          options.hydrateCallOptions,
        )
      : await options.rpc.call('sandbox.run_mode.preference.get')
    const source = payload && typeof payload === 'object'
      ? (payload as Record<string, unknown>).source
      : null
    return applyConfirmedPreference(
      modeFromPayload(payload),
      { selected: source === 'preference' },
    )
  }

  async function setGlobalRunMode(mode: SandboxRunMode): Promise<SandboxRunMode> {
    const requested = modesSafeIncludes(allowedRunModes.value, mode)
      ? mode
      : preferredRunMode(allowedRunModes.value, runModePolicyDefault.value)
    const previous = runMode.value
    const previousSelected = runModeUserSelected.value
    const sequence = ++writeSequence

    // A mode switch is a local interaction first. Reflect it immediately so a
    // slow or queued persistence request cannot make the composer look stuck.
    // Browser storage remains confirmation-only.
    runMode.value = requested
    runModeUserSelected.value = true

    try {
      await waitForSessionRpcConnection(options.rpc, options.writeCallOptions)
      const payload = options.writeCallOptions
        ? await options.rpc.call(
            'sandbox.run_mode.preference.set',
            { runMode: requested },
            options.writeCallOptions,
          )
        : await options.rpc.call('sandbox.run_mode.preference.set', {
            runMode: requested,
          })
      if (sequence !== writeSequence) return runMode.value
      return applyConfirmedPreference(
        modeFromPayload(payload),
        { selected: true },
      )
    } catch (cause) {
      if (sequence === writeSequence) {
        runMode.value = previous
        runModeUserSelected.value = previousSelected
      }
      throw cause
    }
  }

  function applyRunModePreferenceChanged(payload: unknown): SandboxRunMode {
    return applyConfirmedPreference(
      modeFromPayload(payload),
      { selected: true },
    )
  }

  return {
    runMode,
    runModeUserSelected,
    runModeHydrated,
    runModePolicyDefault,
    allowedRunModes,
    hydrateRunModePreference,
    setGlobalRunMode,
    applyRunModePreferenceChanged,
  }
}

function modesSafeIncludes(modes: readonly SandboxRunMode[], mode: SandboxRunMode): boolean {
  return modes.includes(mode)
}
