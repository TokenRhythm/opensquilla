import type { SandboxRunMode, SandboxSetupStatusPayload } from '@/types/sandbox'

export function allowedComposerRunModes(
  allowed: SandboxRunMode[],
  setupStatus: SandboxSetupStatusPayload | null,
  setupResolved: boolean,
  capabilityAvailable = false,
): SandboxRunMode[] {
  return setupResolved && (capabilityAvailable || setupStatus?.state === 'ready')
    ? allowed
    : allowed.filter(mode => mode !== 'safe')
}

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
  capabilityAvailable = false,
): ComposerRunModeSelectionAction {
  if (mode === 'full') return 'persist'
  if (!setupResolved || setupStatus === null) return 'ignore'
  // A ready readiness result is authoritative for mode selection. Setup is
  // reserved for first-time configuration and an explicit retryable failure;
  // selecting Safe again must not reopen administrator setup on Windows.
  if (capabilityAvailable || setupStatus.state === 'ready') return 'persist'
  return ['not_setup', 'failed'].includes(setupStatus.state) && canSetup
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
