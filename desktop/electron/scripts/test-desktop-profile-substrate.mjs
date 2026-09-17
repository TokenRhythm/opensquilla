import assert from 'node:assert/strict'

import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'

const writers = new DesktopWriterAdmission()
const finishExistingWriter = writers.begin('existing recovery writer')
const lifecycleOwner = writers.close('apply downloaded update')
assert.equal(writers.closed, true)
assert.equal(writers.hasOwner(lifecycleOwner), true)
assert.throws(
  () => writers.begin('late context writer'),
  /writer admission is closed/,
)

let drained = false
const drain = writers.waitForAtMost(0).then(() => {
  drained = true
})
await Promise.resolve()
assert.equal(drained, false, 'lifecycle operation must wait for the active writer')
finishExistingWriter()
finishExistingWriter()
await drain
assert.equal(writers.activeCount, 0, 'writer completion must be idempotent')
assert.equal(writers.reopen(Symbol('unrelated owner')), false)
assert.equal(writers.closed, true, 'an unrelated owner must not reopen admission')
assert.equal(writers.reopen(lifecycleOwner), true)
assert.equal(writers.closed, false)
assert.throws(() => writers.waitForAtMost(-1), /non-negative integer/)

const exclusive = writers.tryBeginExclusive('recovery selection')
assert(exclusive, 'exclusive admission must atomically close and reserve a writer')
assert.equal(writers.closed, true)
assert.equal(writers.activeCount, 1)
assert.equal(writers.tryBeginExclusive('second recovery selection'), null)
exclusive.finish()
writers.reopen(exclusive.admissionToken)
assert.equal(writers.closed, false)
assert.equal(writers.activeCount, 0)

console.log('desktop profile substrate checks passed')
