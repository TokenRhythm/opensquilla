import { setTimeout as delay } from 'node:timers/promises'
import { closeElectronWithDeadline } from './e2e-shutdown-helpers.mjs'

// Preserve the production Gateway's shutdown request, 80s exit observation,
// and 6s + 5s hard-kill backstops without changing any interaction budget.
const ELECTRON_CLEANUP_TIMEOUT_MS = 100_000
const PROVIDER_CLEANUP_TIMEOUT_MS = 15_000

export async function captureFirstSendDiagnostic(operation, timeoutMs = 3_000) {
  const controller = new AbortController()
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      delay(timeoutMs, undefined, { signal: controller.signal }).then(() => {
        throw new Error(`First-send diagnostics timed out after ${timeoutMs}ms`)
      }),
    ])
  } catch (error) {
    return { diagnosticError: error?.message || String(error) }
  } finally {
    controller.abort()
  }
}

export async function captureElectronProcessIdentity(app, timeoutMs = 3_000) {
  // Playwright 1.60 launches cmd.exe on Windows. Its process() is the wrapper;
  // capture the actual Electron PID while the main-process protocol is live.
  const wrapperPid = app.process()?.pid ?? null
  const result = await captureFirstSendDiagnostic(
    () => app.evaluate(() => process.pid),
    timeoutMs,
  )
  const identity = {
    wrapperPid,
    electronPid: Number.isSafeInteger(result) && result > 0 ? result : null,
    ...(result?.diagnosticError ? { diagnosticError: result.diagnosticError } : {}),
  }
  return identity
}

export function electronProcessSnapshot(identity) {
  const snapshot = { ...identity }
  for (const role of ['wrapper', 'electron']) {
    const pid = identity?.[`${role}Pid`]
    if (!Number.isSafeInteger(pid) || pid < 1) continue
    // Signal 0 only checks existence. This diagnostic never terminates a PID
    // or treats it as proof of process identity after possible PID reuse.
    try {
      process.kill(pid, 0)
      snapshot[`${role}PidExists`] = true
    } catch (error) {
      snapshot[`${role}PidExists`] = error.code === 'ESRCH' ? false : null
      if (error.code !== 'ESRCH') snapshot[`${role}ProbeError`] = error.code || String(error)
    }
  }
  return snapshot
}

export async function closeElectronAndObserveExit(app, identity, timeoutMs) {
  if (!identity?.electronPid || !identity?.wrapperPid) {
    throw new Error('Electron shutdown requires both observed process identities')
  }
  const deadline = Date.now() + timeoutMs
  // Playwright disposes its ElectronApplication dispatcher after close(), so
  // retain the child handle before asking it to close.
  const child = app.process()
  await app.close()
  await observeNaturalElectronExit(child, identity, Math.max(0, deadline - Date.now()))
}

async function observeNaturalElectronExit(child, identity, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const state = electronProcessSnapshot(identity)
    if (child.exitCode !== null || child.signalCode !== null) {
      if (child.exitCode !== 0 || child.signalCode !== null) {
        throw new Error('Electron quit did not produce a natural zero exit code')
      }
      if (state.wrapperPidExists === false && state.electronPidExists === false) return
    }
    await delay(25)
  }
  throw new Error('Electron quit left an observed Electron or wrapper process alive')
}

export async function cleanupPackagedFirstSend({
  app,
  provider,
  diagnostics,
  processIdentity,
  emit = line => console.error(line),
  onPhase = () => {},
  electronTimeoutMs = ELECTRON_CLEANUP_TIMEOUT_MS,
  diagnosticTimeoutMs = 3_000,
  providerTimeoutMs = PROVIDER_CLEANUP_TIMEOUT_MS,
}) {
  const errors = []
  if (app) {
    onPhase('electron-cleanup-start')
    try {
      const child = app.process()
      const result = await closeElectronWithDeadline({
        app: {
          process: () => child,
          close: () => closeElectronAndObserveExit(app, processIdentity, electronTimeoutMs),
        },
        phase: 'packaged-first-send',
        diagnostics,
        diagnosticTimeoutMs,
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
