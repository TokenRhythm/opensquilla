import { setTimeout as delay } from 'node:timers/promises'
import { terminateOwnedPosixProcessGroup } from './e2e-posix-process-group.mjs'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'
import {
  desktopGatewayStartIdentityConflict,
  desktopProcessStartIdentity,
} from '../dist/desktop-gateway-ownership.js'

// A recycled PID belongs to a different process, not a Gateway that failed to
// exit. Unknown identities never prove exit; a denied PID probe means present.
export function gatewayProcessSnapshot(record, {
  probePid = pid => process.kill(pid, 0),
  readStartIdentity = desktopProcessStartIdentity,
} = {}) {
  let pidPresent = true
  try {
    probePid(record.pid)
  } catch (error) {
    pidPresent = error?.code !== 'ESRCH'
  }
  const liveStartIdentity = pidPresent ? readStartIdentity(record.pid) : null
  const identityConflict = desktopGatewayStartIdentityConflict(
    record.start_identity, liveStartIdentity,
  )
  return {
    pid: record.pid,
    recordedStartIdentity: record.start_identity,
    liveStartIdentity,
    pidPresent,
    identityConflict,
    alive: pidPresent && !identityConflict,
  }
}

const FORCED_PROCESS_EXIT_TIMEOUT_MS = 5_000
const WINDOWS_PROCESS_TREE_KILL_TIMEOUT_MS = 5_000

function timeoutError(label, timeoutMs) {
  const error = new Error(`${label} timed out after ${timeoutMs}ms`)
  error.code = 'DESKTOP_E2E_SHUTDOWN_TIMEOUT'
  return error
}

export async function withDeadline(operation, label, timeoutMs) {
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

function observeElectronShutdown(app) {
  const startedAt = performance.now()
  const events = []
  const listeners = []
  let child = null
  let childError = null
  try { child = app.process() } catch (error) { childError = error?.message || String(error) }
  const state = () => {
    if (!child) return { diagnosticError: childError || 'Electron child process is unavailable.' }
    let pidPresent = null
    if (Number.isSafeInteger(child.pid) && child.pid > 0) {
      try { process.kill(child.pid, 0); pidPresent = true }
      catch (error) { if (error?.code === 'ESRCH') pidPresent = false }
    }
    return {
      pid: child.pid ?? null,
      exitCode: child.exitCode ?? null,
      signalCode: child.signalCode ?? null,
      killed: child.killed ?? false,
      // A PID probe is diagnostic only; absence never proves tree cleanup.
      pidPresent,
      stdio: Object.fromEntries(['stdin', 'stdout', 'stderr'].map(name => [name,
        child[name] ? {
          destroyed: child[name].destroyed,
          readableEnded: child[name].readableEnded,
          writableFinished: child[name].writableFinished,
        } : null,
      ])),
    }
  }
  const record = (event, details = {}) => {
    // These are one-shot lifecycle observations, never stdout or stderr data.
    if (events.length < 32) events.push({
      event, at: new Date().toISOString(), elapsedMs: performance.now() - startedAt, ...details,
    })
  }
  const listen = (target, event, listener) => {
    if (!target?.once) return
    target.once(event, listener)
    listeners.push(() => target.removeListener(event, listener))
  }
  listen(child, 'exit', (code, signal) => record('child-exit', { code, signal }))
  listen(child, 'close', (code, signal) => record('child-close', { code, signal }))
  for (const name of ['stdin', 'stdout', 'stderr']) {
    for (const event of name === 'stdin' ? ['finish', 'close'] : ['end', 'close']) {
      listen(child?.[name], event, () => record(`${name}-${event}`))
    }
  }
  record('close-start', { process: state() })
  return {
    child,
    state,
    record,
    snapshot: () => ({ process: state(), events: [...events] }),
    dispose: () => { for (const remove of listeners) remove() },
  }
}

async function forceElectronProcessExit(child, phase, emit, observation) {
  observation.record('force-start', { process: observation.state() })
  if (process.platform !== 'win32') {
    return terminateOwnedPosixProcessGroup(child, {
      timeoutMs: FORCED_PROCESS_EXIT_TIMEOUT_MS,
    })
  }
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
      onFailure: failure => {
        observation.record('tree-termination-failed', { ...failure, process: observation.state() })
        emit(JSON.stringify({
          event: 'desktop_e2e_process_tree_termination_failed',
          phase,
          ...failure,
        }))
      },
    })
    observation.record('tree-termination-result', { processTreeReaped })
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
  // Playwright's Windows process() is the shell wrapper. Its close protocol
  // waits for child 'close' (including stdio), not just the OS process 'exit'.
  // Capture the owned handle and events before close can dispose its dispatcher.
  const observation = observeElectronShutdown(app)
  try {
    await withDeadline(() => app.close(), `${phase} Electron shutdown`, timeoutMs)
    const child = observation.child
    if (!child || !Number.isSafeInteger(child.pid) || child.pid <= 0
      || child.exitCode !== 0 || child.signalCode !== null) {
      const error = new Error(`${phase} Electron close resolved without a clean process exit: `
        + `exitCode=${child?.exitCode ?? 'unknown'} signal=${child?.signalCode ?? 'none'}`)
      error.code = child && Number.isSafeInteger(child.pid) && child.pid > 0
        && ((Number.isInteger(child.exitCode) && child.exitCode !== 0) || typeof child.signalCode === 'string')
        ? 'DESKTOP_E2E_ABNORMAL_EXIT' : 'DESKTOP_E2E_UNVERIFIED_EXIT'
      throw error
    }
    return {
      closed: true,
      error: null,
      closeErrorCode: null,
      forcedExitSucceeded: false,
      processTreeReaped: false,
    }
  } catch (cause) {
    observation.record('close-failed', { process: observation.state() })
    const snapshot = await boundedDiagnostics(diagnostics, diagnosticTimeoutMs)
    observation.record('diagnostics-complete', { process: observation.state() })
    const child = observation.child
    const { pidPresent: _pidPresent, stdio: _stdio, ...processState } = observation.state()
    const detail = {
      event: 'desktop_e2e_electron_shutdown_failed',
      phase,
      timeoutMs,
      error: cause?.stack || cause?.message || String(cause),
      process: processState,
      diagnostics: snapshot,
      shutdownObservation: observation.snapshot(),
    }
    emit(JSON.stringify(detail))
    const forceNeeded = child && Number.isSafeInteger(child.pid) && child.pid > 0 && !childHasExited(child)
    const forcedExit = forceNeeded
      ? await forceElectronProcessExit(child, phase, emit, observation)
      : {
          error: null,
          processTreeReaped: false,
        }
    const forcedExitSucceeded = Boolean(forceNeeded && forcedExit.error === null)
    observation.record('cleanup-complete', { process: observation.state() })
    emit(JSON.stringify({
      event: 'desktop_e2e_electron_shutdown_cleanup',
      phase,
      forcedExitSucceeded,
      cleanupSkipped: forceNeeded ? null : child && childHasExited(child) ? 'already-exited' : 'unverified-child',
      processTreeReaped: forcedExit.processTreeReaped,
      error: forcedExit.error?.message || null,
      shutdownObservation: observation.snapshot(),
    }))
    const error = new Error(
      `DESKTOP_E2E_ELECTRON_SHUTDOWN_FAILED: phase=${phase} `
      + `cause=${cause?.message || String(cause)} `
      + `forcedExit=${forceNeeded ? forcedExit.error?.message || 'ok' : 'not-attempted'} `
      + `diagnostics=${JSON.stringify(snapshot)}`,
      { cause },
    )
    return {
      closed: false,
      error,
      closeErrorCode: typeof cause?.code === 'string' ? cause.code : null,
      forcedExitSucceeded,
      processTreeReaped: forcedExit.processTreeReaped,
      shutdownObservation: observation.snapshot(),
    }
  } finally {
    observation.dispose()
  }
}
