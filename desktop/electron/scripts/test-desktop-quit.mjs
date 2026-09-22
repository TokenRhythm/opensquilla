import assert from 'node:assert/strict'
import { mock } from 'node:test'

import {
  DESKTOP_QUIT_CLEANUP_MS,
  DESKTOP_QUIT_TERMINATION_MS,
  DesktopQuitBudget,
  DesktopQuitConfirmation,
  quitGatewayWithinBudget,
} from '../dist/desktop-quit.js'

function deferred() {
  let resolve
  const promise = new Promise(accept => { resolve = accept })
  return { promise, resolve }
}

const flush = () => new Promise(resolve => setImmediate(resolve))

async function withBudget(run) {
  let current = 0
  mock.timers.enable({ apis: ['setTimeout'] })
  const budget = new DesktopQuitBudget(
    DESKTOP_QUIT_CLEANUP_MS,
    DESKTOP_QUIT_TERMINATION_MS,
    () => current,
  )
  const clock = {
    now: () => current,
    sleep: milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)),
    advance: async milliseconds => {
      current += milliseconds
      mock.timers.tick(milliseconds)
      await flush()
      // A phase that used all its allowance may hand off through a zero-time
      // wait in the next microtask. Drain those timers without advancing time.
      mock.timers.tick(0)
      await flush()
    },
  }
  try {
    await run(budget, clock)
  } finally {
    budget.dispose()
    mock.timers.reset()
  }
}

function unexpected(name) {
  return () => assert.fail(`${name} must not run in this exit path`)
}

function gatewayOptions(budget, overrides) {
  return {
    budget,
    hasExited: () => false,
    requestQuit: unexpected('requestQuit'),
    legacyDrain: unexpected('legacyDrain'),
    waitForExit: unexpected('waitForExit'),
    terminate: unexpected('terminate'),
    onLegacyDrain: unexpected('onLegacyDrain'),
    onTerminating: unexpected('onTerminating'),
    ...overrides,
  }
}

await withBudget(async (budget, clock) => {
  assert.equal(await quitGatewayWithinBudget(gatewayOptions(budget, {
    hasExited: () => true,
  })), true)
  assert.equal(clock.now(), 0, 'an already-exited child has no minimum quit delay')
})

await withBudget(async (budget, clock) => {
  let exited = false
  const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
    hasExited: () => exited,
    requestQuit: async remainingMs => {
      assert.equal(remainingMs, 10_000)
      return { kind: 'quit_accepted' }
    },
    waitForExit: async remainingMs => {
      assert.equal(remainingMs, 10_000)
      await clock.sleep(25)
      exited = true
      return true
    },
  }))
  await flush()
  await clock.advance(25)
  assert.equal(await quit, true)
  assert.equal(clock.now(), 25, 'successful cleanup must not wait out either budget')
  assert.equal(budget.signal.aborted, false)
})

await withBudget(async (budget, clock) => {
  const previewCleanup = budget.cleanup(clock.sleep(3_000))
  await clock.advance(3_000)
  assert.equal((await previewCleanup).completed, true)
  let exited = false
  const calls = []
  const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
    hasExited: () => exited,
    requestQuit: async remainingMs => {
      calls.push(['request', remainingMs])
      await clock.sleep(2_000)
      return { kind: 'quit_accepted' }
    },
    waitForExit: async remainingMs => {
      calls.push(['wait', remainingMs])
      return await new Promise(() => {})
    },
    onTerminating: () => calls.push(['terminating', clock.now()]),
    terminate: async remainingMs => {
      calls.push(['terminate', remainingMs])
      await clock.sleep(20)
      exited = true
      return true
    },
  }))
  await clock.advance(2_000)
  assert.deepEqual(calls, [['request', 7_000], ['wait', 5_000]])
  await clock.advance(4_999)
  assert.equal(budget.signal.aborted, false)
  assert.equal(calls.length, 2)
  await clock.advance(1)
  assert.equal(budget.signal.aborted, true)
  assert.deepEqual(calls.slice(-2), [['terminating', 10_000], ['terminate', 5_000]])
  await clock.advance(20)
  assert.equal(await quit, true)
  assert.equal(clock.now(), 10_020, 'earlier cleanup and request time must consume the same allowance')
})

await withBudget(async (budget, clock) => {
  const request = deferred()
  const termination = deferred()
  const calls = []
  let exited = false
  const outcomes = []
  const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
    hasExited: () => exited,
    requestQuit: async remainingMs => {
      calls.push(['request', remainingMs])
      return await request.promise
    },
    waitForExit: async remainingMs => {
      calls.push(['wait', remainingMs])
      return await new Promise(() => {})
    },
    onTerminating: () => calls.push(['terminating', clock.now()]),
    terminate: async remainingMs => {
      calls.push(['terminate', remainingMs])
      return await termination.promise
    },
  })).then(result => {
    outcomes.push(result)
    return result
  })
  await clock.advance(9_999)
  assert.deepEqual(calls, [['request', 10_000]])
  await clock.advance(1)
  assert.deepEqual(calls, [
    ['request', 10_000], ['wait', 0], ['terminating', 10_000], ['terminate', 5_000],
  ])
  await clock.advance(4_999)
  assert.deepEqual(outcomes, [])
  await clock.advance(1)
  assert.equal(await quit, false)
  assert.equal(budget.remainingTotalMs, 0)
  assert.deepEqual(outcomes, [false])
  request.resolve({ kind: 'quit_accepted' })
  exited = true
  termination.resolve(true)
  await flush()
  assert.deepEqual(outcomes, [false], 'late callbacks cannot turn an expired failed quit into success')
  assert.equal(calls.filter(([name]) => name === 'terminate').length, 1)
})

await withBudget(async (budget, clock) => {
  const legacy = deferred()
  const phases = []
  let settled = false
  const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
    requestQuit: async () => ({ kind: 'unsupported' }),
    onLegacyDrain: () => phases.push('legacy'),
    legacyDrain: async () => await legacy.promise,
  })).then(result => {
    settled = true
    return result
  })
  await flush()
  await clock.advance(20_000)
  assert.deepEqual(phases, ['legacy'])
  assert.equal(settled, false, 'unsupported peers keep the prior drain policy past the quit budget')
  legacy.resolve(true)
  assert.equal(await quit, true)
})

await withBudget(async (budget, clock) => {
  const result = await quitGatewayWithinBudget(gatewayOptions(budget, {
    requestQuit: async () => ({ kind: 'rejected' }),
  }))
  assert.equal(result, false, 'a rejected ownership request must never trigger termination')
  assert.equal(clock.now(), 0)
})

await withBudget(async (budget, clock) => {
  const calls = []
  const outcomes = []
  const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
    requestQuit: async () => ({
      kind: 'quit_accepted', remainingMs: 0, totalRemainingMs: 200,
    }),
    waitForExit: async remainingMs => {
      calls.push(['wait', remainingMs])
      return await new Promise(() => {})
    },
    onTerminating: () => calls.push(['terminating', clock.now()]),
    terminate: async remainingMs => {
      calls.push(['terminate', remainingMs])
      return await new Promise(() => {})
    },
  })).then(result => { outcomes.push(result); return result })
  await clock.advance(0)
  assert.deepEqual(calls, [['wait', 0], ['terminating', 0], ['terminate', 200]])
  assert.equal(budget.signal.aborted, true)
  await clock.advance(199)
  assert.deepEqual(outcomes, [])
  await clock.advance(1)
  assert.equal(await quit, false)
  assert.equal(clock.now(), 200, 'a late drain ACK must preserve its original final deadline')
  assert.equal(budget.remainingTotalMs, 0)
})

await withBudget(async (budget, clock) => {
  const calls = []
  const outcomes = []
  const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
    requestQuit: async () => {
      await clock.sleep(40)
      return { kind: 'quit_accepted', remainingMs: 100, totalRemainingMs: 300 }
    },
    waitForExit: async remainingMs => {
      calls.push(['wait', remainingMs])
      return await new Promise(() => {})
    },
    onTerminating: () => calls.push(['terminating', clock.now()]),
    terminate: async remainingMs => {
      calls.push(['terminate', remainingMs])
      return await new Promise(() => {})
    },
  })).then(result => { outcomes.push(result); return result })
  await clock.advance(40)
  assert.deepEqual(calls, [['wait', 60]])
  assert.equal(budget.remainingCleanupMs, 60)
  assert.equal(budget.remainingTotalMs, 260, 'the full request round trip consumes both ACK budgets')
  await clock.advance(59)
  assert.equal(calls.length, 1)
  await clock.advance(1)
  assert.deepEqual(calls.slice(-2), [['terminating', 100], ['terminate', 200]])
  await clock.advance(199)
  assert.deepEqual(outcomes, [])
  await clock.advance(1)
  assert.equal(await quit, false)
  assert.equal(clock.now(), 300, 'network time cannot shift the final deadline to 340ms')
})

await withBudget(async (budget, clock) => {
  const outcomes = []
  const cleanup = budget.cleanup(new Promise(() => {})).then(result => {
    outcomes.push(['cleanup', clock.now()])
    return result
  })
  const termination = budget.termination(new Promise(() => {})).then(result => {
    outcomes.push(['termination', clock.now()])
    return result
  })
  await clock.advance(100)
  budget.tighten(50, 200)
  budget.tighten(9000, 14_000)
  assert.equal(budget.remainingCleanupMs, 50)
  assert.equal(budget.remainingTotalMs, 200, 'later callers cannot extend a shared deadline')
  await clock.advance(49)
  assert.deepEqual(outcomes, [])
  await clock.advance(1)
  assert.deepEqual(await cleanup, { completed: false })
  assert.deepEqual(outcomes, [['cleanup', 150]], 'an existing cleanup waiter must be rescheduled')
  budget.tighten(2000, 5000)
  assert.equal(budget.remainingCleanupMs, 0)
  assert.equal(budget.remainingTotalMs, 150)
  await clock.advance(149)
  assert.deepEqual(outcomes, [['cleanup', 150]])
  await clock.advance(1)
  assert.deepEqual(await termination, { completed: false })
  assert.deepEqual(outcomes, [['cleanup', 150], ['termination', 300]],
    'an existing termination waiter must use the shortened shared deadline')
})

for (const terminationResult of [false, true]) {
  await withBudget(async (budget, clock) => {
    const quit = quitGatewayWithinBudget(gatewayOptions(budget, {
      requestQuit: async () => ({ kind: 'unreachable' }),
      waitForExit: async () => await new Promise(() => {}),
      onTerminating: () => {},
      terminate: async () => terminationResult,
    }))
    await flush()
    await clock.advance(10_000)
    assert.equal(await quit, false, 'termination needs observed process exit, even when the command succeeds')
  })
}

{
  const confirmation = new DesktopQuitConfirmation()
  const activity = deferred()
  const answer = deferred()
  const checks = []
  const prompts = []
  const first = confirmation.request({
    check: async () => {
      checks.push('initial')
      return await activity.promise
    },
    confirm: async state => {
      prompts.push(state)
      return await answer.promise
    },
  })
  const repeatedOptions = {
    check: unexpected('a repeated activity check'),
    confirm: unexpected('a repeated prompt'),
  }
  assert.equal(confirmation.request(repeatedOptions), first)
  await flush()
  assert.deepEqual(checks, ['initial'])
  assert.deepEqual(prompts, [])
  activity.resolve('active')
  await flush()
  assert.deepEqual(prompts, ['active'])
  assert.equal(confirmation.request(repeatedOptions), first, 'repeats also coalesce while the dialog is open')
  let quitCalls = 0
  const applicationDecision = first.then(accepted => {
    if (accepted) quitCalls += 1
  })
  answer.resolve(false)
  assert.equal(await first, false)
  await applicationDecision
  assert.equal(quitCalls, 0, 'cancellation must leave the application running')

  const afterCancel = confirmation.request({
    check: async () => {
      checks.push('after-cancel')
      return 'idle'
    },
    confirm: unexpected('an idle prompt'),
  })
  assert.notEqual(afterCancel, first)
  assert.equal(await afterCancel, true)
  assert.deepEqual(checks, ['initial', 'after-cancel'])

  const afterAcceptance = confirmation.request({
    check: async () => {
      checks.push('after-acceptance')
      return 'unknown'
    },
    confirm: async state => {
      prompts.push(state)
      return true
    },
  })
  assert.notEqual(afterAcceptance, afterCancel)
  assert.equal(await afterAcceptance, true)
  assert.deepEqual(checks, ['initial', 'after-cancel', 'after-acceptance'])
  assert.deepEqual(prompts, ['active', 'unknown'], 'unknown activity requires explicit confirmation')
}

for (const failurePhase of ['check', 'confirm']) {
  const confirmation = new DesktopQuitConfirmation()
  const expectedError = new Error(`synthetic ${failurePhase} failure`)
  const failed = confirmation.request({
    check: () => {
      if (failurePhase === 'check') throw expectedError
      return Promise.resolve('active')
    },
    confirm: async () => { throw expectedError },
  })
  assert.equal(confirmation.request({
    check: unexpected('a repeated failing check'),
    confirm: unexpected('a repeated failing prompt'),
  }), failed)
  await assert.rejects(failed, error => error === expectedError)
  assert.equal(await confirmation.request({
    check: async () => 'idle',
    confirm: unexpected('an idle prompt after an error'),
  }), true, `${failurePhase} errors must release the confirmation operation for retry`)
}

console.log('desktop quit budget and confirmation checks passed')
