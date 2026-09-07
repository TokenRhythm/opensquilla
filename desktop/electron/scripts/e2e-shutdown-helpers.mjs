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

export function desktopShutdownEvidenceSince(checkpoint, current) {
  if (typeof checkpoint !== 'string' || typeof current !== 'string') {
    return { gatewayExitLogged: false, committedExitLogged: false }
  }
  if (!current.startsWith(checkpoint)) {
    return { gatewayExitLogged: false, committedExitLogged: false }
  }

  let gatewayExitCount = 0
  let allGatewayExitsClean = true
  let lastGatewayExitIndex = -1
  let committedExitIndex = -1
  let recordIndex = 0
  for (const line of current.slice(checkpoint.length).split(/\r?\n/)) {
    if (!line.trim()) continue
    let record
    try {
      record = JSON.parse(line)
    } catch {
      continue
    }
    if (record?.event === 'quit_gateway_exit') {
      gatewayExitCount += 1
      lastGatewayExitIndex = recordIndex
      if (!(record.exited === true && record.hardTerminated === false)) {
        allGatewayExitsClean = false
      }
    }
    if (
      committedExitIndex === -1
      && record?.event === 'desktop_exit_phase'
      && record.to === 'committed'
      && record.reason === 'all lifecycle-owned Gateways exited'
    ) {
      committedExitIndex = recordIndex
    }
    recordIndex += 1
  }
  const gatewayExitLogged = gatewayExitCount > 0 && allGatewayExitsClean
  const committedExitLogged = gatewayExitLogged
    && committedExitIndex > lastGatewayExitIndex
  return { gatewayExitLogged, committedExitLogged }
}

export function canAcceptWindowsElectronShutdownFallback({
  platform = process.platform,
  shutdown,
  gatewayExitLogged,
  committedExitLogged,
}) {
  return platform === 'win32'
    && shutdown?.closed === false
    && shutdown?.closeErrorCode === 'DESKTOP_E2E_SHUTDOWN_TIMEOUT'
    && shutdown?.forcedExitSucceeded === true
    && shutdown?.processTreeReaped === true
    && gatewayExitLogged === true
    && committedExitLogged === true
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
  if (childHasExited(child)) {
    const processTreeReaped = process.platform !== 'win32'
    return {
      error: processTreeReaped
        ? null
        : new Error(`${phase} Windows process tree exited before reaping could be verified.`),
      processTreeReaped,
    }
  }
  const exited = new Promise(resolve => child.once('exit', resolve))
  let processTreeReaped = process.platform !== 'win32'
  if (process.platform === 'win32' && child.pid) {
    processTreeReaped = await terminateWindowsProcessTree({
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
      return { error, processTreeReaped: false }
    }
  }
  try {
    await withDeadline(
      () => exited,
      `${phase} forced process exit`,
      FORCED_PROCESS_EXIT_TIMEOUT_MS,
    )
  } catch (error) {
    return { error, processTreeReaped }
  }
  if (process.platform === 'win32' && !processTreeReaped) {
    return {
      error: new Error(`${phase} Windows process tree could not be proven reaped.`),
      processTreeReaped: false,
    }
  }
  return { error: null, processTreeReaped }
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
    return {
      closed: true,
      error: null,
      closeErrorCode: null,
      forcedExitSucceeded: false,
      processTreeReaped: false,
    }
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
    const forcedExit = child
      ? await forceElectronProcessExit(child, phase, emit)
      : {
          error: new Error('Electron child process was unavailable for forced shutdown.'),
          processTreeReaped: false,
        }
    const error = new Error(
      `DESKTOP_E2E_ELECTRON_SHUTDOWN_FAILED: phase=${phase} `
      + `cause=${cause?.message || String(cause)} `
      + `forcedExit=${forcedExit.error?.message || 'ok'} `
      + `diagnostics=${JSON.stringify(snapshot)}`,
      { cause },
    )
    return {
      closed: false,
      error,
      closeErrorCode: typeof cause?.code === 'string' ? cause.code : null,
      forcedExitSucceeded: child !== null && forcedExit.error === null,
      processTreeReaped: forcedExit.processTreeReaped,
    }
  }
}
