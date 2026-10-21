import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from '@typescript/typescript6'

// Exercise the shipped batch-close method without booting Electron. Native
// beforeunload behavior is covered by test-browser-host-navigation-electron.
const source = ts.createSourceFile(
  'native-workbench-surface.js',
  readFileSync(new URL('../dist/native-workbench-surface.js', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.JS,
)
let closeSource
function collect(node) {
  if (ts.isMethodDeclaration(node) && node.name?.getText(source) === 'closeBrowserTabs') {
    closeSource = node.getText(source)
  }
  ts.forEachChild(node, collect)
}
collect(source)
assert.ok(closeSource, 'Expected the production browser batch-close method.')

function deferred() {
  let resolve
  const promise = new Promise(accept => { resolve = accept })
  return { promise, resolve }
}
const flush = () => new Promise(resolve => setImmediate(resolve))

function record(id) {
  const state = { url: `https://example.test/${id}`, destroyed: false }
  return {
    id, state, kind: 'url-preview', disposed: false,
    annotationDocumentGeneration: 1,
    owner: { isDestroyed: () => false },
    contents: {
      isDestroyed: () => state.destroyed,
      getURL: () => state.url,
      isLoadingMainFrame: () => false,
    },
  }
}

function harness(count = 2) {
  const records = Array.from({ length: count }, (_, index) => record(`page-${index}`))
  const closed = []
  const grants = []
  let bulkPrompts = 0
  let bulkAnswer = 0
  const env = {
    app: { getLocale: () => 'en-US' },
    dialog: { showMessageBoxSync: () => { bulkPrompts += 1; return bulkAnswer } },
  }
  const closeBrowserTabs = new Function('env', `with (env) { return ({ ${closeSource} }).closeBrowserTabs }`)(env)
  const manager = {
    surfaces: new Map(records.map(entry => [entry.id, entry])),
    browserTabsClosePromise: undefined,
    browserTabsCloseApproved: undefined,
    hasBrowserTabs() {
      return [...this.surfaces.values()].some(entry => !entry.disposed && !entry.contents.isDestroyed())
    },
    async requestBrowserClose(entry) {
      const grant = this.browserTabsCloseApproved
      assert.equal(grant.record, entry, 'Only the exact approved record enters native close.')
      assert.equal(grant.url, entry.contents.getURL())
      assert.equal(grant.generation, entry.annotationDocumentGeneration)
      grants.push(grant)
      closed.push(entry.id)
      entry.state.destroyed = true
      entry.disposed = true
      this.surfaces.delete(entry.id)
      return true
    },
    destroyRecord: () => assert.fail('Successful native close needs no destruction fallback.'),
  }
  manager.closeBrowserTabs = closeBrowserTabs
  return {
    records, manager, closed, grants,
    get bulkPrompts() { return bulkPrompts },
    setBulkAnswer: answer => { bulkAnswer = answer },
  }
}

// Canceling the unified decision retains every page and never starts a native
// close, even while the decision is pending.
{
  const h = harness()
  const answer = deferred()
  const counts = []
  const closing = h.manager.closeBrowserTabs({ confirm: snapshot => {
    counts.push(snapshot.tabCount)
    return answer.promise
  } })
  await flush()
  assert.deepEqual(counts, [2])
  assert.deepEqual(h.closed, [])
  assert.equal(h.manager.surfaces.size, 2)
  answer.resolve(false)
  assert.equal(await closing, false)
  assert.deepEqual(h.closed, [])
  assert.equal(h.bulkPrompts, 0, 'Custom consent replaces the separate bulk prompt.')
  assert.equal(h.manager.browserTabsClosePromise, undefined)

  let retryCount = 0
  assert.equal(await h.manager.closeBrowserTabs({ confirm: () => { retryCount += 1; return true } }), true)
  assert.equal(retryCount, 1, 'A canceled operation cannot authorize a later retry.')
  assert.deepEqual(h.closed, ['page-0', 'page-1'])
  assert.equal(h.grants.length, 2)
  assert.equal(h.bulkPrompts, 0)
  assert.equal(h.manager.browserTabsCloseApproved, undefined)
}

// Every mutation of the approved batch requires a fresh user decision before
// closing even its first page.
for (const mutation of ['new-page', 'navigation', 'same-url-document', 'replacement', 'removed-page']) {
  const h = harness()
  const answer = deferred()
  const closing = h.manager.closeBrowserTabs({ confirm: () => answer.promise })
  await flush()
  const first = h.records[0]
  if (mutation === 'new-page') h.manager.surfaces.set('late-page', record('late-page'))
  else if (mutation === 'navigation') first.state.url += '/new'
  else if (mutation === 'same-url-document') first.annotationDocumentGeneration += 1
  else if (mutation === 'replacement') h.manager.surfaces.set(first.id, record(first.id))
  else h.manager.surfaces.delete(first.id)
  answer.resolve(true)
  assert.equal(await closing, false, mutation)
  assert.deepEqual(h.closed, [], `${mutation}: no page inherits stale consent`)
  assert.equal(h.bulkPrompts, 0)
}

for (const addPage of [false, true]) {
  const h = harness(0)
  const answer = deferred()
  const counts = []
  const closing = h.manager.closeBrowserTabs({ confirm: ({ tabCount }) => {
    counts.push(tabCount)
    return answer.promise
  } })
  await flush()
  assert.deepEqual(counts, [0], 'Tasks can require a unified decision with no browser pages.')
  if (addPage) h.manager.surfaces.set('late-page', record('late-page'))
  answer.resolve(true)
  assert.equal(await closing, !addPage, 'A page opened during task confirmation needs new consent.')
  assert.deepEqual(h.closed, [])
}

// A reload already owns its own batch decision. A new quit must not borrow its
// result and thereby skip the quit caller's task confirmation.
{
  const h = harness()
  const existing = deferred()
  h.manager.browserTabsClosePromise = existing.promise
  let confirmations = 0
  assert.equal(await h.manager.closeBrowserTabs({ confirm: () => { confirmations += 1; return true } }), false)
  assert.equal(confirmations, 0)
  assert.deepEqual(h.closed, [])
  const joined = h.manager.closeBrowserTabs()
  existing.resolve(false)
  assert.equal(await joined, false, 'Ordinary reload calls still join their current batch.')
}

// A failed decision releases the batch lock without closing a page.
{
  const h = harness()
  await assert.rejects(h.manager.closeBrowserTabs({ confirm: async () => {
    throw new Error('Synthetic confirmation failure')
  } }), /Synthetic confirmation failure/)
  assert.equal(h.manager.browserTabsClosePromise, undefined)
  assert.deepEqual(h.closed, [])
}

// The no-options path keeps the original safe default and native batch dialog.
{
  const h = harness()
  const results = await Promise.all([h.manager.closeBrowserTabs(), h.manager.closeBrowserTabs()])
  assert.deepEqual(results, [false, false])
  assert.equal(h.bulkPrompts, 1)
  assert.deepEqual(h.closed, [])
  h.setBulkAnswer(1)
  assert.equal(await h.manager.closeBrowserTabs(), true)
  assert.equal(h.bulkPrompts, 2)
  assert.deepEqual(h.closed, ['page-0', 'page-1'])
  const empty = harness(0)
  assert.equal(await empty.manager.closeBrowserTabs(), true)
  assert.equal(empty.bulkPrompts, 0)
}

console.log('Browser quit confirmation contracts passed: consent precedes native close and binds to the exact page batch.')
