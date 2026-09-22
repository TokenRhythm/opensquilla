import type { SandboxRunMode, SandboxSetupStatusPayload } from '@/types/sandbox'

export function effectiveComposerRunMode(
  preference: SandboxRunMode,
  _setupStatus: SandboxSetupStatusPayload | null,
  activeLock: SandboxRunMode | null,
  _setupResolved = true,
): SandboxRunMode {
  if (activeLock) return activeLock
  return preference
}

export type ComposerRunModeSelectionAction = 'persist' | 'setup' | 'ignore'

export function composerRunModeSelectionAction(
  mode: SandboxRunMode,
  setupStatus: SandboxSetupStatusPayload | null,
  canSetup: boolean,
  setupResolved = true,
): ComposerRunModeSelectionAction {
  if (mode === 'full') return 'persist'
  if (!setupResolved || setupStatus === null) return 'ignore'
  const isWindows = setupStatus.platform.toLowerCase().startsWith('win')
  // Windows startup is passive and may report a stale marker as ready. Route
  // an explicit Safe selection through setup so the offline identity is
  // revalidated/repaired; portable ready states remain a cheap persistence.
  if (setupStatus.state === 'ready' && !(isWindows && canSetup)) return 'persist'
  return ['not_setup', 'failed', 'ready'].includes(setupStatus.state) && canSetup
    ? 'setup'
    : 'ignore'
}

export async function completeComposerSafeSetup(
  ensureSetup: () => Promise<boolean>,
  persistMode: (mode: SandboxRunMode) => Promise<unknown>,
): Promise<boolean> {
  if (!await ensureSetup()) return false
  await persistMode('safe')
  return true
}
