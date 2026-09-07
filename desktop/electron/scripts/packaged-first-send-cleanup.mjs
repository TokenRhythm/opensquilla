import { closeElectronWithDeadline } from './e2e-shutdown-helpers.mjs'

// Preserve the production Gateway's shutdown request, 80s exit observation,
// and 6s + 5s hard-kill backstops without changing any interaction budget.
const ELECTRON_CLEANUP_TIMEOUT_MS = 100_000
const PROVIDER_CLEANUP_TIMEOUT_MS = 15_000

export async function cleanupPackagedFirstSend({
  app,
  provider,
  diagnostics,
  emit = line => console.error(line),
  onPhase = () => {},
  electronTimeoutMs = ELECTRON_CLEANUP_TIMEOUT_MS,
  providerTimeoutMs = PROVIDER_CLEANUP_TIMEOUT_MS,
}) {
  const errors = []
  if (app) {
    onPhase('electron-cleanup-start')
    try {
      const result = await closeElectronWithDeadline({
        app,
        phase: 'packaged-first-send',
        diagnostics,
        emit,
        timeoutMs: electronTimeoutMs,
      })
      onPhase('electron-cleanup-complete', {
        closed: result.closed,
        forcedExitSucceeded: result.forcedExitSucceeded,
      })
      // A forced exit is containment, never evidence that this gate passed.
      if (!result.closed) errors.push(result.error)
    } catch (error) {
      errors.push(error)
    }
  }
  if (provider) {
    onPhase('provider-cleanup-start')
    try {
      await provider.close({ timeoutMs: providerTimeoutMs })
      onPhase('provider-cleanup-complete', { closed: true })
    } catch (error) {
      errors.push(error)
      onPhase('provider-cleanup-complete', { closed: false })
    }
  }
  if (errors.length) {
    throw new AggregateError(errors, 'Packaged first-send cleanup failed')
  }
}
