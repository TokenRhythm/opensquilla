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
  return {
    wrapperPid,
    electronPid: Number.isSafeInteger(result) && result > 0 ? result : null,
    ...(result?.diagnosticError ? { diagnosticError: result.diagnosticError } : {}),
  }
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

export async function installQuitDiagnosticProbe(app, diagnosticFile, { extended = false } = {}) {
  await app.evaluate(({ app, BrowserWindow, webContents }, { file, extended }) => {
    const fs = process.getBuiltinModule('fs')
    const log = (event, detail = {}) => fs.appendFileSync(file, JSON.stringify({
      event, at: new Date().toISOString(), pid: process.pid, ...detail,
    }) + '\n')
    const resourceSnapshot = () => {
      try {
        const resourceTypes = {}
        for (const type of process.getActiveResourcesInfo()) {
          resourceTypes[type] = (resourceTypes[type] || 0) + 1
        }
        const detail = {
          resourceTypes,
          windowCount: BrowserWindow.getAllWindows().length,
          webContentsCount: webContents.getAllWebContents().length,
        }
        if (extended) {
          const activeRequestTypes = {}
          for (const request of process._getActiveRequests?.() || []) {
            const type = request?.constructor?.name || 'Unknown'
            activeRequestTypes[type] = (activeRequestTypes[type] || 0) + 1
          }
          const streamState = stream => ({
            destroyed: stream.destroyed,
            writableEnded: stream.writableEnded,
            writableFinished: stream.writableFinished,
            writableLength: stream.writableLength,
          })
          detail.activeRequestTypes = activeRequestTypes
          detail.webContents = webContents.getAllWebContents().map(contents => {
            const metadata = { id: contents.id }
            try {
              return {
                ...metadata,
                type: contents.getType(),
                destroyed: contents.isDestroyed(),
                osProcessId: contents.getOSProcessId(),
                electronDebuggerAttached: contents.debugger.isAttached(),
              }
            } catch (error) {
              return { ...metadata, diagnosticError: String(error?.message || error).slice(0, 500) }
            }
          })
          const childStreamState = stream => stream ? {
            destroyed: stream.destroyed,
            readableEnded: stream.readableEnded,
            writableFinished: stream.writableFinished,
          } : null
          detail.activeChildProcesses = (process._getActiveHandles?.() || [])
            .filter(handle => handle?.constructor?.name === 'ChildProcess')
            .map(child => ({
              pid: child.pid,
              exitCode: child.exitCode,
              signalCode: child.signalCode,
              connected: child.connected,
              stdin: childStreamState(child.stdin),
              stdout: childStreamState(child.stdout),
              stderr: childStreamState(child.stderr),
            }))
          detail.stdio = {
            stdout: streamState(process.stdout),
            stderr: streamState(process.stderr),
          }
        }
        return detail
      } catch (error) {
        return { resourceDiagnosticError: String(error?.message || error).slice(0, 500) }
      }
    }
    const originalExit = app.exit
    app.exit = function (...args) {
      log('app-exit-entered', resourceSnapshot())
      try {
        const result = originalExit.apply(this, args)
        log('app-exit-returned', resourceSnapshot())
        if (extended) {
          // Observe whether the main event loop runs again without keeping it
          // alive or changing the application's requested exit behavior.
          setImmediate(() => log('app-exit-next-turn', resourceSnapshot())).unref()
          for (const delayMs of [100, 1_000]) {
            setTimeout(() => log('app-exit-after-delay', {
              delayMs, ...resourceSnapshot(),
            }), delayMs).unref()
          }
        }
        return result
      } catch (error) {
        log('app-exit-threw', { error: String(error?.message || error).slice(0, 500) })
        throw error
      }
    }
    app.once('quit', (_event, exitCode) => log('quit', { exitCode }))
    if (extended) {
      app.once('will-quit', () => log('will-quit', resourceSnapshot()))
      process.once('beforeExit', exitCode => log('process-before-exit', { exitCode, ...resourceSnapshot() }))
      process.once('exit', exitCode => log('process-exit', { exitCode, ...resourceSnapshot() }))
      for (const contents of webContents.getAllWebContents()) {
        const webContentsId = contents.id
        contents.once('destroyed', () => log('web-contents-destroyed', { webContentsId }))
      }
    }
    for (const window of BrowserWindow.getAllWindows()) {
      const windowId = window.id
      window.once('closed', () => log('window-closed', { windowId }))
    }
    log('probe-installed', resourceSnapshot())
  }, { file: diagnosticFile, extended })
}

export async function quitElectronOnNextTurn(app, identity, timeoutMs) {
  if (!identity?.electronPid || !identity?.wrapperPid) {
    throw new Error('Deferred quit diagnostic requires both observed process identities')
  }
  const child = app.process()
  // Keep the debugger connection alive during the production asynchronous
  // drain. Playwright's normal close() disconnects it immediately after quit().
  await app.evaluate(({ app }) => { setImmediate(() => app.quit()) })
  await observeNaturalElectronExit(child, identity, timeoutMs)
}

export async function closeElectronAfterRemovingRoutes(app, identity, timeoutMs) {
  return closeElectronAndObserveExit(app, identity, timeoutMs, true)
}

export async function closeElectronAndObserveExit(app, identity, timeoutMs, removeRoutes = false) {
  if (!identity?.electronPid || !identity?.wrapperPid) {
    throw new Error('Electron shutdown requires both observed process identities')
  }
  const deadline = Date.now() + timeoutMs
  // Playwright disposes its ElectronApplication dispatcher after close(), so
  // retain the child handle before asking it to close.
  const child = app.process()
  if (removeRoutes) await app.context().unrouteAll({ behavior: 'wait' })
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
  deferQuit = false,
  unrouteBeforeQuit = false,
  processIdentity,
  emit = line => console.error(line),
  onPhase = () => {},
  electronTimeoutMs = ELECTRON_CLEANUP_TIMEOUT_MS,
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
          close: () => unrouteBeforeQuit
            ? closeElectronAfterRemovingRoutes(app, processIdentity, electronTimeoutMs)
            : deferQuit
              ? quitElectronOnNextTurn(app, processIdentity, electronTimeoutMs)
              : closeElectronAndObserveExit(app, processIdentity, electronTimeoutMs),
        },
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
