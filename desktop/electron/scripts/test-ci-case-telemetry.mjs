import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { EventEmitter, getEventListeners, once } from 'node:events'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createConnection } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'

import {
  runCommandWithTelemetry,
  startCaseTelemetry,
} from './ci-case-telemetry.mjs'
import {
  canAcceptWindowsElectronShutdownFallback,
  closeElectronWithDeadline,
  closeHttpServerWithDeadline,
  desktopShutdownEvidenceSince,
  gatewayProcessSnapshot,
  requestElectronQuitWithConfirmation,
  trackHttpServerConnections,
} from './e2e-shutdown-helpers.mjs'
import { buildDesktopQuitDialogHtml, parseDesktopQuitDialogResponse } from '../dist/desktop-quit-dialog.js'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'
import { captureFirstSendDiagnostic } from './packaged-first-send-cleanup.mjs'

import { readLinuxProcessStat } from './e2e-posix-process-group.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const helperPath = join(scriptDir, 'ci-case-telemetry.mjs')
const root = await mkdtemp(join(tmpdir(), 'opensquilla-ci-case-telemetry-'))
const outputPath = join(root, 'reports', 'cases.jsonl')
const emitted = []

try {
  // Exercise the actual quit document's click handler without Electron. Only
  // this explicit request callback may accept the expected interruption prompt.
  for (const { mode, allowExitWithoutConfirmation = false } of [
    ...['confirm', 'missing-window', 'missing-exit', 'early-exit', 'already-exited', 'wrong-window', 'click-failed']
      .map(mode => ({ mode })),
    ...['confirm', 'early-exit', 'missing-exit', 'early-exit-evaluate-closed', 'missing-exit-evaluate-closed', 'window-before-exit']
      .map(mode => ({ mode, allowExitWithoutConfirmation: true })),
  ]) {
    const calls = []
    const child = new EventEmitter()
    Object.assign(child, { pid: process.pid, exitCode: mode === 'already-exited' ? 0 : null, signalCode: null })
    const app = new EventEmitter()
    app.process = () => child
    app.close = () => assert.fail('expected confirmation must precede Playwright app.close')
    const listeners = new Map()
    const html = buildDesktopQuitDialogHtml({
      title: 'Synthetic quit', message: 'Synthetic unknown activity', detail: 'Synthetic cleanup',
      keepRunning: 'Keep running', confirm: 'Stop tasks and quit',
    })
    // This repository-owned template has one fixed inline script. Reject a
    // changed template shape instead of parsing or filtering arbitrary HTML.
    const scriptStart = html.split('<script>')
    assert.equal(scriptStart.length, 2, 'the generated quit document must have one script')
    const scriptEnd = scriptStart[1].split('</script>')
    assert.equal(scriptEnd.length, 2, 'the generated quit script must have a closing tag')
    const buttons = new Map(['keep', 'confirm'].map(id => [id, {
      addEventListener: (event, callback) => listeners.set(`${id}:${event}`, callback),
      focus() {}, disabled: false,
    }]))
    runInNewContext(scriptEnd[0], {
      document: {
        getElementById: id => buttons.get(id) ?? null,
        querySelectorAll: selector => { assert.equal(selector, 'button'); return [...buttons.values()] },
      },
      window: {
        addEventListener() {},
        opensquillaDesktop: { quitDialogRespond: response => {
          assert.equal(response.remember, false)
          calls.push(`ipc:${parseDesktopQuitDialogResponse(response)}`)
          if (mode !== 'missing-exit') {
            child.exitCode = 0
            child.emit('exit', 0)
          }
        } },
      },
    })
    const page = {
      url: () => mode === 'wrong-window' ? 'opensquilla-app://desktop/' : 'data:text/html;charset=utf-8,synthetic',
      locator: selector => ({
        waitFor: async options => {
          assert.ok(['[role="dialog"]', '#keep'].includes(selector))
          assert.ok(options.timeout > 0 && options.timeout <= 100)
        },
        click: async options => {
          assert.equal(selector, '#confirm')
          assert.ok(options.timeout > 0 && options.timeout <= 100)
          if (mode === 'click-failed') throw new Error('synthetic confirmation click failed')
          calls.push('click')
          listeners.get('confirm:click')()
        },
      }),
    }
    app.evaluate = async callback => {
      callback({ app: { quit() {
        calls.push('quit-request')
        if (mode === 'early-exit') {
          child.exitCode = 0
          child.emit('exit', 0)
        } else if (mode === 'early-exit-evaluate-closed') {
          setImmediate(() => {
            child.exitCode = 0
            child.emit('exit', 0)
          })
        } else if (mode !== 'missing-window' && mode !== 'already-exited' && mode !== 'missing-exit-evaluate-closed') {
          app.emit('window', page)
          if (mode === 'window-before-exit') {
            child.exitCode = 0
            child.emit('exit', 0)
          }
        }
      } } })
      if (mode.endsWith('evaluate-closed')) {
        throw new Error('electronApplication.evaluate: Target page, context or browser has been closed')
      }
    }
    const abort = new AbortController()
    let seenSignal
    const run = requestElectronQuitWithConfirmation(app, {
      signal: abort.signal, timeoutMs: 100, allowExitWithoutConfirmation,
    })
    if (mode.startsWith('missing-')) setImmediate(() => abort.abort())
    if (mode === 'confirm' || (allowExitWithoutConfirmation && mode.startsWith('early-exit'))) {
      await run
      assert.deepEqual(calls, mode.startsWith('early-exit') ? ['quit-request'] : ['quit-request', 'click', 'ipc:confirm'])
      // Verify that the shared shutdown helper supplies its one deadline's
      // abort signal to custom requests and cancels it after completion.
      const result = await closeElectronWithDeadline({
        app, phase: 'synthetic-confirmed-quit', timeoutMs: 100,
        requestClose: async options => {
          assert.equal(options.timeoutMs, 100)
          assert.equal(options.signal.aborted, false)
          seenSignal = options.signal
        },
      })
      assert.equal(result.closed, true)
      assert.equal(seenSignal.aborted, true)
    } else {
      await assert.rejects(run, mode.startsWith('missing-')
        ? /Expected quit confirmation timed out/
        : mode === 'click-failed' ? /synthetic confirmation click failed/
        : mode === 'wrong-window' ? /Expected the self-contained/ : /before the expected/)
      if (mode !== 'missing-exit') assert.ok(!calls.includes('ipc:confirm'))
    }
    assert.equal(app.listenerCount('window'), 0)
    assert.equal(child.listenerCount('exit'), 0)
    assert.equal(getEventListeners(abort.signal, 'abort').length, 0)
  }

  {
    // A missing expected dialog still exercises real cleanup. The strict
    // shutdown helper must only force a process this test actually spawned.
    const child = spawn(process.execPath, ['-e', `
      process.on('disconnect', () => process.exit(0));
      process.send({ type: 'ready', pid: process.pid });
      setInterval(() => {}, 1000);
    `], {
      stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
      detached: process.platform !== 'win32',
      windowsHide: true,
    })
    const childClosed = new Promise(resolveClose => child.once('close', resolveClose))
    try {
      const [ready] = await once(child, 'message', { signal: AbortSignal.timeout(5_000) })
      assert.deepEqual(ready, { type: 'ready', pid: child.pid })
      const exitListeners = child.listenerCount('exit')
      const app = new EventEmitter()
      app.process = () => child
      app.evaluate = async callback => callback({ app: { quit() {} } })
      let sharedSignal
      const result = await closeElectronWithDeadline({
        app, phase: 'synthetic-missing-quit-dialog', timeoutMs: 25, emit() {},
        requestClose: options => {
          sharedSignal = options.signal
          assert.equal(options.timeoutMs, 25)
          return requestElectronQuitWithConfirmation(app, options)
        },
      })
      const supportsOwnedTreeReaping = ['win32', 'linux'].includes(process.platform)
      assert.equal(result.closed, false, 'a missing expected prompt must remain a failure')
      assert.equal(result.closeErrorCode, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
      assert.equal(sharedSignal.aborted, true)
      assert.equal(result.forcedExitSucceeded, supportsOwnedTreeReaping)
      assert.equal(result.processTreeReaped, supportsOwnedTreeReaping)
      assert.equal(child.exitCode !== null || child.signalCode !== null, supportsOwnedTreeReaping)
      assert.equal(app.listenerCount('window'), 0)
      assert.equal(child.listenerCount('exit'), exitListeners)
      assert.equal(getEventListeners(sharedSignal, 'abort').length, 0)
    } finally {
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
      let closeTimer
      try {
        await Promise.race([
          childClosed,
          new Promise((_, reject) => {
            closeTimer = setTimeout(() => reject(new Error('Owned missing-dialog fixture did not close')), 5_000)
          }),
        ])
      } finally {
        clearTimeout(closeTimer)
      }
    }
    assert.throws(() => process.kill(child.pid, 0), { code: 'ESRCH' })
  }

  for (const scheme of [
    'windows-creation-filetime:', 'linux-proc-start-ticks:', 'posix-ps-lstart:',
  ]) {
    const record = { pid: 1234, start_identity: `${scheme}old` }
    const probePid = pid => assert.equal(pid, record.pid)
    const recycled = gatewayProcessSnapshot(record, {
      probePid,
      readStartIdentity: () => `${scheme}new`,
    })
    assert.equal(recycled.pidPresent, true)
    assert.equal(recycled.identityConflict, true)
    assert.equal(recycled.alive, false, 'PID reuse must not keep the old Gateway alive')
    for (const liveStartIdentity of [record.start_identity, null, 'runtime-start:unknown']) {
      assert.equal(gatewayProcessSnapshot(record, {
        probePid,
        readStartIdentity: () => liveStartIdentity,
      }).alive, true, 'matching or unprovable identities must keep waiting')
    }
    assert.equal(gatewayProcessSnapshot(record, {
      probePid: () => { throw Object.assign(new Error('missing'), { code: 'ESRCH' }) },
      readStartIdentity: () => { assert.fail('an absent PID needs no identity probe') },
    }).alive, false)
    assert.equal(gatewayProcessSnapshot(record, {
      probePid: () => { throw Object.assign(new Error('denied'), { code: 'EPERM' }) },
      readStartIdentity: () => null,
    }).alive, true, 'a denied probe is not exit evidence')
  }
  assert.equal(gatewayProcessSnapshot({ pid: 1234, start_identity: 'runtime-start:old' }, {
    probePid: () => {},
    readStartIdentity: () => 'windows-creation-filetime:new',
  }).alive, true, 'an opaque recorded identity cannot prove PID reuse')
  const telemetry = startCaseTelemetry({
    caseName: 'direct-case',
    os: 'TestOS',
    shard: 'unit',
    attempt: 2,
    outputPath,
    emit: line => emitted.push(line),
  })
  const direct = await telemetry.finish('passed')
  assert.equal(direct.case, 'direct-case')
  assert.equal(direct.os, 'TestOS')
  assert.equal(direct.shard, 'unit')
  assert.equal(direct.attempt, 2)
  assert.equal(direct.status, 'passed')
  assert.equal(direct.duration_unit, 'ms')
  assert.ok(direct.duration >= 0)
  assert.ok(Date.parse(direct.end) >= Date.parse(direct.start))
  await assert.rejects(() => telemetry.finish('passed'), /already finished/)

  const passed = await runCommandWithTelemetry({
    caseName: 'command-pass',
    os: 'TestOS',
    shard: 'unit',
    attempt: 1,
    outputPath,
    emit: line => emitted.push(line),
    command: process.execPath,
    args: ['-e', 'process.exit(0)'],
  })
  assert.equal(passed.exitCode, 0)
  assert.equal(passed.record.status, 'passed')

  const failed = await runCommandWithTelemetry({
    caseName: 'command-fail',
    os: 'TestOS',
    shard: 'unit',
    attempt: 3,
    outputPath,
    emit: line => emitted.push(line),
    command: process.execPath,
    args: ['-e', 'process.exit(7)'],
  })
  assert.equal(failed.exitCode, 7)
  assert.equal(failed.record.status, 'failed')
  assert.deepEqual(failed.record.details, { exit_code: 7, signal: null })

  const timedOut = await runCommandWithTelemetry({
    caseName: 'command-timeout',
    os: 'TestOS',
    shard: 'unit',
    attempt: 1,
    outputPath,
    emit: line => emitted.push(line),
    timeoutMs: 100,
    command: process.execPath,
    args: ['-e', 'setTimeout(() => {}, 30_000)'],
  })
  assert.notEqual(timedOut.exitCode, 0)
  assert.equal(timedOut.record.status, 'failed')
  assert.equal(timedOut.record.details.timed_out, true)
  assert.equal(timedOut.record.details.timeout_ms, 100)

  const cliPassed = spawnSync(process.execPath, [
    helperPath,
    'run',
    '--case', 'cli-pass',
    '--os', 'TestOS',
    '--shard', 'cli',
    '--attempt', '4',
    '--output', outputPath,
    '--',
    process.execPath,
    '-e',
    'process.exit(process.env.OPENSQUILLA_DESKTOP_E2E_ATTEMPT === "4" '
      + '&& process.env.OPENSQUILLA_DESKTOP_E2E_SHARD === "cli" '
      + `&& process.env.OPENSQUILLA_CI_CASE_TELEMETRY_PATH === ${JSON.stringify(outputPath)} `
      + '? 0 : 8)',
  ], { encoding: 'utf8' })
  assert.equal(cliPassed.status, 0, cliPassed.stderr)
  const cliPassedRecord = JSON.parse(cliPassed.stdout.trim())
  assert.equal(cliPassedRecord.status, 'passed')

  const cliFailed = spawnSync(process.execPath, [
    helperPath,
    'run',
    '--case', 'cli-fail',
    '--os', 'TestOS',
    '--shard', 'cli',
    '--attempt', '5',
    '--output', outputPath,
    '--',
    process.execPath,
    '-e',
    'process.exit(9)',
  ], { encoding: 'utf8' })
  assert.equal(cliFailed.status, 9, cliFailed.stderr)
  const cliFailedRecord = JSON.parse(cliFailed.stdout.trim())
  assert.equal(cliFailedRecord.status, 'failed')

  const activeServer = createServer(() => {})
  const activeConnections = trackHttpServerConnections(activeServer)
  await new Promise((resolveListen, rejectListen) => {
    activeServer.once('error', rejectListen)
    activeServer.listen(0, '127.0.0.1', resolveListen)
  })
  const activeAddress = activeServer.address()
  assert.ok(activeAddress && typeof activeAddress === 'object')
  const activeServerClosed = once(activeServer, 'close')
  const activeRequest = once(activeServer, 'request')
  const activeSocket = createConnection(activeAddress.port, '127.0.0.1')
  const activeSocketClosed = once(activeSocket, 'close')
  await once(activeSocket, 'connect')
  activeSocket.write('GET /held-open HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n')
  await activeRequest
  await assert.rejects(
    () => closeHttpServerWithDeadline(activeServer, activeConnections, {
      label: 'held-open fixture shutdown',
      timeoutMs: 25,
    }),
    /held-open fixture shutdown timed out after 25ms/,
  )
  await activeServerClosed
  await activeSocketClosed
  assert.equal(activeSocket.destroyed, true)
  assert.equal(activeConnections.size, 0)

  const successfulKiller = new EventEmitter()
  successfulKiller.exitCode = null
  successfulKiller.signalCode = null
  successfulKiller.kill = () => true
  const successfulTreeKill = terminateWindowsProcessTree({
    pid: 41,
    timeoutMs: 100,
    fallback: () => assert.fail('successful taskkill must not use the fallback'),
    spawnProcess: () => {
      queueMicrotask(() => successfulKiller.emit('exit', 0, null))
      return successfulKiller
    },
  })
  await assert.doesNotReject(successfulTreeKill)
  assert.equal(await successfulTreeKill, true)

  const hangingKiller = new EventEmitter()
  hangingKiller.exitCode = null
  hangingKiller.signalCode = null
  let killedTaskkillWith = null
  hangingKiller.kill = signal => {
    killedTaskkillWith = signal
    return true
  }
  let directFallbacks = 0
  let treeFailure = null
  assert.equal(await terminateWindowsProcessTree({
    pid: 42,
    timeoutMs: 25,
    fallback: () => { directFallbacks += 1 },
    onFailure: failure => { treeFailure = failure },
    spawnProcess: () => hangingKiller,
  }), false)
  assert.equal(killedTaskkillWith, 'SIGKILL')
  assert.equal(directFallbacks, 1)
  assert.deepEqual(treeFailure, {
    pid: 42,
    timedOut: true,
    exitCode: null,
    signal: null,
    error: 'taskkill exceeded 25ms',
  })

  // Execute the first-chat fixture's own completion/cleanup helpers. A visible
  // response is insufficient: signed activity must become idle without consent.
  const firstChatFixture = await readFile(join(scriptDir, 'test-onboarding-first-chat.mjs'), 'utf8')
  const firstChatHelpers = firstChatFixture.slice(
    firstChatFixture.indexOf('async function waitForFirstChatIdle('),
    firstChatFixture.indexOf('// Do not let real model credentials'),
  )
  const idleActivity = { kind: 'ok', activeCount: 0,
    activity: { running: 0, queued: 0, waiting: 0, reservations: 0, auxiliary: 0 } }
  const fingerprint = 'a'.repeat(64)
  const firstChatRecord = { profile_fingerprint: fingerprint, port: 18791 }
  for (const mode of ['settles', 'never-idle', 'unreachable', 'late-idle', 'wrong-profile', 'wrong-port']) {
    let now = 0
    let reads = 0
    const timeouts = []
    const activity = [
      { kind: 'ok', activeCount: 1, activity: { ...idleActivity.activity, running: 1 } },
      { kind: 'ok', activeCount: 1, activity: { ...idleActivity.activity, auxiliary: 1 } },
      { kind: 'unreachable' },
      idleActivity,
    ]
    const scope = {
      assert, join, URL, userDataDir: 'synthetic-user-data', lastActivity: null, gatewayRecord: null,
      Date: { now: () => now }, delay: async milliseconds => { now += milliseconds },
      captureFirstSendDiagnostic: async (operation, timeoutMs) => {
        assert.equal(timeoutMs, 1_000 - now)
        return await operation()
      },
      desktop: { evaluate: async () => ({ status: 'ready', profileFingerprint: fingerprint,
        httpUrl: 'http://127.0.0.1:18791' }) },
      loadDesktopGatewayOwnershipRecord: path => {
        assert.equal(path, join('synthetic-user-data', 'gateway-ownership', fingerprint))
        return { status: 'valid', record: mode === 'wrong-profile'
          ? { ...firstChatRecord, profile_fingerprint: 'b'.repeat(64) }
          : mode === 'wrong-port' ? { ...firstChatRecord, port: 18792 } : firstChatRecord }
      },
      readVerifiedDesktopGatewayActivity: async (record, options) => {
        assert.equal(record, firstChatRecord)
        assert.ok(options.timeoutMs > 0 && options.timeoutMs <= 1_000 - now)
        timeouts.push(options.timeoutMs)
        reads++
        if (mode === 'late-idle') { now = 1_000; return idleActivity }
        if (mode === 'unreachable') return { kind: 'unreachable' }
        if (mode === 'never-idle') return activity[0]
        return activity.shift()
      },
    }
    const waitIdle = runInNewContext(`${firstChatHelpers}\nwaitForFirstChatIdle`, scope)
    if (mode === 'settles') {
      await waitIdle(1_000)
      assert.equal(reads, 4, 'wait through task settlement, auxiliary work and unknown activity')
      assert.equal(scope.lastActivity, idleActivity)
      assert.deepEqual(timeouts, [1_000, 800, 600, 400], 'all polls share the completion deadline')
    } else if (mode.startsWith('wrong-')) {
      await assert.rejects(waitIdle(1_000), /Expected values to be strictly equal/)
      assert.equal(reads, 0, 'do not inspect an unrelated Gateway')
    } else {
      await assert.rejects(waitIdle(1_000), /did not become idle within its completion budget/)
      assert.equal(now, 1_000)
    }
  }
  {
    const waitIdle = runInNewContext(`${firstChatHelpers}\nwaitForFirstChatIdle`, {
      captureFirstSendDiagnostic, desktop: { evaluate: () => new Promise(() => {}) },
    })
    await assert.rejects(waitIdle(Date.now() + 25), /First-send diagnostics timed out after/,
      'a hung renderer connection lookup must share the completion deadline')
  }
  for (const mode of ['success', 'close-failed', 'close-threw', 'server-failed', 'remove-failed',
    'primary-error', 'windows-wrapper', 'windows-no-commit', 'windows-live-electron',
    'windows-live-gateway', 'posix-wrapper']) {
    const primary = new Error('synthetic chat assertion failed')
    const cleanupFailure = new Error('synthetic shutdown failed')
    const calls = []
    const reports = []
    const ownedApp = {}
    const successful = ['success', 'windows-wrapper'].includes(mode)
    const wrapperTimeout = mode.startsWith('windows-') || mode === 'posix-wrapper'
    const committedLog = [
      { event: 'quit_gateway_exit', exited: true, hardTerminated: false },
      { event: 'desktop_exit_phase', to: 'committed', reason: 'all lifecycle-owned Gateways exited' },
    ].map(record => JSON.stringify(record)).join('\n') + '\n'
    const scope = {
      app: ownedApp, passed: true, runError: mode === 'primary-error' ? primary : null,
      appIdentity: {}, gatewayRecord: firstChatRecord,
      lastActivity: idleActivity, stages: ['reply rendered', 'Gateway idle'], requests: [], diagnostics: [],
      userDataRoot: 'synthetic-root', userDataDir: 'synthetic-user-data', outputDir: 'synthetic-output',
      syntheticKey: 'synthetic-key', serverSockets: new Set(), join,
      server: { closeAllConnections: () => calls.push('connections') },
      closeElectronWithDeadline: async options => {
        calls.push('electron')
        assert.equal(options.app, ownedApp)
        assert.equal(options.timeoutMs, 15_000)
        assert.equal(options.requestClose, undefined, 'first chat must never accept a quit dialog')
        assert.equal((await options.diagnostics()).lastActivity, idleActivity)
        if (mode === 'close-threw') throw cleanupFailure
        if (wrapperTimeout) return { closed: false, error: cleanupFailure,
          closeErrorCode: 'DESKTOP_E2E_SHUTDOWN_TIMEOUT', forcedExitSucceeded: true, processTreeReaped: true }
        return ['close-failed', 'primary-error'].includes(mode)
          ? { closed: false, error: cleanupFailure } : { closed: true }
      },
      canAcceptWindowsElectronShutdownFallback: options => canAcceptWindowsElectronShutdownFallback({
        ...options, platform: mode.startsWith('windows-') ? 'win32' : 'darwin',
      }),
      desktopShutdownEvidenceSince,
      electronProcessSnapshot: () => ({ electronPidExists: mode === 'windows-live-electron' }),
      gatewayProcessSnapshot: () => ({ alive: mode === 'windows-live-gateway' }),
      console: { warn() {} },
      closeHttpServerWithDeadline: async (_server, _sockets, options) => {
        calls.push('server')
        assert.equal(options.timeoutMs, 15_000)
        if (mode === 'server-failed') throw cleanupFailure
      },
      rm: async () => { calls.push('remove'); if (mode === 'remove-failed') throw cleanupFailure },
      readFile: async () => 'synthetic-key fixture log\n'
        + (calls.includes('electron') && mode !== 'windows-no-commit' ? committedLog : ''),
      writeFile: async (_path, body) => { calls.push('report'); reports.push(JSON.parse(body)) },
    }
    const finish = runInNewContext(`${firstChatHelpers}\nfinishFirstChat`, scope)
    if (successful) await finish()
    else await assert.rejects(finish(), error => error === (mode === 'primary-error' ? primary : cleanupFailure))
    assert.deepEqual(calls.slice(0, 3), ['electron', 'connections', 'server'])
    assert.equal(reports.length, 1)
    assert.equal(reports[0].ok, successful, 'report success only after bounded cleanup')
    assert.equal(calls.includes('remove'), successful || mode === 'remove-failed',
      'retain the fixture profile after an assertion or shutdown failure')
    assert.ok(!JSON.stringify(reports).includes('synthetic-key'), 'redact the synthetic credential')
    assert.equal(calls.at(-1), 'report')
  }

  // ElectronApplication.process() depends on a live Playwright dispatcher.
  // Successful close disposes it, so even diagnostics must retain the owned
  // ChildProcess beforehand instead of accessing the application afterward.
  const nativeFixtureSource = await readFile(
    join(scriptDir, 'test-desktop-window-background-flow.mjs'), 'utf8',
  )
  const assertNativeShutdownProcessOwnership = source => {
    const closeIndex = source.indexOf('const shutdown = await closeElectronWithDeadline(')
    const capture = /const\s+ownedChild\s*=\s*desktopApp\s*\.\s*process\s*\(\s*\)/.exec(source)
    assert.ok(closeIndex >= 0, 'native fixture must retain bounded Electron shutdown')
    assert.ok(capture && capture.index < closeIndex,
      'capture the owned ChildProcess before closing Electron')
    assert.doesNotMatch(source.slice(closeIndex), /desktopApp\s*\.\s*process\s*\(/,
      'never call the disposed Playwright process accessor after close begins')
    assert.match(source.slice(closeIndex), /childExited:\s*ownedChild\s*\?/,
      'post-close diagnostics must inspect the retained owned handle')
  }
  assertNativeShutdownProcessOwnership(nativeFixtureSource)
  assert.throws(() => assertNativeShutdownProcessOwnership(
    nativeFixtureSource.replace('childExited: ownedChild ?', 'childExited: desktopApp.process() ?'),
  ), /never call the disposed Playwright process accessor/)
  assert.throws(() => assertNativeShutdownProcessOwnership(
    nativeFixtureSource.replace(/const\s+ownedChild\s*=\s*desktopApp\s*\.\s*process\s*\(\s*\)/, ''),
  ), /capture the owned ChildProcess before closing Electron/)

  // Execute the profile fixture's actual close/finally blocks without launching
  // Electron. A failed close must retain its owner and survive a later EBUSY.
  const importFixture = (await readFile(join(scriptDir, 'test-profile-import-flow.mjs'), 'utf8')).replace(/\r\n/g, '\n')
  const closeStart = importFixture.indexOf('async function closeActiveApp(')
  const closeEnd = importFixture.indexOf('\n\ntry {', closeStart)
  const closeSource = importFixture.slice(closeStart, closeEnd)
  const primaryCloseError = new Error('original Electron shutdown timeout')
  const timeoutShutdown = {
    closed: false, error: primaryCloseError, closeErrorCode: 'DESKTOP_E2E_SHUTDOWN_TIMEOUT',
    forcedExitSucceeded: false, processTreeReaped: false,
  }
  const cleanExitLog = [
    { event: 'quit_gateway_exit', exited: true, hardTerminated: false },
    { event: 'desktop_exit_phase', to: 'committed', reason: 'all lifecycle-owned Gateways exited' },
  ].map(record => JSON.stringify(record)).join('\n') + '\n'
  const secretProbe = 'synthetic-secret-must-not-appear'
  for (const [shutdown, electronPidExists, cleanExit, retainOwner, accepted] of [
    [timeoutShutdown, true, false, true, false],
    [{ ...timeoutShutdown, forcedExitSucceeded: true, processTreeReaped: true }, false, false, false, false],
    [{ ...timeoutShutdown, forcedExitSucceeded: true, processTreeReaped: true }, false, true, false, true],
    [{ ...timeoutShutdown, forcedExitSucceeded: true, processTreeReaped: true }, true, true, true, false],
    [{ ...timeoutShutdown, forcedExitSucceeded: true, processTreeReaped: true }, null, true, true, false],
    [{ closed: true, error: null }, false, true, false, true],
  ]) {
    const ownedApp = {}
    let closeCalls = 0
    let currentLog = [...Array.from({ length: 30 }, () => ({
      event: 'gateway_starting', path: secretProbe,
    })), { event: 'quit_gateway_process_cleanup', pid: 1234, complete: false,
      error: secretProbe }].map(record => JSON.stringify(record)).join('\n') + '\n'
    const scope = {
      app: ownedApp, activeAppUserData: 'synthetic-user-data', activeAppIdentity: {},
      ELECTRON_SHUTDOWN_TIMEOUT_MS: 15_000, join,
      readFile: async () => currentLog,
      closeElectronWithDeadline: async options => {
        closeCalls++
        assert.equal(options.app, ownedApp)
        assert.equal(options.timeoutMs, 15_000, 'preserve the native shutdown deadline')
        currentLog += [
          { event: 'before_quit', at: '2026-10-10T21:57:37.570Z', message: secretProbe },
          { event: 'desktop_exit_phase', to: 'draining', reason: secretProbe },
          { event: 'quit_deferred_for_profile_writer', at: secretProbe, error: secretProbe },
          { event: 'quit_gateway_activity_checked', pid: 1234, kind: 'unreachable',
            activeCount: null, ownershipMatched: null, retainedScope: true, exited: true,
            gatewayStatus: 'ready', instance_nonce: secretProbe, record: { proof: secretProbe } },
          { event: 'quit_confirmation_required', activity: 'unknown', message: secretProbe },
          { event: 'quit_gateway_activity_checked', at: secretProbe, pid: secretProbe,
            kind: secretProbe, activeCount: secretProbe, ownershipMatched: secretProbe,
            retainedScope: secretProbe, exited: secretProbe, gatewayStatus: secretProbe,
            activity: secretProbe, complete: secretProbe, code: secretProbe, signal: secretProbe },
          { event: secretProbe, token: secretProbe },
        ].map(record => JSON.stringify(record)).join('\n') + '\n'
        if (cleanExit) currentLog += cleanExitLog
        const diagnostics = await options.diagnostics()
        assert.equal(diagnostics.processes.electronPid, 54321)
        assert.equal(diagnostics.processes.electronPidExists, electronPidExists)
        assert.ok(diagnostics.exitEvents.some(record => record.event === 'before_quit'))
        assert.ok(diagnostics.exitEvents.some(record => record.phase === 'draining'))
        assert.ok(diagnostics.exitEvents.some(record => record.event === 'quit_gateway_activity_checked'
          && record.pid === 1234 && record.retainedScope === true && record.exited === true))
        assert.ok(diagnostics.exitEvents.some(record => record.event === 'quit_confirmation_required'
          && record.activity === 'unknown'))
        assert.ok(diagnostics.recentLifecycleEvents.length <= 24)
        assert.ok(diagnostics.recentLifecycleEvents.some(record => record.pid === 1234
          && record.event === 'quit_gateway_process_cleanup' && record.complete === false))
        assert.ok(!JSON.stringify(diagnostics).includes(secretProbe), 'diagnostics must omit raw log and probe errors')
        return shutdown
      },
      electronProcessSnapshot: () => ({ electronPid: 54321, electronPidExists,
        diagnosticError: secretProbe, electronProbeError: secretProbe }),
      desktopShutdownEvidenceSince,
      canAcceptWindowsElectronShutdownFallback: options => canAcceptWindowsElectronShutdownFallback({ ...options, platform: 'win32' }),
      console: { warn() {}, error() {} },
    }
    const close = runInNewContext(`${closeSource}\ncloseActiveApp`, scope)
    if (accepted) await close('existing-profile-electron-shutdown')
    else await assert.rejects(close('existing-profile-electron-shutdown'), error => error === primaryCloseError)
    assert.equal(scope.app, retainOwner ? ownedApp : null, 'clear only a proven exited owner')
    assert.equal(scope.activeAppUserData, retainOwner ? 'synthetic-user-data' : null)
    const finalCloseError = await close('profile-import-final-shutdown', { failOnError: false })
    assert.equal(finalCloseError, retainOwner ? primaryCloseError : undefined)
    assert.equal(closeCalls, retainOwner ? 2 : 1, 'an exited process must not incur another close deadline')
  }
  const finalizer = importFixture.slice(importFixture.lastIndexOf('\n} finally {') + 2)
  for (const [primaryError, failureStage] of [
    [primaryCloseError, 'remove'], [null, 'remove'],
    [primaryCloseError, 'close'], [null, 'close'],
  ]) {
    const cleanupError = Object.assign(new Error('synthetic file still locked'), { code: 'EBUSY' })
    const cleanupReports = []
    const scope = {
      runError: primaryError, primaryError, app: null, fakeProvider: null, root: 'synthetic-root',
      closeActiveApp: async () => failureStage === 'close' ? cleanupError : undefined,
      rm: async () => { if (failureStage === 'remove') throw cleanupError },
      console: { error: line => cleanupReports.push(line) },
    }
    const work = runInNewContext(
      `(async () => { try { if (primaryError) throw primaryError } ${finalizer} })()`, scope,
    )
    await assert.rejects(work, error => error === (primaryError ?? cleanupError))
    if (primaryError) assert.ok(cleanupReports.length > 0, 'secondary cleanup failure must remain observable')
  }

  // Exercise the actual helper with a Windows child lifecycle, without sending
  // OS signals to synthetic PIDs. Exit and stdio close are independent events.
  const shutdownSource = await readFile(join(scriptDir, 'e2e-shutdown-helpers.mjs'), 'utf8')
  const shutdownBody = shutdownSource.slice(shutdownSource.indexOf('const FORCED_PROCESS_EXIT_TIMEOUT_MS'))
    .replace(/^export /gm, '')
  for (const scenario of ['success', 'exit-during-diagnostics', 'taskkill-128-race']) {
    const child = Object.assign(new EventEmitter(), {
      pid: 1234, exitCode: null, signalCode: null, killed: false,
      ...Object.fromEntries(['stdin', 'stdout', 'stderr'].map(name => [name,
        Object.assign(new EventEmitter(), {
          destroyed: false, readableEnded: false, writableFinished: false,
        }),
      ])),
    })
    const exitChild = () => { child.exitCode = 0; child.emit('exit', 0, null) }
    let processCalls = 0
    let closeStarted = false
    let treeKillCalls = 0
    const logs = []
    const fixture = runInNewContext(`${shutdownBody}\n({ closeElectronWithDeadline, canAcceptWindowsElectronShutdownFallback })`, {
      AbortController, performance, delay,
      process: {
        platform: 'win32',
        kill: pid => {
          assert.equal(pid, child.pid)
          if (child.exitCode !== null) throw Object.assign(new Error('missing'), { code: 'ESRCH' })
        },
      },
      terminateWindowsProcessTree: async ({ pid, onFailure }) => {
        treeKillCalls += 1
        assert.equal(scenario, 'taskkill-128-race')
        assert.equal(pid, child.pid)
        exitChild()
        onFailure({ pid, timedOut: false, exitCode: 128, signal: null, error: null })
        return false
      },
    })
    const result = await fixture.closeElectronWithDeadline({
      app: {
        process: () => {
          processCalls += 1
          assert.equal(closeStarted, false, 'capture the owned child before closing the dispatcher')
          return child
        },
        close: () => {
          closeStarted = true
          assert.equal(child.listenerCount('exit'), 1)
          assert.equal(child.listenerCount('close'), 1)
          if (scenario !== 'success') return new Promise(() => {})
          exitChild()
          child.stdout.emit('end')
          child.stdout.emit('close')
          child.emit('close', 0, null)
        },
      },
      phase: scenario, timeoutMs: 10,
      diagnostics: async () => {
        if (scenario === 'exit-during-diagnostics') exitChild()
        return { marker: 'bounded-diagnostic' }
      },
      emit: line => logs.push(JSON.parse(line)),
    })
    assert.equal(processCalls, 1)
    for (const emitter of [child, child.stdin, child.stdout, child.stderr]) {
      assert.equal(emitter.eventNames().length, 0, 'diagnostic listeners must always be removed')
    }
    if (scenario === 'success') {
      assert.equal(result.closed, true)
      assert.equal(logs.length, 0, 'successful shutdown must remain silent')
      assert.equal(treeKillCalls, 0)
      continue
    }
    assert.equal(result.closed, false)
    assert.equal(result.closeErrorCode, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
    assert.equal(result.forcedExitSucceeded, false)
    assert.equal(result.processTreeReaped, false)
    assert.equal(fixture.canAcceptWindowsElectronShutdownFallback({
      shutdown: result, gatewayExitLogged: true, committedExitLogged: true,
    }), false, 'neither PID absence nor taskkill 128 proves tree cleanup')
    const observation = result.shutdownObservation
    const events = observation.events.map(event => event.event)
    assert.equal(observation.process.pidPresent, false)
    assert.equal(observation.process.exitCode, 0)
    assert.equal(observation.process.stdio.stdout.destroyed, false)
    assert.equal(observation.process.stdio.stdout.readableEnded, false)
    assert.ok(events.indexOf('close-start') < events.indexOf('close-failed'))
    assert.ok(events.indexOf('close-failed') < events.indexOf('child-exit'))
    assert.equal(events.includes('child-close'), false)
    assert.equal(events.includes('stdout-end'), false)
    assert.equal(logs.at(-1).event, 'desktop_e2e_electron_shutdown_cleanup')
    if (scenario === 'exit-during-diagnostics') {
      assert.equal(treeKillCalls, 0)
      assert.ok(events.indexOf('child-exit') < events.indexOf('diagnostics-complete'))
    } else {
      assert.equal(treeKillCalls, 1)
      assert.ok(events.indexOf('force-start') < events.indexOf('child-exit'))
      assert.ok(events.indexOf('child-exit') < events.indexOf('tree-termination-failed'))
      assert.equal(logs[1].exitCode, 128)
    }
  }

  const shutdownLogs = []
  // The production helper invokes real taskkill on Windows, so this fixture
  // must own its PID rather than supplying a synthetic EventEmitter PID.
  const hangingProcess = spawn(process.execPath, ['-e', `
    process.title = 'ci( ) worker)';
    process.on('disconnect', () => process.exit(0));
    process.send({ type: 'ready', pid: process.pid });
    setInterval(() => {}, 1000);
  `], {
    stdio: ['ignore', 'ignore', 'ignore', 'ipc'],
    detached: process.platform !== 'win32',
    windowsHide: true,
  })
  const hangingProcessClosed = new Promise(resolveClose => hangingProcess.once('close', resolveClose))
  try {
    const [ready] = await once(hangingProcess, 'message', { signal: AbortSignal.timeout(5_000) })
    assert.deepEqual(ready, { type: 'ready', pid: hangingProcess.pid })
    if (process.platform === 'linux') {
      // Exercise actual kernel records, including ')' inside the process name.
      assert.equal(readLinuxProcessStat(process.pid).ppid, process.ppid)
      const rawStat = await readFile('/proc/' + hangingProcess.pid + '/stat', 'utf8')
      assert.ok(rawStat.startsWith(hangingProcess.pid + ' (ci( ) worker)) '))
      const childStat = readLinuxProcessStat(hangingProcess.pid)
      assert.equal(childStat.pid, hangingProcess.pid)
      assert.equal(childStat.ppid, process.pid)
      assert.equal(childStat.pgid, hangingProcess.pid)
      assert.match(childStat.startTicks, /^[1-9][0-9]*$/)
    }
    const hangingElectron = {
      close: () => new Promise(() => {}),
      process: () => hangingProcess,
    }
    const supportsOwnedTreeReaping = ['win32', 'linux'].includes(process.platform)
    const hangingShutdown = await closeElectronWithDeadline({
      app: hangingElectron,
      phase: 'unit-hanging-electron',
      timeoutMs: 25,
      diagnosticTimeoutMs: 25,
      diagnostics: async () => ({ marker: 'bounded-diagnostic' }),
      emit: line => shutdownLogs.push(line),
    })
    assert.equal(hangingShutdown.closed, false)
    assert.equal(hangingShutdown.closeErrorCode, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
    assert.equal(hangingShutdown.forcedExitSucceeded, supportsOwnedTreeReaping)
    assert.equal(hangingShutdown.processTreeReaped, supportsOwnedTreeReaping)
    assert.match(hangingShutdown.error.message, /DESKTOP_E2E_ELECTRON_SHUTDOWN_FAILED/)
    assert.equal(hangingProcess.exitCode !== null || hangingProcess.signalCode !== null, supportsOwnedTreeReaping)
    assert.equal(shutdownLogs.length, 2)
    const shutdownLog = JSON.parse(shutdownLogs[0])
    const { shutdownObservation, ...originalDetail } = shutdownLog
    assert.deepEqual({
      ...originalDetail,
      error: String(shutdownLog.error).split('\n')[0],
    }, {
      event: 'desktop_e2e_electron_shutdown_failed',
      phase: 'unit-hanging-electron',
      timeoutMs: 25,
      error: 'Error: unit-hanging-electron Electron shutdown timed out after 25ms',
      process: {
        pid: hangingProcess.pid,
        exitCode: null,
        signalCode: null,
        killed: false,
      },
      diagnostics: { marker: 'bounded-diagnostic' },
    })
    assert.equal(shutdownObservation.events[0].event, 'close-start')
    const cleanupLog = JSON.parse(shutdownLogs[1])
    assert.equal(cleanupLog.event, 'desktop_e2e_electron_shutdown_cleanup')
    assert.equal(cleanupLog.processTreeReaped, supportsOwnedTreeReaping)
    assert.equal(cleanupLog.forcedExitSucceeded, supportsOwnedTreeReaping)
    assert.equal(cleanupLog.shutdownObservation.events.some(event => event.event === 'child-exit'), supportsOwnedTreeReaping)
    assert.equal(hangingProcess.listenerCount('exit'), 0)
  } finally {
    if (hangingProcess.exitCode === null && hangingProcess.signalCode === null) {
      hangingProcess.kill('SIGKILL')
    }
    let closeTimer
    try {
      await Promise.race([
        hangingProcessClosed,
        new Promise((_, reject) => {
          closeTimer = setTimeout(() => reject(new Error('Owned telemetry fixture did not close')), 5_000)
        }),
      ])
    } finally {
      clearTimeout(closeTimer)
    }
  }
  assert.throws(() => process.kill(hangingProcess.pid, 0), { code: 'ESRCH' })

  const shutdownCheckpoint = '{"event":"previous_launch"}\n'
  const cleanGatewayExit = JSON.stringify({
    event: 'quit_gateway_exit',
    exited: true,
    hardTerminated: false,
  })
  const committedExit = JSON.stringify({
    event: 'desktop_exit_phase',
    to: 'committed',
    reason: 'all lifecycle-owned Gateways exited',
  })
  const shutdownEvidence = desktopShutdownEvidenceSince(
    shutdownCheckpoint,
    `${shutdownCheckpoint}${cleanGatewayExit}\n${committedExit}\n`,
  )
  assert.deepEqual(shutdownEvidence, {
    gatewayExitLogged: true,
    committedExitLogged: true,
  })
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      shutdownCheckpoint,
      `${cleanGatewayExit}\n${committedExit}\n`,
    ),
    { gatewayExitLogged: false, committedExitLogged: false },
  )
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      shutdownCheckpoint,
      `${shutdownCheckpoint}${committedExit}\n${cleanGatewayExit}\n`,
    ),
    { gatewayExitLogged: true, committedExitLogged: false },
  )
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      shutdownCheckpoint,
      `${shutdownCheckpoint}${JSON.stringify({
        event: 'quit_gateway_exit',
        exited: true,
        hardTerminated: true,
      })}\n${committedExit}\n`,
    ),
    { gatewayExitLogged: false, committedExitLogged: false },
  )
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      null,
      `${shutdownCheckpoint}${cleanGatewayExit}\n${committedExit}\n`,
    ),
    { gatewayExitLogged: false, committedExitLogged: false },
  )
  const hardGatewayExit = JSON.stringify({
    event: 'quit_gateway_exit',
    exited: true,
    hardTerminated: true,
  })
  const failedGatewayExit = JSON.stringify({
    event: 'quit_gateway_exit',
    exited: false,
    hardTerminated: false,
  })
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      shutdownCheckpoint,
      `${shutdownCheckpoint}${cleanGatewayExit}\n${hardGatewayExit}\n${committedExit}\n`,
    ),
    { gatewayExitLogged: false, committedExitLogged: false },
  )
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      shutdownCheckpoint,
      `${shutdownCheckpoint}${cleanGatewayExit}\n${failedGatewayExit}\n${committedExit}\n`,
    ),
    { gatewayExitLogged: false, committedExitLogged: false },
  )
  assert.deepEqual(
    desktopShutdownEvidenceSince(
      shutdownCheckpoint,
      `${shutdownCheckpoint}${cleanGatewayExit}\n${committedExit}\n${hardGatewayExit}\n`,
    ),
    { gatewayExitLogged: false, committedExitLogged: false },
  )

  const acceptedShutdown = {
    closed: false,
    closeErrorCode: 'DESKTOP_E2E_SHUTDOWN_TIMEOUT',
    forcedExitSucceeded: true,
    processTreeReaped: true,
  }
  const acceptanceProof = {
    platform: 'win32',
    shutdown: acceptedShutdown,
    ...shutdownEvidence,
  }
  assert.equal(canAcceptWindowsElectronShutdownFallback(acceptanceProof), true)
  for (const rejected of [
    { ...acceptanceProof, platform: 'darwin' },
    { ...acceptanceProof, shutdown: { ...acceptedShutdown, closed: true } },
    {
      ...acceptanceProof,
      shutdown: { ...acceptedShutdown, closeErrorCode: 'SYNTHETIC_CLOSE_FAILURE' },
    },
    {
      ...acceptanceProof,
      shutdown: { ...acceptedShutdown, forcedExitSucceeded: false },
    },
    {
      ...acceptanceProof,
      shutdown: { ...acceptedShutdown, processTreeReaped: false },
    },
    { ...acceptanceProof, gatewayExitLogged: false },
    { ...acceptanceProof, committedExitLogged: false },
  ]) {
    assert.equal(canAcceptWindowsElectronShutdownFallback(rejected), false)
  }

  const records = (await readFile(outputPath, 'utf8'))
    .trim()
    .split('\n')
    .map(line => JSON.parse(line))
  assert.deepEqual(records, [
    ...emitted.map(line => JSON.parse(line)),
    cliPassedRecord,
    cliFailedRecord,
  ])
  assert.deepEqual(records.map(record => record.case), [
    'direct-case',
    'command-pass',
    'command-fail',
    'command-timeout',
    'cli-pass',
    'cli-fail',
  ])
  // This portable contract belongs in the existing Node owner, without a
  // second interpreter launch from the Python upgrade suite.
  {
    const source = await readFile(join(scriptDir, 'test-packaged-session-recovery.mjs'), 'utf8')
    const start = source.indexOf('  gatewayReadinessStartedAt = performance.now()')
    const end = source.indexOf('  // The preceding release-upgrade launch', start)
    assert.ok(start > 0 && end > start)
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
    const run = new AsyncFunction('waitFor', 'page', 'assert', 'performance', 'console',
      'DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS',
      'let gatewayReadinessStartedAt, lastGatewayConnection;\n' + source.slice(start, end))
    for (const terminal of ['ready', 'error', 'stopped', 'starting']) {
      let calls = 0
      const logs = []
      const page = { evaluate: async () => ({
        status: ++calls === 1 ? 'starting' : terminal,
        error: terminal === 'error' ? 'synthetic startup failure' : null,
        authToken: 'must-never-appear-in-evidence',
      }) }
      const wait = async (check, label, budget) => {
        assert.equal(budget, 120_000)
        assert.equal(label, 'candidate Desktop Gateway readiness')
        for (let i = 0; i < 3; i++) if (await check()) return
        throw new Error('synthetic startup timeout')
      }
      const attempt = run(wait, page, assert, performance,
        { error: value => logs.push(value) }, 120_000)
      if (terminal === 'ready') {
        await attempt
        assert.equal(calls, 2)
        assert.equal(JSON.parse(logs[0]).budgetMs, 120_000)
      } else {
        await assert.rejects(attempt, error => {
          assert.ok(!error.message.includes('must-never-appear-in-evidence'))
          assert.match(error.message, terminal === 'starting'
            ? /synthetic startup timeout/ : /Candidate Gateway failed/)
          return true
        })
        assert.equal(calls, terminal === 'starting' ? 3 : 2)
        assert.deepEqual(logs, [])
      }
    }
  }
  await import('./test-packaged-first-send-cleanup.mjs')
  await import('./test-session-recovery-transport-contract.mjs')
  await import('./test-session-recovery-rpc-evidence.mjs')
  await import('./test-e2e-subprocess-diagnostics.mjs')
  console.log('Desktop E2E case telemetry checks passed')
} finally {
  await rm(root, { recursive: true, force: true })
}
