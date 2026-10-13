import assert from 'node:assert/strict'
import { createBrowserReloadGuard } from '../dist/desktop-browser-reload-guard.js'

function deferred() {
  let resolve
  const promise = new Promise(yes => { resolve = yes })
  return { promise, resolve }
}

const closing = deferred()
let closeCalls = 0
let firstReloads = 0
let secondReloads = 0
const guard = createBrowserReloadGuard(() => {
  closeCalls++
  return closing.promise
})
const first = guard(() => { firstReloads++ })
const duplicate = guard(() => { secondReloads++ })
assert.strictEqual(first, duplicate, 'concurrent callers share one close decision')
await Promise.resolve()
assert.equal(closeCalls, 1)
closing.resolve(true)
assert.equal(await first, true)
assert.equal(await duplicate, true)
assert.equal(firstReloads, 1)
assert.equal(secondReloads, 0)

let accepted = false
let reloads = 0
const retry = createBrowserReloadGuard(async () => accepted)
assert.equal(await retry(() => { reloads++ }), false)
assert.equal(reloads, 0, 'cancelled closure preserves the current UI')
accepted = true
assert.equal(await retry(() => { reloads++ }), true)
assert.equal(reloads, 1, 'a later explicit request can reload')

const closeFailure = createBrowserReloadGuard(async () => { throw new Error('close failed') })
assert.equal(await closeFailure(() => { reloads++ }), false)
assert.equal(reloads, 1)

const reloadFailure = createBrowserReloadGuard(async () => true)
assert.equal(await reloadFailure(() => { throw new Error('reload failed') }), false)
assert.equal(await reloadFailure(() => { reloads++ }), true)
assert.equal(reloads, 2)

console.log('Browser reload guard passed: single flight, cancellation, retry and failure isolation.')
