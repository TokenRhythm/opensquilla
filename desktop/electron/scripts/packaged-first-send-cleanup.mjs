import { setTimeout as delay } from 'node:timers/promises'
import { join } from 'node:path'

// Preserve the production Gateway's shutdown request, 80s exit observation,
// and 6s + 5s hard-kill backstops without changing any interaction budget.
const ELECTRON_CLEANUP_TIMEOUT_MS = 100_000
const PROVIDER_CLEANUP_TIMEOUT_MS = 15_000

/** A terminal renderer receipt can precede the Gateway's driver/auxiliary settlement. */
export async function waitForFirstSendGatewayIdle({
  page, userDataDir, deadline, onSample = () => {},
  now = Date.now, sleep = delay,
  loadOwnership, readActivity,
}) {
  const started = now()
  const fail = reason => {
    const error = new Error(`First-send Gateway idle verification failed: ${reason}`)
    error.code = 'FIRST_SEND_GATEWAY_NOT_IDLE'
    error.reason = reason
    throw error
  }
  if (!Number.isFinite(deadline) || now() >= deadline) fail('completion_deadline')
  const connection = await captureFirstSendDiagnostic(() => page.evaluate(async () => {
    const descriptor = await window.opensquillaDesktop.getGatewayConnection()
    const address = new URL(descriptor.httpUrl)
    return { status: descriptor.status, profileFingerprint: descriptor.profileFingerprint,
      protocol: address.protocol, hostname: address.hostname, port: Number(address.port) }
  }), Math.max(1, deadline - now()))
  if (connection?.diagnosticError || connection?.status !== 'ready'
    || !/^[0-9a-f]{64}$/.test(connection.profileFingerprint)
    || connection.protocol !== 'http:'
    || !['127.0.0.1', 'localhost', '[::1]'].includes(connection.hostname)
    || !Number.isSafeInteger(connection.port) || connection.port < 1) fail('invalid_connection')
  // This read-only endpoint authenticates the exact owned instance. Never use
  // renderer run-status or an unauthenticated health response as idle proof.
  const ownership = loadOwnership && readActivity ? null
    : await import('../dist/desktop-gateway-ownership.js')
  const loaded = (loadOwnership ?? ownership.loadDesktopGatewayOwnershipRecord)(
    join(userDataDir, 'gateway-ownership', connection.profileFingerprint),
  )
  if (loaded.status !== 'valid' || loaded.record.profile_fingerprint !== connection.profileFingerprint
    || loaded.record.port !== connection.port) fail('ownership_mismatch')
  const read = readActivity ?? ownership.readVerifiedDesktopGatewayActivity
  let polls = 0
  const countKeys = ['running', 'queued', 'waiting', 'reservations', 'auxiliary']
  while (now() < deadline) {
    const result = await read(loaded.record, { timeoutMs: Math.max(1, Math.min(1_000, deadline - now())) })
    polls += 1
    if (!result || !['ok', 'unreachable', 'rejected', 'unsupported'].includes(result.kind)) fail('invalid_activity')
    let activity
    if (result.kind === 'ok') {
      if (!result.activity || countKeys.some(key => !Number.isSafeInteger(result.activity[key])
        || result.activity[key] < 0)
        || result.activeCount !== countKeys.reduce((sum, key) => sum + result.activity[key], 0)) {
        fail('invalid_activity')
      }
      activity = Object.fromEntries(countKeys.map(key => [key, result.activity[key]]))
    }
    const sample = { polls, waitElapsedMs: Math.max(0, now() - started), kind: result.kind,
      ...(activity ? { activeCount: result.activeCount, activity } : {}) }
    onSample(sample)
    if (result.kind === 'rejected' || result.kind === 'unsupported') fail(result.kind)
    if (now() < deadline && result.kind === 'ok' && result.activeCount === 0) return sample
    await sleep(Math.max(0, Math.min(200, deadline - now())))
  }
  fail('completion_deadline')
}

export function expectedShutdownCancellationIndices(records) {
  const positions = ['before_quit', 'quit_gateway_shutdown_requested', 'quit_gateway_exit']
    .map(event => records.flatMap((record, index) => record?.event === event ? [index] : []))
  // Multiple or incomplete quit attempts cannot establish one cancellation interval.
  if (positions.some(indices => indices.length !== 1)) return new Set()
  const [[before], [accepted], [exited]] = positions
  if (!(before < accepted && accepted < exited)
    || records[accepted].accepted !== true
    || records[exited].exited !== true
    || records[exited].hardTerminated !== false) return new Set()

  const expected = new Set()
  for (let index = before; index <= exited; index += 1) {
    const record = records[index]
    if (!record || record.event === 'quit_gateway_drain_failed'
      || record.event === 'quit_gateway_still_running'
      || record.event === 'renderer_unresponsive'
      || record.event === 'renderer_process_gone'
      || record.event === 'renderer_console_suppressed'
      || (record.event === 'desktop_exit_phase' && record.to === 'running')) return new Set()
    if (record.event === 'renderer_console') {
      if (index > accepted && index < exited && record.level === 'error'
        && record.message === '[useSessions] session directory error: Connection closed') {
        expected.add(index)
      } else {
        return new Set()
      }
    }
  }
  return expected
}

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
  // The log classifier is also consumed before a desktop build exists.
  // Load process shutdown helpers only when an actual cleanup is requested.
  const { closeElectronWithDeadline } = await import('./e2e-shutdown-helpers.mjs')
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
