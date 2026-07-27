import { computed, ref, watch } from 'vue'

import { SANDBOX_RUN_MODES, isSandboxRunMode, type SandboxRunMode } from '@/types/sandbox'

export const RUN_MODE_STORAGE_KEY = 'opensquilla.chat.runMode'

export interface RunModePolicy {
  allowedRunModes?: unknown
  defaultRunMode?: unknown
  fullHostAccessDisabledReason?: unknown
}

interface UseChatRunModePreferenceOptions {
  runModePolicy: () => RunModePolicy | null | undefined
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
    return isSandboxRunMode(value) ? value : null
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
  if (modes.includes('trusted')) return 'trusted'
  return modes[0] ?? 'trusted'
}

export function useChatRunModePreference(options: UseChatRunModePreferenceOptions) {
  // Default to full host access. For the local owner the backend policy already
  // reports 'full'; this seeds it before the policy loads (no trusted flicker).
  // Remote non-owners still get 'trusted' from their policy, and the backend
  // coerces disallowed modes, so this does not weaken the sandbox boundary.
  const runMode = ref<SandboxRunMode>('full')
  const runModeUserSelected = ref(false)

  const currentRunModePolicy = computed(() => {
    const policy = options.runModePolicy()
    return policy && typeof policy === 'object' ? policy : null
  })

  const runModePolicyDefault = computed<SandboxRunMode>(() => {
    const raw = currentRunModePolicy.value?.defaultRunMode
    // Fall back to 'full' only when the policy omits a default; the backend
    // always supplies 'trusted' for non-owner principals, so they are unaffected.
    return isSandboxRunMode(raw) ? raw : 'full'
  })

  const allowedRunModes = computed<SandboxRunMode[]>(() => {
    const raw = currentRunModePolicy.value?.allowedRunModes
    if (!Array.isArray(raw)) return [...SANDBOX_RUN_MODES]
    const allowed = raw.filter(isSandboxRunMode)
    return allowed.length > 0 ? allowed : [...SANDBOX_RUN_MODES]
  })

  watch([allowedRunModes, runModePolicyDefault], ([modes, defaultMode]) => {
    const storedMode = readStoredRunMode()
    if (storedMode && modes.includes(storedMode)) {
      runMode.value = storedMode
      runModeUserSelected.value = true
      return
    }
    if (storedMode) clearStoredRunMode()

    if (runModeUserSelected.value && modes.includes(runMode.value)) return

    runModeUserSelected.value = false
    runMode.value = preferredRunMode(modes, defaultMode)
  }, { immediate: true })

  function setRunMode(mode: SandboxRunMode): SandboxRunMode {
    const next = modesSafeIncludes(allowedRunModes.value, mode)
      ? mode
      : preferredRunMode(allowedRunModes.value, runModePolicyDefault.value)
    runMode.value = next
    runModeUserSelected.value = true
    writeStoredRunMode(next)
    return next
  }

  return {
    runMode,
    runModeUserSelected,
    runModePolicyDefault,
    allowedRunModes,
    setRunMode,
  }
}

function modesSafeIncludes(modes: readonly SandboxRunMode[], mode: SandboxRunMode): boolean {
  return modes.includes(mode)
}
