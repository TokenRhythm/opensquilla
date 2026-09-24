import assert from 'node:assert/strict'
import { test } from 'node:test'
import { DesktopBrowserMcp } from '../dist/desktop-browser-mcp.js'
import { DesktopBrowserError } from '../dist/desktop-browser.js'

const coordinate = { targetRef: 'page-fixture', actions: [{ action: 'click', x: 30, y: 40,
  observationId: 'observation-fixture', imageId: 'image-fixture' }] }

for (const returnedFailure of [false, true]) {
  // Old adapters may still report IMAGE_NOT_DELIVERED. Current screenshot
  // freshness refusals must retain the same bounded recovery behavior.
  for (const finalCode of ['STALE_OBSERVATION', 'IMAGE_NOT_DELIVERED']) {
  test(`visual recovery stays bounded across observations (${returnedFailure ? 'result' : 'exception'}, ${finalCode})`, async () => {
    let attempts = 0
    const mcp = new DesktopBrowserMcp(async request => {
      if (request.operation === 'batch' && request.actions?.some(action => action.x !== undefined)) {
        attempts++
        const code = attempts === 1 ? 'STALE_OBSERVATION' : finalCode
        if (!returnedFailure) throw new DesktopBrowserError(code, 'Synthetic visual refusal.', 409,
          { outcome: 'not_started', recovery: 'observe' })
        return { targetRef: request.targetRef, execution: { state: 'failed', actions: [
          { code, outcome: 'not_started', performed: false, message: 'Synthetic visual refusal.' },
        ] }, observation: { consistency: 'consistent' } }
      }
      return { targetRef: request.targetRef, observation: { consistency: 'consistent' } }
    })
    let id = 0
    const call = async (name, args, scope = 'turn-fixture') => {
      const result = await mcp.handle({ jsonrpc: '2.0', id: ++id, method: 'tools/call', params: {
        name, arguments: args, _meta: { sessionKey: 'session-fixture', operationId: `call-${id}`, recoveryScope: scope },
      } }, new AbortController().signal)
      assert.ok(result.result, JSON.stringify(result))
      return result.result.structuredContent
    }
    for (let attempt = 1; attempt <= 2; attempt++) {
      const failed = await call('browser_batch', coordinate)
      assert.equal(failed.outcome, 'not_started')
      assert.equal(failed.recoveryBudget.attempts, attempt)
      assert.equal(failed.recoveryBudget.domain, 'visual')
      await call('browser_observe', { targetRef: coordinate.targetRef })
      await call('browser_tab', { targetRef: coordinate.targetRef, tabAction: 'switch' })
    }
    const stopped = await call('browser_batch', coordinate)
    assert.equal(stopped.code, 'BROWSER_RECOVERY_EXHAUSTED')
    assert.equal(attempts, 2, 'no third coordinate action reaches the driver')
    const cleaned = await call('browser_batch', { targetRef: coordinate.targetRef,
      actions: [{ action: 'click', ref: 'element-fixture' }] })
    assert.notEqual(cleaned.ok, false, 'DOM cleanup remains available')
    assert.equal((await call('browser_batch', coordinate)).code, 'BROWSER_RECOVERY_EXHAUSTED')
    await call('browser_batch', coordinate, 'next-turn-fixture')
    assert.equal(attempts, 3, 'a new trusted turn has its own budget')
  })
  }
}

test('a skipped batch tail is not reported as an executed action', async () => {
  const mcp = new DesktopBrowserMcp(async () => ({ targetRef: 'page-fixture', execution: {
    state: 'failed', actions: [
      { code: 'STALE_ELEMENT', outcome: 'not_started', performed: false },
      { state: 'not_started' },
    ],
  } }))
  const result = await mcp.handle({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: {
    name: 'browser_batch', arguments: { targetRef: 'page-fixture', actions: [
      { action: 'fill', ref: 'input-fixture', text: 'sample' }, { action: 'click', ref: 'button-fixture' },
    ] }, _meta: { sessionKey: 'session-fixture', operationId: 'call-fixture' },
  } }, new AbortController().signal)
  assert.equal(result.result.structuredContent.outcome, 'not_started')
})

test('completed form fields cannot reset a failing final visual action budget', async () => {
  let attempts = 0
  const mcp = new DesktopBrowserMcp(async () => {
    attempts++
    return { targetRef: 'page-fixture', execution: { state: 'partial', actions: [
      { action: 'fill', performed: true },
      { action: 'click', performed: false, code: 'STALE_OBSERVATION', outcome: 'not_started' },
    ] }, observation: { consistency: 'consistent' } }
  })
  const call = async id => (await mcp.handle({ jsonrpc: '2.0', id, method: 'tools/call', params: {
    name: 'browser_batch', arguments: { ...coordinate, actions: [
      { action: 'fill', ref: 'input-fixture', text: 'sample' }, ...coordinate.actions,
    ] }, _meta: { sessionKey: 'session-fixture', operationId: `partial-${id}` },
  } }, new AbortController().signal)).result.structuredContent
  for (let id = 1; id <= 2; id++) {
    const result = await call(id)
    assert.equal(result.outcome, 'completed', 'the fill was executed')
    assert.equal(result.execution.state, 'partial', 'the visual click was not executed')
    assert.equal(result.execution.actions[1].outcome, 'not_started')
    assert.equal(result.recoveryBudget.domain, 'visual')
    assert.equal(result.recoveryBudget.attempts, id)
  }
  assert.equal((await call(3)).code, 'BROWSER_RECOVERY_EXHAUSTED')
  assert.equal(attempts, 2)
})
