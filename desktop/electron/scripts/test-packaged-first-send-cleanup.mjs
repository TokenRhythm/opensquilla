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
  closeElectronAfterRemovingRoutes,
  electronProcessSnapshot,
  quitElectronOnNextTurn,
} from './packaged-first-send-cleanup.mjs'

const fixtureProcesses = []
const fixtureServers = []

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
  await quitElectronOnNextTurn({
    process: () => naturalWrapper.child,
    evaluate: async () => {
      naturalElectron.child.send('quit')
      naturalWrapper.child.send('quit')
    },
  }, { wrapperPid: naturalWrapper.child.pid, electronPid: naturalElectron.child.pid }, 5_000)
  assert.equal(naturalWrapper.child.exitCode, 0)
  await assertProcessExited(naturalElectron.child.pid)

  const exitedWrapper = await startChild()
  const liveElectron = await startChild()
  await assert.rejects(quitElectronOnNextTurn({
    process: () => exitedWrapper.child,
    evaluate: async () => {
      const exited = once(exitedWrapper.child, 'exit')
      exitedWrapper.child.send('quit')
      await exited
    },
  }, { wrapperPid: exitedWrapper.child.pid, electronPid: liveElectron.child.pid }, 25),
  /left an observed Electron or wrapper process alive/)
  assert.equal(liveElectron.child.exitCode, null, 'natural observation must never kill Electron')
  await assert.rejects(closeElectronAfterRemovingRoutes({
    process: () => exitedWrapper.child,
    context: () => ({ unrouteAll: async () => {} }),
    close: async () => {},
  }, { wrapperPid: exitedWrapper.child.pid, electronPid: liveElectron.child.pid }, 25),
  /left an observed Electron or wrapper process alive/)

  const unroutedWrapper = await startChild()
  const unroutedElectron = await startChild()
  let releaseRoute
  let closeCalled = false
  const removingRoutes = new Promise(resolve => { releaseRoute = resolve })
  const unroutedClose = closeElectronAfterRemovingRoutes({
    process: () => {
      assert.equal(closeCalled, false, 'Playwright process() is unavailable after close() disposes the app')
      return unroutedWrapper.child
    },
    context: () => ({ unrouteAll: options => {
      assert.deepEqual(options, { behavior: 'wait' })
      return removingRoutes
    } }),
    close: async () => {
      closeCalled = true
      unroutedElectron.child.send('quit')
      unroutedWrapper.child.send('quit')
    },
  }, { wrapperPid: unroutedWrapper.child.pid, electronPid: unroutedElectron.child.pid }, 5_000)
  await delay(25)
  assert.equal(closeCalled, false, 'quit must wait for outstanding route handlers')
  releaseRoute()
  await unroutedClose
  assert.equal(unroutedWrapper.child.exitCode, 0)
  await assertProcessExited(unroutedElectron.child.pid)

  const stuckRoute = await startChild()
  await assert.rejects(cleanupPackagedFirstSend({
    app: {
      process: () => stuckRoute.child,
      context: () => ({ unrouteAll: () => new Promise(() => {}) }),
      close: async () => { assert.fail('close must not run before route removal finishes') },
    },
    unrouteBeforeQuit: true,
    processIdentity: { wrapperPid: stuckRoute.child.pid, electronPid: stuckRoute.child.pid },
    electronTimeoutMs: 25,
    emit: () => {},
  }), error => {
    assert.equal(error.errors[0].cause.code, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
    return true
  })
  await assertProcessExited(stuckRoute.child.pid)

  const { child } = await startChild()
  const provider = await startProvider()
  const phases = []
  await cleanupPackagedFirstSend({
    app: {
      close: async () => {
        const exited = once(child, 'exit')
        child.kill('SIGTERM')
        await exited
      },
      process: () => child,
    },
    provider,
    onPhase: (phase, details) => phases.push({ phase, ...details }),
    electronTimeoutMs: 5_000,
    providerTimeoutMs: 100,
  })
  assert.equal(provider.server.listening, false)
  assert.equal(phases.find(event => event.phase === 'electron-cleanup-complete').closed, true)
  assert.equal(phases.find(event => event.phase === 'electron-cleanup-complete').forcedExitSucceeded, false)

  const hanging = await startChild(process.platform === 'win32')
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
    provider: hangingProvider,
    electronTimeoutMs: 25,
    providerTimeoutMs: 100,
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
  assert.equal(forced.forcedExitSucceeded, true, 'even successful containment must fail this gate')
  assert.equal(hangingProvider.server.listening, false, 'provider cleanup must run after Electron failure')
  assert.equal(shutdownLogs[0].process.pid, hanging.child.pid)
  await assertProcessExited(hanging.child.pid)
  assert.equal(electronProcessSnapshot(identity).wrapperPidExists, false)
  assert.equal(electronProcessSnapshot(identity).electronPidExists, true)
  if (hanging.descendantPid) await assertProcessExited(hanging.descendantPid)
  assert.equal(unaffected.child.exitCode, null, 'cleanup must not kill another Node process')
  assert.equal(unaffected.child.signalCode, null)

  const activeProvider = await startProvider()
  const activeSocket = await startActiveRequest(activeProvider)
  await assert.rejects(cleanupPackagedFirstSend({
    app: { close: async () => {} },
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
  await assert.rejects(cleanupPackagedFirstSend({
    app: { close: async () => { throw new Error('Synthetic close rejection') }, process: () => null },
    provider: rejectedProvider,
    emit: () => {},
    providerTimeoutMs: 100,
  }), AggregateError)
  assert.equal(rejectedProvider.server.listening, false)
  console.log('Packaged first-send cleanup checks passed: graceful, deferred/unroute natural exit, hung routes and Electron tree, active HTTP, close rejection')
} finally {
  for (const { server, sockets } of fixtureServers) {
    server.closeAllConnections?.()
    for (const socket of sockets) socket.destroy()
    server.close()
  }
  for (const child of fixtureProcesses) {
    try {
      if (child.exitCode === undefined || (child.exitCode === null && child.signalCode === null)) {
        process.kill(child.pid, 'SIGKILL')
      }
    } catch (error) {
      if (error.code !== 'ESRCH') throw error
    }
  }
}
