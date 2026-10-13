import { strict as assert } from 'node:assert'
import test from 'node:test'
import { EventEmitter } from 'node:events'
import { readFile } from 'node:fs/promises'
import { setTimeout as delay } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { terminateOwnedPosixProcessGroup } from './e2e-posix-process-group.mjs'

function fixture() {
  const child = {
    pid: 31, exitCode: null, signalCode: null, killed: false,
    stdout: { readableEnded: false }, stderr: { readableEnded: false },
    kill() { assert.fail('must not suppress Playwright group cleanup') },
  }
  const stat = { pid: 31, ppid: 7, pgid: 31, startTicks: '123' }
  let clock = 0
  const signals = []
  const exited = () => {
    child.signalCode = 'SIGKILL'
    child.stdout.readableEnded = true
    child.stderr.readableEnded = true
  }
  const options = {
    platform: 'linux', ownerPid: 7, timeoutMs: 100, readStat: () => stat,
    now: () => clock, sleep: async ms => { clock += ms },
    signalGroup: (pid, signal) => {
      signals.push([pid, signal])
      if (signal === 'SIGKILL') exited()
      else throw Object.assign(new Error('absent'), { code: 'ESRCH' })
    },
  }
  return { child, stat, options, signals, exited }
}

test('owned live leader receives one group kill and closes without child.kill', async () => {
  const f = fixture()
  const result = await terminateOwnedPosixProcessGroup(f.child, f.options)
  assert.equal(result.error, null)
  assert.equal(result.processTreeReaped, true)
  assert.equal(f.child.killed, false)
  assert.deepEqual(f.signals, [[31, 'SIGKILL'], [31, 0]])
})

for (const [label, change] of [
  ['non Linux has no strict identity', f => { f.options.platform = 'darwin' }],
  ['leader already exited but descendants hold pipes', f => { f.child.exitCode = 0 }],
  ['not a direct child', f => { f.stat.ppid = 8 }],
  ['not a detached leader', f => { f.stat.pgid = 7 }],
  ['stat PID mismatch', f => { f.stat.pid = 32 }],
  ['missing start identity', f => { f.stat.startTicks = null }],
  ['exit during inspection', f => { f.options.readStat = () => { f.child.exitCode = 0; return f.stat } }],
  ['inspection failed', f => { f.options.readStat = () => { throw new Error('denied') } }],
]) {
  test('refuse unsafe cleanup: ' + label, async () => {
    const f = fixture()
    change(f)
    const result = await terminateOwnedPosixProcessGroup(f.child, f.options)
    assert.equal(result.processTreeReaped, false)
    assert.ok(result.error)
    assert.deepEqual(f.signals, [])
  })
}

for (const [label, probe] of [
  ['group remains including zombies', () => {}],
  ['group absent but pipes remain', () => { throw Object.assign(new Error('absent'), { code: 'ESRCH' }) }],
  ['permission denied is not absence', () => { throw Object.assign(new Error('denied'), { code: 'EPERM' }) }],
]) {
  test('kill request is not reaping proof: ' + label, async () => {
    const f = fixture()
    let kills = 0
    f.options.signalGroup = (_, signal) => {
      if (signal === 'SIGKILL') { kills += 1; f.child.signalCode = 'SIGKILL' }
      else probe()
    }
    const result = await terminateOwnedPosixProcessGroup(f.child, f.options)
    assert.equal(kills, 1)
    assert.equal(result.processTreeReaped, false)
    assert.ok(result.error)
  })
}

test('ESRCH during kill is accepted only once native exit and EOF are observed', async () => {
  const f = fixture()
  f.options.signalGroup = () => {
    f.exited()
    throw Object.assign(new Error('absent'), { code: 'ESRCH' })
  }
  assert.equal((await terminateOwnedPosixProcessGroup(f.child, f.options)).processTreeReaped, true)
})

test('successful POSIX cleanup preserves the original graceful deadline failure', async () => {
  const source = await readFile(new URL('./e2e-shutdown-helpers.mjs', import.meta.url), 'utf8')
  const body = source.slice(source.indexOf('const FORCED_PROCESS_EXIT_TIMEOUT_MS')).replace(/^export /gm, '')
  const child = Object.assign(new EventEmitter(), { pid: 31, exitCode: null, signalCode: null })
  let forcedCalls = 0
  const helper = runInNewContext(body + '\n({ closeElectronWithDeadline })', {
    AbortController, performance, delay,
    process: { platform: 'linux', kill() {} },
    terminateOwnedPosixProcessGroup: async owned => {
      assert.equal(owned, child)
      forcedCalls += 1
      return { error: null, processTreeReaped: true }
    },
  })
  const result = await helper.closeElectronWithDeadline({
    app: { process: () => child, close: () => new Promise(() => {}) },
    phase: 'fake-posix', timeoutMs: 1, emit() {},
  })
  assert.equal(forcedCalls, 1)
  assert.equal(result.closed, false)
  assert.equal(result.forcedExitSucceeded, true)
  assert.equal(result.processTreeReaped, true)
  assert.equal(result.closeErrorCode, 'DESKTOP_E2E_SHUTDOWN_TIMEOUT')
  assert.match(result.error.cause.message, /timed out after 1ms/)
  assert.equal(child.listenerCount('exit'), 0)
})

for (const platform of ['linux', 'win32']) {
  for (const scenario of ['exit-zero', 'exit-nonzero', 'exit-signal', 'missing-child',
    'missing-pid', 'unknown-status', 'still-live', 'close-rejects-after-exit']) {
    test(platform + ' close resolution verifies actual exit: ' + scenario, async () => {
      const source = await readFile(new URL('./e2e-shutdown-helpers.mjs', import.meta.url), 'utf8')
      const body = source.slice(source.indexOf('const FORCED_PROCESS_EXIT_TIMEOUT_MS')).replace(/^export /gm, '')
      const child = Object.assign(new EventEmitter(), {
        pid: 31, exitCode: null, signalCode: null, killed: false,
      })
      const primary = Object.assign(new Error('original close rejection'), { code: 'ORIGINAL_CLOSE_FAILURE' })
      let processReads = 0
      let closeStarted = false
      let forcedCalls = 0
      const logs = []
      const force = () => {
        forcedCalls += 1
        child.signalCode = 'SIGKILL'
        child.emit('exit', null, 'SIGKILL')
      }
      const helper = runInNewContext(body + '\n({ closeElectronWithDeadline, canAcceptWindowsElectronShutdownFallback })', {
        AbortController, performance, delay,
        process: { platform, kill() {} },
        terminateOwnedPosixProcessGroup: async owned => {
          assert.equal(owned, child)
          force()
          return { error: null, processTreeReaped: true }
        },
        terminateWindowsProcessTree: async () => { force(); return true },
      })
      const result = await helper.closeElectronWithDeadline({
        app: {
          process: () => {
            processReads += 1
            assert.equal(closeStarted, false, 'use the handle captured before dispatcher disposal')
            return scenario === 'missing-child' ? undefined : child
          },
          close: async () => {
            closeStarted = true
            if (scenario === 'exit-signal') child.signalCode = 'SIGSEGV'
            else if (scenario === 'exit-nonzero') child.exitCode = 7
            else if (scenario === 'unknown-status') child.signalCode = undefined
            else if (scenario !== 'still-live') child.exitCode = 0
            if (scenario === 'missing-pid') child.pid = undefined
            if (scenario === 'close-rejects-after-exit') throw primary
          },
        },
        phase: scenario, timeoutMs: 20, emit: line => logs.push(JSON.parse(line)),
      })
      assert.equal(processReads, 1)
      assert.equal(child.listenerCount('exit'), 0)
      if (scenario === 'exit-zero') {
        assert.equal(result.closed, true)
        assert.equal(result.error, null)
        assert.equal(logs.length, 0)
      } else {
        const expected = scenario === 'close-rejects-after-exit' ? primary.code
          : ['exit-nonzero', 'exit-signal'].includes(scenario)
            ? 'DESKTOP_E2E_ABNORMAL_EXIT' : 'DESKTOP_E2E_UNVERIFIED_EXIT'
        assert.equal(result.closed, false)
        assert.equal(result.closeErrorCode, expected)
        assert.equal(result.error.cause.code, expected)
        if (scenario === 'close-rejects-after-exit') assert.equal(result.error.cause, primary)
        assert.equal(result.forcedExitSucceeded, scenario === 'still-live')
        assert.equal(result.processTreeReaped, scenario === 'still-live')
        assert.equal(helper.canAcceptWindowsElectronShutdownFallback({
          platform, shutdown: result, gatewayExitLogged: true, committedExitLogged: true,
        }), false, 'unclean or unknown exit must never use timeout-only Windows fallback')
      }
      assert.equal(forcedCalls, scenario === 'still-live' ? 1 : 0)
    })
  }
}
