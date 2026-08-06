import type { SandboxRunMode, SandboxSetupStatusPayload } from '@/types/sandbox'

export function effectiveComposerRunMode(
  preference: SandboxRunMode,
  setupStatus: SandboxSetupStatusPayload | null,
  activeLock: SandboxRunMode | null,
  setupResolved = true,
): SandboxRunMode {
  if (activeLock) return activeLock
  if (!setupResolved) return 'full'
  if (preference === 'safe' && setupStatus && setupStatus.state !== 'ready') return 'full'
  return preference
}

export type ComposerRunModeSelectionAction = 'persist' | 'setup' | 'ignore'

export function composerRunModeSelectionAction(
  mode: SandboxRunMode,
  setupStatus: SandboxSetupStatusPayload | null,
  canSetup: boolean,
  setupResolved = true,
): ComposerRunModeSelectionAction {
  if (mode === 'safe' && !setupResolved) return 'ignore'
  if (mode === 'full' || setupStatus === null || setupStatus.state === 'ready') return 'persist'
  return canSetup ? 'setup' : 'ignore'
}

export async function completeComposerSafeSetup(
  ensureSetup: () => Promise<boolean>,
  persistMode: (mode: SandboxRunMode) => Promise<unknown>,
): Promise<boolean> {
  if (!await ensureSetup()) return false
  await persistMode('safe')
  return true
}
