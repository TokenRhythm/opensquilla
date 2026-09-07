import assert from 'node:assert/strict'

import { assertConcurrentRecoveryTransport } from './session-recovery-transport-contract.mjs'

const healthy = Object.freeze({
  concurrentHistoryReads: true,
  socketCount: 1,
  newSocketCount: 0,
  closeCount: 0,
})

assert.deepEqual(assertConcurrentRecoveryTransport(healthy), healthy)

const rejected = [
  [{ concurrentHistoryReads: false }, /hello must advertise concurrent history reads/],
  [{ concurrentHistoryReads: null }, /hello must advertise concurrent history reads/],
  [{ concurrentHistoryReads: 'true' }, /hello must advertise concurrent history reads/],
  [{ socketCount: 0 }, /exactly one target WebSocket/],
  [{ socketCount: 2 }, /exactly one target WebSocket/],
  [{ newSocketCount: 1 }, /must not create a replacement WebSocket/],
  [{ closeCount: 1 }, /must not close the healthy WebSocket/],
]

for (const [overrides, message] of rejected) {
  assert.throws(
    () => assertConcurrentRecoveryTransport({ ...healthy, ...overrides }),
    { name: 'AssertionError', message },
    JSON.stringify(overrides),
  )
}

console.log('Session recovery transport contracts passed (8 cases)')
