import assert from 'node:assert/strict'

import {
  cleanupSelectorArgs,
  DesktopCleanupPreviewStore,
  desktopCleanupScopeIsContained,
  parseDesktopCleanupMode,
  parseDesktopCleanupProtocol,
  sameDesktopCleanupScope,
} from '../dist/desktop-cleanup.js'

const report = parseDesktopCleanupProtocol({
  schema_version: 1,
  outcome: 'ready',
  stable_code: 'cleanup_ready',
  mode: 'delete-current-profile',
  items: [{
    kind: 'primary-home',
    path: '/synthetic/user-data/opensquilla',
    exists: true,
    identity: '1:2',
  }],
  transaction_id: 'synthetic-transaction',
  revision: 42,
  scope_fingerprint: 'a'.repeat(64),
})

assert.equal(parseDesktopCleanupMode('reset-current-settings'), 'reset-current-settings')
assert.equal(parseDesktopCleanupMode('delete-current-profile'), 'delete-current-profile')
assert.equal(parseDesktopCleanupMode('delete-all-user-data'), 'delete-all-user-data')
assert.equal(parseDesktopCleanupMode('purge'), null)
assert.equal(report.items[0].identity, '1:2')
assert.equal(desktopCleanupScopeIsContained(report, '/synthetic/user-data'), true)
assert.equal(desktopCleanupScopeIsContained({
  ...report,
  items: [{ ...report.items[0], path: '/synthetic/outside' }],
}, '/synthetic/user-data'), false)
assert.equal(desktopCleanupScopeIsContained({
  ...report,
  items: [{ ...report.items[0], path: '/synthetic/user-data/..cache' }],
}, '/synthetic/user-data'), true)
assert.equal(sameDesktopCleanupScope(report, {
  ...report,
  revision: 99,
  items: report.items.map((item) => ({ ...item, identity: '3:4' })),
}, '/synthetic/user-data'), true, 'content metadata may change while the gateway stops')
assert.equal(sameDesktopCleanupScope(report, {
  ...report,
  items: [...report.items, {
    kind: 'new-user-data-entry',
    path: '/synthetic/user-data/new-entry',
    exists: true,
    identity: '5:6',
  }],
}, '/synthetic/user-data'), false, 'new paths require a new visible inventory')

assert.throws(
  () => parseDesktopCleanupProtocol({ ...report, schema_version: 2 }),
  /unsupported protocol schema/,
)
assert.throws(
  () => parseDesktopCleanupProtocol({ ...report, revision: Number.MAX_SAFE_INTEGER + 1 }),
  /invalid revision/,
)
assert.throws(
  () => parseDesktopCleanupProtocol({ ...report, scope_fingerprint: 'not-a-digest' }),
  /invalid scope fingerprint/,
)
assert.throws(
  () => parseDesktopCleanupProtocol({ ...report, items: [{ path: '/unbound' }] }),
  /invalid inventory item/,
)

const primarySelection = {
  mode: 'delete-current-profile',
  profileKey: 'primary',
}
assert.deepEqual(cleanupSelectorArgs(primarySelection), [
  '--mode', 'delete-current-profile',
  '--profile-kind', 'primary',
])

const store = new DesktopCleanupPreviewStore(1_000)
const preview = store.create(report, primarySelection, 100)
assert.equal(
  store.consume(preview.id, 'stale-primary-context', 200),
  null,
  'a preview must be bound to the exact primary context',
)
assert.equal(
  store.consume(preview.id, 'primary', 200),
  null,
  'a rejected preview is consumed and cannot be replayed',
)

const fresh = store.create(report, primarySelection, 100)
assert.equal(store.consume(fresh.id, 'primary', 1_101), null, 'expired previews fail closed')

const discardable = store.create(report, primarySelection, 100)
assert.equal(store.discard(discardable.id, 'stale-primary-context'), false)
assert.equal(
  store.consume(discardable.id, 'primary', 200)?.id,
  discardable.id,
  'a stale renderer cannot discard the current primary preview',
)
const explicitlyDiscarded = store.create(report, primarySelection, 100)
assert.equal(store.discard(explicitlyDiscarded.id, 'primary'), true)
assert.equal(
  store.consume(explicitlyDiscarded.id, 'primary', 200),
  null,
  'cancelled previews cannot be replayed',
)

const accepted = store.create(report, primarySelection, 100)
assert.equal(
  store.consume(accepted.id, 'primary', 200)?.report.transaction_id,
  'synthetic-transaction',
)
assert.equal(store.consume(accepted.id, 'primary', 201), null, 'previews are one-shot')

console.log('desktop cleanup contract checks passed')
