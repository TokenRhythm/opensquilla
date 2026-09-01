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
  if (setupStatus.state === 'ready') return 'persist'
  return setupStatus.state === 'not_setup' && canSetup ? 'setup' : 'ignore'
}

export async function completeComposerSafeSetup(
  ensureSetup: () => Promise<boolean>,
  persistMode: (mode: SandboxRunMode) => Promise<unknown>,
): Promise<boolean> {
  if (!await ensureSetup()) return false
  await persistMode('safe')
  return true
}
