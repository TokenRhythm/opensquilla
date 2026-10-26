import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { createConnection } from 'node:net'
import { setTimeout as delay } from 'node:timers/promises'

import {
  closeHttpServerWithDeadline,
  trackHttpServerConnections,
} from './e2e-shutdown-helpers.mjs'
import {
  captureElectronProcessIdentity,
  captureFirstSendDiagnostic,
  cleanupPackagedFirstSend,
  closeElectronAndObserveExit,
  electronProcessSnapshot,
  expectedShutdownCancellationIndices,
  waitForFirstSendGatewayIdle,
} from './packaged-first-send-cleanup.mjs'

const quitStart = { event: 'before_quit', gatewayDrainInFlight: false }
const quitAccepted = { event: 'quit_gateway_shutdown_requested', accepted: true, alreadyStopping: false }
const quitExited = { event: 'quit_gateway_exit', exited: true, hardTerminated: false }
const cancelledDirectory = {
  event: 'renderer_console', level: 'error',
  message: '[useSessions] session directory error: Connection closed',
}
const normalQuit = [quitStart, quitAccepted, cancelledDirectory, quitExited]
const quitLogCases = [
  ['confirmed normal shutdown', normalQuit, [2], 0],
  ['multiple exact cancellations', [quitStart, quitAccepted, cancelledDirectory, cancelledDirectory, quitExited], [2, 3], 0],
  ['no cancellation', [quitStart, quitAccepted, quitExited], [], 0],
  ['business phase', [cancelledDirectory, quitStart, quitAccepted, quitExited], [], 1],
  ['before shutdown acceptance', [quitStart, cancelledDirectory, quitAccepted, quitExited], [], 1],
  ['after Gateway exit', [quitStart, quitAccepted, quitExited, cancelledDirectory], [], 1],
  ['business error alongside valid cancellation', [cancelledDirectory, ...normalQuit], [3], 1],
  ['missing before_quit', normalQuit.slice(1), [], 1],
  ['missing acceptance', [quitStart, cancelledDirectory, quitExited], [], 1],
  ['missing exit', normalQuit.slice(0, -1), [], 1],
  ['out of order acceptance', [quitAccepted, quitStart, cancelledDirectory, quitExited], [], 1],
  ['second quit attempt', [quitStart, quitAccepted, cancelledDirectory, quitStart, quitExited], [], 1],
  ['second accepted shutdown', [quitStart, quitAccepted, cancelledDirectory, quitAccepted, quitExited], [], 1],
  ['second Gateway exit', [...normalQuit, quitExited], [], 1],
  ['separate completed attempts', [...normalQuit, ...normalQuit], [], 2],
  ...[false, null, 'true', 1, undefined].map(accepted => [
    `acceptance must be boolean true: ${accepted}`,
    [quitStart, { ...quitAccepted, accepted }, cancelledDirectory, quitExited], [], 1,
  ]),
  ...[false, null, 'true', 1, undefined].map(exited => [
    `exit must be boolean true: ${exited}`,
    [quitStart, quitAccepted, cancelledDirectory, { ...quitExited, exited }], [], 1,
  ]),
  ...[true, null, 'false', 0, undefined].map(hardTerminated => [
    `natural exit must be boolean false: ${hardTerminated}`,
    [quitStart, quitAccepted, cancelledDirectory, { ...quitExited, hardTerminated }], [], 1,
  ]),
  ...['quit_gateway_drain_failed', 'quit_gateway_still_running', 'renderer_unresponsive',
    'renderer_process_gone', 'renderer_console_suppressed'].map(event => [
    `intermediate failure: ${event}`,
    [quitStart, quitAccepted, cancelledDirectory, { event }, quitExited], [], 1,
  ]),
  ['quit returned to running', [quitStart, quitAccepted, cancelledDirectory,
    { event: 'desktop_exit_phase', to: 'running' }, quitExited], [], 1],
  ['malformed record in interval', [quitStart, quitAccepted, cancelledDirectory, null, quitExited], [], 1],
  ['other renderer failure', [quitStart, quitAccepted, cancelledDirectory,
    { ...cancelledDirectory, message: 'TypeError: synthetic rendering failure' }, quitExited], [], 2],
  ['forbidden failure', [quitStart, quitAccepted, cancelledDirectory,
    { ...cancelledDirectory, message: '[ErrorBoundary] synthetic failure' }, quitExited], [], 2],
  ['different error text', [quitStart, quitAccepted,
    { ...cancelledDirectory, message: 'Connection closed' }, quitExited], [], 1],
  ['extra error text', [quitStart, quitAccepted,
    { ...cancelledDirectory, message: `${cancelledDirectory.message} unexpectedly` }, quitExited], [], 1],
  ['different console level', [quitStart, quitAccepted,
    { ...cancelledDirectory, level: 'warn' }, quitExited], [], 1],
]
for (const [name, records, expectedIndices, unexpectedCount] of quitLogCases) {
  const expected = expectedShutdownCancellationIndices(records)
  assert.deepEqual([...expected], expectedIndices, name)
  assert.equal(records.filter(record => record?.event === 'renderer_console').length
    - expected.size, unexpectedCount, `${name}: remaining errors`)
}
console.log(`First-send shutdown log classification checks passed: ${quitLogCases.length} synthetic cases`)

const idleCounts = { running: 0, queued: 0, waiting: 0, reservations: 0, auxiliary: 0 }
const idleActivity = { kind: 'ok', activeCount: 0, activity: idleCounts }
const fingerprint = 'a'.repeat(64)
const idleOwner = { profile_fingerprint: fingerprint, port: 18791, instance_nonce: 'do-not-log-owner-secret' }
const connection = { status: 'ready', profileFingerprint: fingerprint,
  protocol: 'http:', hostname: '127.0.0.1', port: 18791 }
function idleHarness(overrides = {}) {
  let now = 0
  let reads = 0
  const samples = []
  const timeouts = []
  return {
    options: {
      page: { evaluate: async () => connection }, userDataDir: 'isolated-profile', deadline: 1_000,
      now: () => now, sleep: async ms => { now += ms }, onSample: sample => samples.push(sample),
      loadOwnership: () => ({ status: 'valid', record: idleOwner }),
      readActivity: async (record, options) => {
        assert.equal(record, idleOwner, 'do not switch the authenticated owner while waiting')
        assert.ok(options.timeoutMs > 0 && options.timeoutMs <= 1_000 - now)
        timeouts.push(options.timeoutMs)
        reads++
        return idleActivity
      },
      ...overrides,
    },
    samples, timeouts, reads: () => reads, setNow: value => { now = value },
  }
}

// Before: the UI has finished but immediate Quit sees active work and opens a
// confirmation that this unattended fixture never answers. After: the same
// completion path waits for signed idle and then requests ordinary Quit once.
{
  let activity = { kind: 'ok', activeCount: 1, activity: { ...idleCounts, running: 1 } }
  const quit = () => activity.activeCount > 0 ? 'confirmation_required' : 'natural_exit'
  assert.equal(quit(), 'confirmation_required')
  let releasePoll
  let waiting
  let quitCalls = 0
  const waitingForPoll = () => new Promise(resolve => { waiting = resolve })
  const firstPoll = waitingForPoll()
  const h = idleHarness({
    readActivity: async record => { assert.equal(record, idleOwner); return activity },
    sleep: () => new Promise(resolve => { releasePoll = resolve; waiting() }),
  })
  const completion = waitForFirstSendGatewayIdle(h.options).then(() => { quitCalls++; return quit() })
  await firstPoll
  assert.equal(quitCalls, 0, 'a rendered terminal answer does not bypass driver settlement')
  activity = { kind: 'ok', activeCount: 1, activity: { ...idleCounts, auxiliary: 1 } }
  const secondPoll = waitingForPoll()
  releasePoll()
  await secondPoll
  assert.equal(quitCalls, 0, 'auxiliary completion is still active work')
  activity = idleActivity
  releasePoll()
  assert.equal(await completion, 'natural_exit')
  assert.equal(quitCalls, 1)
  assert.deepEqual(h.samples.map(sample => sample.activity), [
    { ...idleCounts, running: 1 }, { ...idleCounts, auxiliary: 1 }, idleCounts,
  ])
  assert.ok(!JSON.stringify(h.samples).includes(idleOwner.instance_nonce))
}
for (const kind of ['running', 'auxiliary', 'unreachable', 'late-idle']) {
  const h = idleHarness()
  h.options.readActivity = async () => {
    if (kind === 'late-idle') { h.setNow(1_000); return idleActivity }
    return kind === 'unreachable' ? { kind } : {
      kind: 'ok', activeCount: 1, activity: { ...idleCounts, [kind]: 1 },
    }
  }
  await assert.rejects(waitForFirstSendGatewayIdle(h.options), error => error.reason === 'completion_deadline')
  assert.ok(h.samples.length > 0, 'deadline failure retains the last authenticated activity category')
}
for (const result of [
  { kind: 'rejected' }, { kind: 'unsupported' }, { kind: 'ok', activeCount: 0 },
  { ...idleActivity, activeCount: 1 },
  { ...idleActivity, activity: { ...idleCounts, auxiliary: undefined } },
]) {
  const h = idleHarness({ readActivity: async () => result,
    sleep: async () => assert.fail('invalid/unauthenticated activity must fail immediately') })
  await assert.rejects(waitForFirstSendGatewayIdle(h.options), error =>
    ['rejected', 'unsupported', 'invalid_activity'].includes(error.reason))
}
for (const wrong of [
  { status: 'missing' },
  { status: 'valid', record: { ...idleOwner, port: 18792 } },
  { status: 'valid', record: { ...idleOwner, profile_fingerprint: 'b'.repeat(64) } },
]) {
  const h = idleHarness({ loadOwnership: () => wrong })
  await assert.rejects(waitForFirstSendGatewayIdle(h.options), error => error.reason === 'ownership_mismatch')
  assert.equal(h.reads(), 0)
}
{
  const h = idleHarness({ deadline: 0,
    page: { evaluate: async () => assert.fail('expired completion must not start a descriptor request') } })
  await assert.rejects(waitForFirstSendGatewayIdle(h.options), error => error.reason === 'completion_deadline')
  assert.equal(h.reads(), 0)
}
{
  const h = idleHarness({ page: { evaluate: async () => ({ ...connection, hostname: 'unrelated.example' }) } })
  await assert.rejects(waitForFirstSendGatewayIdle(h.options), error => error.reason === 'invalid_connection')
  assert.equal(h.reads(), 0)
}
{
  const h = idleHarness({ page: { evaluate: () => new Promise(() => {}) }, deadline: Date.now() + 25, now: Date.now })
  await assert.rejects(waitForFirstSendGatewayIdle(h.options), error => error.reason === 'invalid_connection')
  assert.equal(h.reads(), 0)
}
console.log('First-send signed-idle checks passed: active-before-quit, driver/auxiliary settlement, deadline and owner fences')

const fixtureProcesses = []
const fixtureServers = []
let refusedCleanupChild = null

async function startChild(withDescendant = false) {
  const source = `
    const { spawn } = require('node:child_process');
    const descendant = ${withDescendant} ? spawn(process.execPath,
      ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore', windowsHide: true }) : null;
    process.send({ descendantPid: descendant?.pid || null });
    process.on('message', message => { if (message === 'quit') process.exit(0); });
    setInterval(() => {}, 1000);
  `
  const child = spawn(process.execPath, ['-e', source], {
    stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
    detached: process.platform !== 'win32',
    windowsHide: true,
  })
  fixtureProcesses.push(child)
  const [message] = await once(child, 'message', { signal: AbortSignal.timeout(5_000) })
  if (message.descendantPid) fixtureProcesses.push({ pid: message.descendantPid })
  return { child, descendantPid: message.descendantPid }
}

async function startProvider() {
  // Deliberately leave requests unfinished so shutdown must handle active
  // sockets, rather than only the ordinary keep-alive/idle connection case.
  const server = createServer(() => {})
  const sockets = trackHttpServerConnections(server)
  fixtureServers.push({ server, sockets })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  return {
    server,
    sockets,
    close: options => closeHttpServerWithDeadline(server, sockets, options),
  }
}

async function startActiveRequest(provider) {
  const request = once(provider.server, 'request', { signal: AbortSignal.timeout(5_000) })
  const socket = createConnection(provider.server.address().port, '127.0.0.1')
  socket.on('error', () => {})
  await once(socket, 'connect', { signal: AbortSignal.timeout(5_000) })
  socket.write('GET /synthetic HTTP/1.1\r\nHost: localhost\r\n\r\n')
  await request
  return socket
}

async function assertProcessExited(pid) {
  const deadline = Date.now() + 5_000
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0)
    } catch (error) {
      if (error.code === 'ESRCH') return
      throw error
    }
    await delay(25)
  }
  assert.fail(`Synthetic child ${pid} was not reaped`)
}

try {
  const naturalWrapper = await startChild()
  const naturalElectron = await startChild()
  await closeElectronAndObserveExit({
    process: () => naturalWrapper.child,
    close: async () => {
      naturalElectron.child.send('quit')
      naturalWrapper.child.send('quit')
    },
  }, { wrapperPid: naturalWrapper.child.pid, electronPid: naturalElectron.child.pid }, 5_000)
  assert.equal(naturalWrapper.child.exitCode, 0)
  await assertProcessExited(naturalElectron.child.pid)

  const exitedWrapper = await startChild()
  const liveElectron = await startChild()
  await assert.rejects(closeElectronAndObserveExit({
    process: () => exitedWrapper.child,
    close: async () => {
      const exited = once(exitedWrapper.child, 'exit')
      exitedWrapper.child.send('quit')
      await exited
    },
  }, { wrapperPid: exitedWrapper.child.pid, electronPid: liveElectron.child.pid }, 25),
  /left an observed Electron or wrapper process alive/)
  assert.equal(liveElectron.child.exitCode, null, 'natural observation must never kill Electron')
  await assert.rejects(closeElectronAndObserveExit({
    process: () => exitedWrapper.child,
    close: async () => {},
  }, { wrapperPid: exitedWrapper.child.pid, electronPid: liveElectron.child.pid }, 25),
  /left an observed Electron or wrapper process alive/)
  const { child } = await startChild()
  const defaultElectron = await startChild()
  const provider = await startProvider()
  const phases = []
  let defaultCloseCalled = false
  await cleanupPackagedFirstSend({
    app: {
      close: async () => {
        defaultCloseCalled = true
        const exited = once(child, 'exit')
        defaultElectron.child.send('quit')
        child.send('quit')
        await exited
      },
      process: () => {
        assert.equal(defaultCloseCalled, false, 'default close must retain the child before dispatcher disposal')
        return child
      },
    },
    processIdentity: { wrapperPid: child.pid, electronPid: defaultElectron.child.pid },
    provider,
    onPhase: (phase, details) => phases.push({ phase, ...details }),
    electronTimeoutMs: 5_000,
    providerTimeoutMs: 100,
  })
  assert.equal(provider.server.listening, false)
  assert.equal(phases.find(event => event.phase === 'electron-cleanup-complete').closed, true)
  assert.equal(phases.find(event => event.phase === 'electron-cleanup-complete').forcedExitSucceeded, false)
  assert.equal(child.exitCode, 0)
  await assertProcessExited(defaultElectron.child.pid)

  const signalled = await startChild()
  await assert.rejects(closeElectronAndObserveExit({
    process: () => signalled.child,
    close: async () => {
      const exited = once(signalled.child, 'exit')
      signalled.child.kill('SIGTERM')
      await exited
    },
  }, { wrapperPid: signalled.child.pid, electronPid: signalled.child.pid }, 5_000),
  /did not produce a natural zero exit code/)

  const hanging = await startChild(process.platform === 'win32')
  const supportsOwnedTreeReaping = ['win32', 'linux'].includes(process.platform)
  if (!supportsOwnedTreeReaping) refusedCleanupChild = hanging.child
  const unaffected = await startChild()
  const identity = await captureElectronProcessIdentity({
    process: () => hanging.child,
    evaluate: async () => unaffected.child.pid,
  })
  assert.deepEqual(electronProcessSnapshot(identity), {
    wrapperPid: hanging.child.pid,
    electronPid: unaffected.child.pid,
    wrapperPidExists: true,
    electronPidExists: true,
  })
  const unavailableIdentity = await captureElectronProcessIdentity({
    process: () => hanging.child,
    evaluate: () => new Promise(() => {}),
  }, 25)
  assert.equal(unavailableIdentity.wrapperPid, hanging.child.pid)
  assert.equal(unavailableIdentity.electronPid, null)
  assert.match(unavailableIdentity.diagnosticError, /timed out after 25ms/)
  assert.match((await captureFirstSendDiagnostic(() => {
    throw new Error('Synthetic diagnostic failure')
  })).diagnosticError, /Synthetic diagnostic failure/)
  const hangingProvider = await startProvider()
  const hangingPhases = []
  const shutdownLogs = []
  await assert.rejects(cleanupPackagedFirstSend({
    app: { close: () => new Promise(() => {}), process: () => hanging.child },
    processIdentity: { wrapperPid: hanging.child.pid, electronPid: hanging.child.pid },
    provider: hangingProvider,
    electronTimeoutMs: 25,
    providerTimeoutMs: 100,
    diagnostics: () => ({ ownedFixture: true }),
    emit: line => shutdownLogs.push(JSON.parse(line)),
    onPhase: (phase, details) => hangingPhases.push({ phase, ...details }),
  }), error => {
    assert.ok(error instanceof AggregateError)
    assert.equal(error.errors.length, 1)
    assert.equal(error.errors[0].cause.code, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
    return true
  })
  const forced = hangingPhases.find(event => event.phase === 'electron-cleanup-complete')
  assert.equal(forced.closed, false)
  assert.equal(forced.forcedExitSucceeded, supportsOwnedTreeReaping,
    'successful containment or refusal must preserve the shutdown failure')
  assert.equal(hangingProvider.server.listening, false, 'provider cleanup must run after Electron failure')
  assert.equal(shutdownLogs[0].process.pid, hanging.child.pid)
  assert.equal(shutdownLogs[0].diagnostics.ownedFixture, true)
  if (supportsOwnedTreeReaping) {
    await assertProcessExited(hanging.child.pid)
    assert.equal(electronProcessSnapshot(identity).wrapperPidExists, false)
  } else {
    assert.equal(hanging.child.exitCode, null, 'unverified containment must leave the child alive')
    assert.equal(hanging.child.signalCode, null)
    assert.equal(hanging.child.killed, false)
    assert.equal(electronProcessSnapshot(identity).wrapperPidExists, true)
  }
  assert.equal(electronProcessSnapshot(identity).electronPidExists, true)
  if (hanging.descendantPid) await assertProcessExited(hanging.descendantPid)
  assert.equal(unaffected.child.exitCode, null, 'cleanup must not kill another Node process')
  assert.equal(unaffected.child.signalCode, null)

  const activeProvider = await startProvider()
  const activeSocket = await startActiveRequest(activeProvider)
  await assert.rejects(cleanupPackagedFirstSend({
    provider: activeProvider,
    providerTimeoutMs: 25,
  }), error => {
    assert.ok(error instanceof AggregateError)
    assert.equal(error.errors.length, 1)
    assert.equal(error.errors[0].code, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
    return true
  })
  assert.equal(activeProvider.server.listening, false)
  assert.equal(activeProvider.sockets.size, 0)
  activeSocket.destroy()

  const rejectedProvider = await startProvider()
  const rejectedChild = await startChild()
  await assert.rejects(cleanupPackagedFirstSend({
    app: {
      close: async () => { throw new Error('Synthetic close rejection') },
      process: () => rejectedChild.child,
    },
    processIdentity: { wrapperPid: rejectedChild.child.pid, electronPid: rejectedChild.child.pid },
    provider: rejectedProvider,
    emit: () => {},
    providerTimeoutMs: 100,
  }), error => {
    assert.ok(error instanceof AggregateError)
    assert.equal(error.errors[0].cause.message, 'Synthetic close rejection')
    return true
  })
  assert.equal(rejectedProvider.server.listening, false)
  console.log('Packaged first-send cleanup checks passed: natural dual-PID zero exit, surviving Electron rejection, hung Electron tree, active HTTP, close rejection')
} finally {
  for (const { server, sockets } of fixtureServers) {
    server.closeAllConnections?.()
    for (const socket of sockets) socket.destroy()
    server.close()
  }
  for (const child of fixtureProcesses) {
    try {
      if (child.exitCode === undefined || (child.exitCode === null && child.signalCode === null)) {
        const refusedChildClosed = child === refusedCleanupChild
          ? once(child, 'close', { signal: AbortSignal.timeout(5_000) }) : null
        // Retained direct children include the refused macOS fixture. The
        // existing PID-only entry is the Windows descendant checked above.
        if (typeof child.kill === 'function') child.kill('SIGKILL')
        else process.kill(child.pid, 'SIGKILL')
        if (refusedChildClosed) await refusedChildClosed
      }
    } catch (error) {
      if (error.code !== 'ESRCH') throw error
    }
  }
}

if (refusedCleanupChild) {
  // Refusal is not tree-reaping proof. Verify this child only after our own
  // fixture finally, which holds its ChildProcess and creates no descendants.
  await assertProcessExited(refusedCleanupChild.pid)
  assert.equal(refusedCleanupChild.signalCode, 'SIGKILL')
}
