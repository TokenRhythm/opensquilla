import assert from 'node:assert/strict'

// The current Gateway admits concurrent history reads. A held session RPC must
// fail locally without recycling the healthy transport shared by other reads.
export function assertConcurrentRecoveryTransport({
  concurrentHistoryReads,
  socketCount,
  newSocketCount,
  closeCount,
}) {
  assert.equal(
    concurrentHistoryReads,
    true,
    'candidate Gateway hello must advertise concurrent history reads',
  )
  assert.equal(socketCount, 1, 'session recovery must stay on exactly one target WebSocket')
  assert.equal(newSocketCount, 0, 'session recovery must not create a replacement WebSocket')
  assert.equal(closeCount, 0, 'session recovery must not close the healthy WebSocket')
  return { concurrentHistoryReads, socketCount, newSocketCount, closeCount }
}
