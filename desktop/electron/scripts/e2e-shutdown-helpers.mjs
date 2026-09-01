import { setTimeout as delay } from 'node:timers/promises'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

const FORCED_PROCESS_EXIT_TIMEOUT_MS = 5_000
const WINDOWS_PROCESS_TREE_KILL_TIMEOUT_MS = 5_000

function timeoutError(label, timeoutMs) {
  const error = new Error(`${label} timed out after ${timeoutMs}ms`)
  error.code = 'DESKTOP_E2E_SHUTDOWN_TIMEOUT'
  return error
}

async function withDeadline(operation, label, timeoutMs) {
  const controller = new AbortController()
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      delay(timeoutMs, undefined, { signal: controller.signal }).then(() => {
        throw timeoutError(label, timeoutMs)
      }),
    ])
  } finally {
    controller.abort()
  }
}

export function trackHttpServerConnections(server) {
  const sockets = new Set()
  server.on('connection', (socket) => {
    sockets.add(socket)
    socket.once('close', () => sockets.delete(socket))
  })
  return sockets
}

export async function closeHttpServerWithDeadline(
  server,
  sockets,
  { label = 'HTTP fixture shutdown', timeoutMs = 15_000 } = {},
) {
  server.closeIdleConnections?.()
  const closed = new Promise((resolveClose, rejectClose) => {
    try {
      server.close(error => error ? rejectClose(error) : resolveClose())
    } catch (error) {
      rejectClose(error)
    }
  })
  try {
    await withDeadline(() => closed, label, timeoutMs)
  } catch (error) {
    // Node's HTTP close waits for active requests. A failed test can strand one
    // behind an Electron/Gateway teardown, so force only this synthetic fixture
    // after preserving the bounded failure as the primary diagnostic.
    server.closeAllConnections?.()
    for (const socket of sockets) socket.destroy()
    sockets.clear()
    throw error
  }
}

async function boundedDiagnostics(diagnostics, timeoutMs) {
  if (!diagnostics) return null
  try {
    return await withDeadline(diagnostics, 'Electron shutdown diagnostics', timeoutMs)
  } catch (error) {
    return { diagnosticError: error?.message || String(error) }
  }
}

function childHasExited(child) {
  return child.exitCode !== null || child.signalCode !== null
}

async function forceElectronProcessExit(child, phase, emit) {
  if (childHasExited(child)) return null
  const exited = new Promise(resolve => child.once('exit', resolve))
  if (process.platform === 'win32' && child.pid) {
    await terminateWindowsProcessTree({
      pid: child.pid,
      timeoutMs: WINDOWS_PROCESS_TREE_KILL_TIMEOUT_MS,
      fallback: () => {
        if (!childHasExited(child)) child.kill('SIGKILL')
      },
      onFailure: failure => emit(JSON.stringify({
        event: 'desktop_e2e_process_tree_termination_failed',
        phase,
        ...failure,
      })),
    })
  } else {
    try {
      child.kill('SIGKILL')
    } catch (error) {
      return error
    }
  }
  try {
    await withDeadline(
      () => exited,
      `${phase} forced process exit`,
      FORCED_PROCESS_EXIT_TIMEOUT_MS,
    )
    return null
  } catch (error) {
    return error
  }
}

export async function closeElectronWithDeadline({
  app,
  phase,
  diagnostics,
  emit = line => console.error(line),
  timeoutMs = 15_000,
  diagnosticTimeoutMs = 3_000,
}) {
  try {
    await withDeadline(() => app.close(), `${phase} Electron shutdown`, timeoutMs)
    return { closed: true, error: null }
  } catch (cause) {
    const snapshot = await boundedDiagnostics(diagnostics, diagnosticTimeoutMs)
    let child = null
    const processState = (() => {
      try {
        child = app.process()
        return {
          pid: child?.pid ?? null,
          exitCode: child?.exitCode ?? null,
          signalCode: child?.signalCode ?? null,
          killed: child?.killed ?? false,
        }
      } catch (error) {
        return { diagnosticError: error?.message || String(error) }
      }
    })()
    const detail = {
      event: 'desktop_e2e_electron_shutdown_failed',
      phase,
      timeoutMs,
      error: cause?.stack || cause?.message || String(cause),
      process: processState,
      diagnostics: snapshot,
    }
    emit(JSON.stringify(detail))
    const forcedExitError = child
      ? await forceElectronProcessExit(child, phase, emit)
      : new Error('Electron child process was unavailable for forced shutdown.')
    const error = new Error(
      `DESKTOP_E2E_ELECTRON_SHUTDOWN_FAILED: phase=${phase} `
      + `cause=${cause?.message || String(cause)} `
      + `forcedExit=${forcedExitError?.message || 'ok'} `
      + `diagnostics=${JSON.stringify(snapshot)}`,
      { cause },
    )
    return { closed: false, error }
  }
}
