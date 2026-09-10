import assert from 'node:assert/strict'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { WindowsUpdateCoordinator, WindowsUpdatePreparationError } from '../dist/windows-update-coordinator.js'

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function fixture(overrides = {}) {
  const events = []
  const writers = new DesktopWriterAdmission()
  let token = null
  let running = true
  const hooks = {
    canStart: () => !writers.closed,
    started: () => events.push('started'),
    verify: async () => { events.push('verified') },
    closeWriters: () => { token = writers.close('update'); events.push('closed') },
    waitForWriters: async (signal) => { await writers.waitForAtMost(0, signal); events.push('drained') },
    stopGateways: async () => { events.push('gateway-stopped'); running = false; return true },
    assertCanHandoff: () => { assert.equal(writers.activeCount, 0); assert.equal(running, false) },
    launchInstaller: async () => { events.push('spawned') },
    committed: () => { events.push('committed') },
    recover: async (error, stopped) => {
      if (token) writers.reopen(token)
      if (stopped) running = true
      events.push(['failed', error, stopped])
    },
    ...overrides,
  }
  return { events, writers, hooks, isRunning: () => running }
}

// Verification is first; a failure leaves both pending writes and Gateway alone.
{
  const denial = new Error('wrong signer')
  const f = fixture({ verify: async () => { throw denial } })
  const completeWrite = f.writers.begin('user save')
  assert.equal(await new WindowsUpdateCoordinator().run(f.hooks), 'failed')
  assert.equal(f.writers.activeCount, 1)
  assert.equal(f.isRunning(), true)
  assert.deepEqual(f.events, ['started', ['failed', denial, false]])
  completeWrite()
}

// A real admitted writer must finish; duplicate clicks cannot start another run.
{
  const f = fixture()
  const finish = f.writers.begin('settings transaction')
  const coordinator = new WindowsUpdateCoordinator()
  const run = coordinator.run(f.hooks)
  await Promise.resolve()
  assert.equal(await coordinator.run(f.hooks), 'busy')
  assert.equal(f.isRunning(), true)
  assert.throws(() => f.writers.begin('late save'), /closed/)
  finish()
  assert.equal(await run, 'handed-off')
  assert.deepEqual(f.events, ['started', 'verified', 'closed', 'drained', 'gateway-stopped', 'spawned', 'committed'])
  assert.equal(await coordinator.run(f.hooks), 'busy')
}

// Deadline cancels only the wait. The writer is never killed or marked finished.
{
  const f = fixture()
  const finish = f.writers.begin('slow write')
  assert.equal(await new WindowsUpdateCoordinator(1).run(f.hooks), 'failed')
  assert.equal(f.writers.activeCount, 1)
  assert.equal(f.writers.closed, false)
  assert.equal(f.isRunning(), true)
  const error = f.events.at(-1)[1]
  assert.ok(error instanceof WindowsUpdatePreparationError)
  assert.equal(error.reason, 'writers_busy')
  finish()
  await f.writers.waitForAtMost(0)
  f.writers.begin('retry save')()
}

// A still-running Gateway prevents installer startup and leaves retry possible.
{
  const f = fixture({ stopGateways: async () => false })
  const coordinator = new WindowsUpdateCoordinator()
  assert.equal(await coordinator.run(f.hooks), 'failed')
  assert.equal(f.events.some((event) => event === 'spawned'), false)
  assert.equal(f.writers.closed, false)
  assert.equal(f.events.at(-1)[1].reason, 'gateway_busy')
}

// Asynchronous launch failure restores admission and the previously running service.
{
  const launched = deferred()
  const f = fixture({ launchInstaller: () => launched.promise })
  const coordinator = new WindowsUpdateCoordinator()
  const run = coordinator.run(f.hooks)
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  assert.equal(f.isRunning(), false)
  const error = new Error('ENOENT')
  launched.reject(error)
  assert.equal(await run, 'failed')
  assert.equal(f.isRunning(), true)
  assert.equal(f.writers.closed, false)
  assert.deepEqual(f.events.at(-1), ['failed', error, true])
  f.hooks.launchInstaller = async () => f.events.push('spawned')
  assert.equal(await coordinator.run(f.hooks), 'handed-off')
}

// Once NSIS exists, even a failure to quit is not permission to resume writers.
{
  const error = new Error('quit failed')
  const f = fixture({ committed: () => { throw error } })
  const coordinator = new WindowsUpdateCoordinator()
  await assert.rejects(coordinator.run(f.hooks), (value) => value === error)
  assert.equal(f.writers.closed, true)
  assert.equal(f.isRunning(), false)
  assert.equal(f.events.some((event) => Array.isArray(event) && event[0] === 'failed'), false)
  assert.equal(await coordinator.run(f.hooks), 'busy')
}

// Abort listeners are removed when a real writer finishes, not left to reject later.
{
  const writers = new DesktopWriterAdmission()
  const finish = writers.begin('write')
  const controller = new AbortController()
  const waiting = writers.waitForAtMost(0, controller.signal)
  finish()
  await waiting
  controller.abort(new Error('late timeout'))
  await writers.waitForAtMost(0)
}
console.log('Windows update coordinator checks passed (no installer or Gateway executed).')
