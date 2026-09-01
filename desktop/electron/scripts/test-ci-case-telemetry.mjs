import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { EventEmitter, once } from 'node:events'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createConnection } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  runCommandWithTelemetry,
  startCaseTelemetry,
} from './ci-case-telemetry.mjs'
import {
  closeElectronWithDeadline,
  closeHttpServerWithDeadline,
  trackHttpServerConnections,
} from './e2e-shutdown-helpers.mjs'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const helperPath = join(scriptDir, 'ci-case-telemetry.mjs')
const root = await mkdtemp(join(tmpdir(), 'opensquilla-ci-case-telemetry-'))
const outputPath = join(root, 'reports', 'cases.jsonl')
const emitted = []

try {
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

  let killedWith = null
  const shutdownLogs = []
  const hangingProcess = new EventEmitter()
  Object.assign(hangingProcess, {
    pid: 43,
    exitCode: null,
    signalCode: null,
    killed: false,
  })
  hangingProcess.kill = signal => {
    killedWith = signal
    hangingProcess.killed = true
    hangingProcess.signalCode = signal
    queueMicrotask(() => hangingProcess.emit('exit', null, signal))
    return true
  }
  const hangingElectron = {
    close: () => new Promise(() => {}),
    process: () => hangingProcess,
  }
  const hangingShutdown = await closeElectronWithDeadline({
    app: hangingElectron,
    phase: 'unit-hanging-electron',
    timeoutMs: 25,
    diagnosticTimeoutMs: 25,
    diagnostics: async () => ({ marker: 'bounded-diagnostic' }),
    emit: line => shutdownLogs.push(line),
  })
  assert.equal(hangingShutdown.closed, false)
  assert.match(hangingShutdown.error.message, /DESKTOP_E2E_ELECTRON_SHUTDOWN_FAILED/)
  assert.equal(killedWith, 'SIGKILL')
  assert.equal(shutdownLogs.length, 1)
  const shutdownLog = JSON.parse(shutdownLogs[0])
  assert.deepEqual({
    ...shutdownLog,
    error: String(shutdownLog.error).split('\n')[0],
  }, {
    event: 'desktop_e2e_electron_shutdown_failed',
    phase: 'unit-hanging-electron',
    timeoutMs: 25,
    error: 'Error: unit-hanging-electron Electron shutdown timed out after 25ms',
    process: {
      pid: 43,
      exitCode: null,
      signalCode: null,
      killed: false,
    },
    diagnostics: { marker: 'bounded-diagnostic' },
  })

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
  console.log('Desktop E2E case telemetry checks passed')
} finally {
  await rm(root, { recursive: true, force: true })
}
