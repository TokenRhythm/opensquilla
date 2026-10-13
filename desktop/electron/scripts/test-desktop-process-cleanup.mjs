import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mock } from 'node:test'

import { cleanupDesktopProcessTrees } from '../dist/desktop-process-cleanup.js'
import { DesktopQuitBudget } from '../dist/desktop-quit.js'

const protocol = 'opensquilla-desktop-process-cleanup-v1'
const flush = () => new Promise(resolve => setImmediate(resolve))
const launch = {
  command: 'synthetic-runtime', args: ['gateway', 'run'], cwd: '/synthetic/runtime',
  mode: 'bundled', env: { PATH: '/synthetic/bin' },
  ownershipDir: '/synthetic/control', instanceNonce: 'synthetic-private-launch-nonce',
}

function harness({ mode = 'bundled', respond, signalFails = false, spawnFails = false } = {}) {
  const calls = { spawns: [], requests: [], signals: [], directKills: [], drains: 0 }
  const helper = new EventEmitter()
  Object.assign(helper, {
    pid: 4242, exitCode: null, signalCode: null,
    stdout: new EventEmitter(), stdin: new EventEmitter(),
    stderr: { resume: () => { calls.drains += 1 } },
    kill: signal => { calls.directKills.push(signal); return true },
  })
  const close = (response, code = 0, signal = null) => {
    if (response !== undefined) helper.stdout.emit('data', response)
    helper.exitCode = code
    helper.signalCode = signal
    helper.emit('exit', code, signal)
    helper.emit('close', code, signal)
  }
  helper.stdin.end = text => {
    calls.requests.push(JSON.parse(text))
    if (respond) queueMicrotask(() => respond({ helper, close }))
  }
  const options = {
    launch: { ...launch, mode },
    spawnProcess: (...args) => {
      calls.spawns.push(args)
      if (spawnFails) throw new Error('synthetic spawn failure')
      return helper
    },
    signalProcess: (pid, signal) => {
      calls.signals.push([pid, signal])
      if (signalFails) throw new Error('synthetic group failure')
      return true
    },
  }
  return { helper, calls, options, close }
}

async function runWithBudget(options, test) {
  const budget = new DesktopQuitBudget(1000, 1000)
  try { await test(budget, options) } finally { budget.dispose() }
}

for (const mode of ['bundled', 'dev']) {
  const h = harness({ mode, respond: ({ close }) => close(JSON.stringify({ protocol, complete: true })) })
  await runWithBudget(h.options, async budget => {
    assert.equal(await cleanupDesktopProcessTrees({ ...h.options, budget }), true)
    const [command, args, spawnOptions] = h.calls.spawns[0]
    assert.equal(command, launch.command)
    assert.deepEqual(args, mode === 'bundled'
      ? ['--internal-child', 'process-tree', '--desktop-quit-cleanup']
      : ['run', '--no-sync', 'python', '-m', 'opensquilla.process_tree', '--desktop-quit-cleanup'])
    assert.equal(args.some(value => value.includes(launch.instanceNonce)), false)
    assert.equal(args.some(value => value.includes(launch.ownershipDir)), false)
    assert.equal(spawnOptions.cwd, launch.cwd)
    assert.deepEqual(spawnOptions.env, launch.env)
    assert.equal(spawnOptions.detached, true)
    assert.deepEqual(spawnOptions.stdio, ['pipe', 'pipe', 'pipe'])
    assert.equal(h.calls.requests[0].instance_nonce, launch.instanceNonce)
    assert.equal(h.calls.requests[0].ownership_dir, launch.ownershipDir)
    assert.ok(h.calls.requests[0].timeout_ms > 0 && h.calls.requests[0].timeout_ms <= 2000)
    assert.equal(h.calls.drains, 1)
    assert.deepEqual(h.calls.signals, [])
  })
}

for (const [response, code, signal] of [
  [JSON.stringify({ protocol, complete: false }), 0, null],
  [JSON.stringify({ protocol: 'unknown', complete: true }), 0, null],
  [JSON.stringify({ protocol, complete: 'true' }), 0, null],
  [JSON.stringify({ protocol, complete: true, extra: 1 }), 0, null],
  [JSON.stringify({ protocol, complete: true }), 1, null],
  [JSON.stringify({ protocol, complete: true }), null, 'SIGKILL'],
  ['[]', 0, null], ['null', 0, null], ['', 0, null], ['{bad}', 0, null],
  [`log\n${JSON.stringify({ protocol, complete: true })}`, 0, null],
]) {
  const h = harness({ respond: ({ close }) => close(response, code, signal) })
  await runWithBudget(h.options, async budget => {
    assert.equal(await cleanupDesktopProcessTrees({ ...h.options, budget }), false,
      'only a successful, complete, exact protocol response proves cleanup')
  })
}

for (const failure of ['spawn', 'stdin', 'oversized', 'process']) {
  const h = harness({
    spawnFails: failure === 'spawn',
    respond: ({ helper }) => {
      if (failure === 'oversized') helper.stdout.emit('data', 'x'.repeat(8193))
      else if (failure === 'stdin') helper.stdin.emit('error', new Error('synthetic pipe failure'))
      else helper.emit('error', new Error('synthetic process failure'))
    },
  })
  await runWithBudget(h.options, async budget => {
    assert.equal(await cleanupDesktopProcessTrees({ ...h.options, budget }), false)
    assert.deepEqual(h.calls.signals, failure === 'spawn' ? [] : [[-4242, 'SIGKILL']])
    h.close(JSON.stringify({ protocol, complete: true }))
  })
}

for (const alreadyExited of [false, true]) {
  let now = 0
  mock.timers.enable({ apis: ['setTimeout'] })
  const budget = new DesktopQuitBudget(1000, 1000, () => now)
  const h = harness({ signalFails: true })
  try {
    const work = cleanupDesktopProcessTrees({ ...h.options, budget })
    if (alreadyExited) {
      h.helper.exitCode = 0
      h.helper.emit('exit', 0, null)
    }
    now = 10
    mock.timers.tick(10)
    budget.tighten(0, 20)
    now = 30
    mock.timers.tick(20)
    await flush()
    assert.equal(await work, false, 'a tightened shared deadline bounds even a silent helper')
    assert.deepEqual(h.calls.signals, alreadyExited ? [] : [[-4242, 'SIGKILL']])
    assert.deepEqual(h.calls.directKills, alreadyExited ? [] : ['SIGKILL'])
    h.close(JSON.stringify({ protocol, complete: true }))
  } finally {
    budget.dispose()
    mock.timers.reset()
  }
}

{
  const budget = new DesktopQuitBudget(0, 0)
  const h = harness()
  try {
    assert.equal(await cleanupDesktopProcessTrees({ ...h.options, budget }), false)
    assert.equal(h.calls.spawns.length, 0, 'an exhausted quit budget cannot launch more work')
  } finally { budget.dispose() }
}

{
  const budget = new DesktopQuitBudget(10_000, 5000)
  const h = harness({ respond: ({ close }) => close(JSON.stringify({ protocol, complete: true })) })
  h.options.launch.env = {
    ...launch.env, PYTHONPATH: '/synthetic/python',
    OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE: 'stale-private-nonce',
    OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_ID: 'stale-id',
    OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR: '/synthetic/stale-control',
  }
  try {
    assert.equal(await cleanupDesktopProcessTrees({ ...h.options, budget }), true)
    assert.equal(h.calls.requests[0].timeout_ms, 5000, 'the helper protocol accepts at most five seconds')
    assert.deepEqual(h.calls.spawns[0][2].env, { PATH: '/synthetic/bin', PYTHONPATH: '/synthetic/python' })
    assert.ok(h.options.launch.env.OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE,
      'sanitizing the helper environment must not mutate retained launch metadata')
  } finally { budget.dispose() }
}

console.log('desktop process cleanup transport and deadline checks passed')
