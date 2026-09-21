import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import { build, normalizePath } from 'vite'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

// No Gateway, live account, or main application is involved. Bundle the actual
// production modules and serve them entirely through Playwright interception.
const ORIGIN = 'http://127.0.0.1:44177'
const DATABASE = 'opensquilla-chat-pending-inputs'
let fixtureModule: string

test.beforeAll(async () => {
  const root = fileURLToPath(new URL('..', import.meta.url))
  const source = (path: string) => JSON.stringify(normalizePath(resolve(root, path)))
  const entry = '\0durable-delivery-browser-fixture'
  const output = await build({
    root, configFile: false, logLevel: 'silent',
    resolve: { alias: { '@': resolve(root, 'src') } },
    plugins: [{
      name: 'durable-delivery-browser-fixture',
      resolveId: id => id === entry ? entry : undefined,
      load: id => id === entry ? `
        import { createPendingInputWal } from ${source('src/utils/chat/pendingInputWal.ts')};
        import { createDurableDelivery } from ${source('src/runtime/durableDelivery.ts')};
        import { TurnCommandError } from ${source('src/modules/turnCommands.ts')};
        window.deliveryFixture = { createPendingInputWal, createDurableDelivery, TurnCommandError };
        window.deliveryFixture.wal = createPendingInputWal();
      ` : undefined,
    }],
    build: { write: false, minify: false, rollupOptions: { input: entry, output: { format: 'es' } } },
  })
  const outputs = Array.isArray(output) ? output : [output]
  const chunks = outputs.flatMap(item => {
    if (!('output' in item)) throw new Error('Fixture build unexpectedly started a watcher')
    return item.output.filter(asset => asset.type === 'chunk')
  })
  expect(chunks).toHaveLength(1)
  fixtureModule = chunks[0]!.code
})

test.beforeEach(async ({ context }) => {
  await context.route('**/*', async route => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/delivery-fixture.mjs') {
      await route.fulfill({ contentType: 'text/javascript', body: fixtureModule })
    } else if (pathname === '/delivery-fixture.html') {
      await route.fulfill({ contentType: 'text/html', body:
        '<!doctype html><title>Synthetic durable delivery fixture</title><script type="module" src="/delivery-fixture.mjs"></script>',
      })
    } else await route.fulfill({ status: 404, body: 'No network in this fixture' })
  })
})

async function fixturePage(context: BrowserContext): Promise<Page> {
  const page = await context.newPage()
  await page.goto(`${ORIGIN}/delivery-fixture.html`)
  await page.waitForFunction(() => !!(window as any).deliveryFixture?.wal)
  return page
}

function deliveryRecord(id = 'synthetic-delivery') {
  return {
    schemaVersion: 2, ownerRequestId: id, deliveryIdentity: 'synthetic-identity',
    requestSessionKey: 'synthetic-session', phase: 'unknown', revision: 1,
    createdAt: 1, updatedAt: 1,
    request: { kind: 'send', request: { kind: 'new-turn', params: {
      sessionKey: 'synthetic-session', clientRequestId: id,
      clientMessageId: `message-${id}`, message: 'Synthetic recovery input',
    } } },
  }
}

async function installOwner(page: Page, identity = 'synthetic-identity', holdLookup = false) {
  await page.evaluate(({ identity, holdLookup }) => {
    const fixture = (window as any).deliveryFixture
    fixture.identity = identity
    fixture.generation = 1
    fixture.calls = { sends: 0, lookups: 0, cancels: [] }
    fixture.owner = fixture.createDurableDelivery({
      wal: fixture.wal,
      access: {
        identity: () => fixture.identity, available: () => true,
        generation: () => fixture.generation,
      },
      commands: {
        send: async () => { fixture.calls.sends += 1; throw new Error('Recovery must not admit') },
        steer: async () => { throw new Error('Recovery must not steer') },
        supports: () => true, supportsReceiptLookup: () => true,
        lookupReceipt: async () => {
          fixture.calls.lookups += 1
          const found = { status: 'found', response: {
            taskId: 'synthetic-recovered-task', sessionKey: 'synthetic-handoff-target',
          } }
          if (holdLookup) return new Promise(resolve => { fixture.resolveLookup = () => resolve(found) })
          return found
        },
        cancel: async (request: unknown) => {
          fixture.calls.cancels.push(request)
          return { aborted: true }
        },
      },
    })
  }, { identity, holdLookup })
}

test('v3 preserves v2 handoffs in quarantine and old clients fail without resetting data', async ({ context }) => {
  const page = await fixturePage(context)
  const result = await page.evaluate(async databaseName => {
    const fixture = (window as any).deliveryFixture
    const legacy = { schemaVersion: 1, ownerRequestId: 'synthetic-v2-handoff',
      requestSessionKey: 'synthetic-session', clientRequestId: 'synthetic-v2-handoff',
      clientMessageId: 'synthetic-message', params: { message: 'Synthetic legacy draft' },
      composerText: 'Synthetic legacy draft', recoveryAttachments: [], state: 'submitting',
      createdAt: 1, updatedAt: 1 }
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 2)
      request.onupgradeneeded = () => {
        request.result.createObjectStore('pending_chat_inputs', { keyPath: 'pendingInputId' })
          .createIndex('session_created', ['sessionKey', 'createdAt'])
        const store = request.result.createObjectStore('response_handoffs', { keyPath: 'ownerRequestId' })
        store.put(legacy)
        store.put({ schemaVersion: 77, ownerRequestId: 'synthetic-future-record', payload: 'Synthetic future material' })
      }
      request.onerror = () => reject(request.error)
      request.onsuccess = () => { request.result.close(); resolve() }
    })
    const quarantined = await fixture.wal.countQuarantinedDeliveries()
    const recovery = await fixture.wal.listRecoveryDeliveries()
    const oldClientError = await new Promise<string>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 2)
      request.onerror = () => resolve(request.error!.name)
      request.onsuccess = () => { request.result.close(); reject(new Error('Old client unexpectedly opened v3')) }
    })
    const stored = await new Promise<unknown[]>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 3)
      request.onerror = () => reject(request.error)
      request.onsuccess = () => {
        const database = request.result
        const read = database.transaction('response_handoffs').objectStore('response_handoffs').getAll()
        read.onsuccess = () => { database.close(); resolve(read.result) }
        read.onerror = () => reject(read.error)
      }
    })
    return { quarantined, recovery, oldClientError, stored, legacy }
  }, DATABASE)
  expect(result.quarantined).toBe(2)
  expect(result.recovery.records).toEqual([])
  expect(result.oldClientError).toBe('VersionError')
  expect(result.stored).toContainEqual(result.legacy)
  expect(result.stored).toContainEqual({ schemaVersion: 77, ownerRequestId: 'synthetic-future-record', payload: 'Synthetic future material' })
})

test('a blocked open closes its late connection without replacing a newer open', async ({ context }) => {
  const blocker = await fixturePage(context)
  await blocker.evaluate(async databaseName => {
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 2)
      request.onupgradeneeded = () => {
        request.result.createObjectStore('pending_chat_inputs', { keyPath: 'pendingInputId' })
          .createIndex('session_created', ['sessionKey', 'createdAt'])
        request.result.createObjectStore('response_handoffs', { keyPath: 'ownerRequestId' })
      }
      request.onerror = () => reject(request.error)
      request.onsuccess = () => { (window as any).deliveryFixture.blocker = request.result; resolve() }
    })
  }, DATABASE)
  const page = await fixturePage(context)
  await page.evaluate(() => {
    const fixture = (window as any).deliveryFixture
    fixture.closedVersions = []
    const originalClose = IDBDatabase.prototype.close
    IDBDatabase.prototype.close = function () {
      fixture.closedVersions.push(this.version)
      return originalClose.call(this)
    }
  })
  const blocked = await page.evaluate(async () => {
    try { await (window as any).deliveryFixture.wal.countQuarantinedDeliveries(); return '' }
    catch (error) { return String(error) }
  })
  expect(blocked).toContain('blocked')
  await page.evaluate(() => {
    const fixture = (window as any).deliveryFixture
    fixture.reopened = fixture.wal.countQuarantinedDeliveries().then((count: number) => { fixture.reopenedCount = count })
  })
  await blocker.evaluate(() => (window as any).deliveryFixture.blocker.close())
  await expect.poll(() => page.evaluate(() => (window as any).deliveryFixture.reopenedCount)).toBe(0)
  expect(await page.evaluate(() => (window as any).deliveryFixture.closedVersions)).toContain(3)
  expect(await page.evaluate(() => (window as any).deliveryFixture.wal.listRecoveryDeliveries())).toEqual({ records: [] })
})

test('two tabs serialize real IndexedDB compare-and-swap without overwriting the winner', async ({ context }) => {
  const first = await fixturePage(context)
  const second = await fixturePage(context)
  const record = deliveryRecord()
  await first.evaluate(record => (window as any).deliveryFixture.wal.prepareDelivery(record), record)
  const results = await Promise.all([first, second].map((page, index) => page.evaluate(async ({ record, index }) => {
    return (window as any).deliveryFixture.wal.compareAndSwapDelivery(record.ownerRequestId, 1, {
      ...record, revision: 2, lease: { owner: `synthetic-owner-${index}`, epoch: 1, expiresAt: Date.now() + 60_000 },
      ...(index === 0 ? { stop: { requested: true } } : {}),
    })
  }, { record, index })))
  expect(results.filter(result => result.applied)).toHaveLength(1)
  const winner = results.find(result => result.applied)!.record
  const final = await second.evaluate(id => (window as any).deliveryFixture.wal.getDelivery(id), record.ownerRequestId)
  expect(final).toMatchObject(winner)
  const stale = await first.evaluate(record => (window as any).deliveryFixture.wal.compareAndSwapDelivery(
    record.ownerRequestId, 1, { ...record, revision: 2, phase: 'not-sent' },
  ), record)
  expect(stale.applied).toBe(false)
  expect(stale.record).toMatchObject(winner)
})

test('a stale not-sent view cannot resend after another tab records unknown acceptance', async ({ context }) => {
  const first = await fixturePage(context)
  const second = await fixturePage(context)
  const record = { ...deliveryRecord(), phase: 'not-sent' }
  await first.evaluate(record => (window as any).deliveryFixture.wal.prepareDelivery(record), record)
  await installOwner(first)
  await first.evaluate(record => {
    const fixture = (window as any).deliveryFixture
    const prepare = fixture.wal.prepareDelivery.bind(fixture.wal)
    fixture.wal.prepareDelivery = async (...args: unknown[]) => {
      const previous = await prepare(...args)
      fixture.prepared = true
      await new Promise<void>(resolve => { fixture.releasePrepare = resolve })
      return previous
    }
    fixture.dispatch = fixture.owner.commands.send(record.request.request)
      .then((response: unknown) => { fixture.response = response }, (error: unknown) => { fixture.dispatchError = String(error) })
  }, record)
  await expect.poll(() => first.evaluate(() => (window as any).deliveryFixture.prepared)).toBe(true)
  const changed = await second.evaluate(record => (window as any).deliveryFixture.wal.compareAndSwapDelivery(
    record.ownerRequestId, 1, { ...record, revision: 2, phase: 'unknown' },
  ), record)
  expect(changed.applied).toBe(true)
  await first.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    fixture.releasePrepare()
    await fixture.dispatch
  })
  expect(await first.evaluate(() => (window as any).deliveryFixture.calls.sends)).toBe(0)
  expect(await first.evaluate(() => (window as any).deliveryFixture.response?.taskId)).toBe('synthetic-recovered-task')
})

test('two application owners share one receipt lookup lease and never resend unknown input', async ({ context }) => {
  const first = await fixturePage(context)
  const second = await fixturePage(context)
  await first.evaluate(record => (window as any).deliveryFixture.wal.prepareDelivery(record), deliveryRecord())
  await installOwner(first, 'synthetic-identity', true)
  await installOwner(second, 'synthetic-identity', true)
  await Promise.all([first, second].map(page => page.evaluate(() => {
    const fixture = (window as any).deliveryFixture
    fixture.wakePromise = fixture.owner.wake()
  })))
  await expect.poll(async () => (await Promise.all([first, second].map(page => page.evaluate(() => (window as any).deliveryFixture.calls.lookups))))
    .reduce((total, count) => total + count, 0)).toBe(1)
  await Promise.all([first, second].map(page => page.evaluate(() => (window as any).deliveryFixture.resolveLookup?.())))
  await expect.poll(() => first.evaluate(async () => (await (window as any).deliveryFixture.wal.getDelivery('synthetic-delivery')).phase)).toBe('accepted')
  expect(await first.evaluate(() => (window as any).deliveryFixture.calls.sends)).toBe(0)
  expect(await second.evaluate(() => (window as any).deliveryFixture.calls.sends)).toBe(0)
})

test('closing and reopening a tab recovers exact Stop only under its original identity', async ({ context }) => {
  const initial = await fixturePage(context)
  await initial.evaluate(record => (window as any).deliveryFixture.wal.prepareDelivery({ ...record, stop: { requested: true } }), deliveryRecord())
  await initial.close()
  const reopened = await fixturePage(context)
  await installOwner(reopened, 'different-synthetic-identity')
  await reopened.evaluate(() => (window as any).deliveryFixture.owner.wake())
  expect(await reopened.evaluate(() => (window as any).deliveryFixture.calls)).toEqual({ sends: 0, lookups: 0, cancels: [] })
  await reopened.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    fixture.identity = 'synthetic-identity'
    fixture.generation += 1
    await fixture.owner.wake()
  })
  const state = await reopened.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    return { record: await fixture.wal.getDelivery('synthetic-delivery'), calls: fixture.calls }
  })
  expect(state.record.phase).toBe('accepted')
  expect(state.record.stop.completed).toBe(true)
  expect(state.calls.sends).toBe(0)
  expect(state.calls.lookups).toBe(1)
  expect(state.calls.cancels).toEqual([{
    sessionKey: 'synthetic-handoff-target', taskId: 'synthetic-recovered-task', source: 'webui_stop', scope: 'task',
  }])
})

test('a later database upgrade invalidates the existing WAL and application owner', async ({ context }) => {
  const page = await fixturePage(context)
  await installOwner(page)
  await page.evaluate(() => (window as any).deliveryFixture.wal.countQuarantinedDeliveries())
  await page.evaluate(async databaseName => {
    await new Promise<void>((resolve, reject) => {
      const upgrade = indexedDB.open(databaseName, 4)
      upgrade.onerror = () => reject(upgrade.error)
      upgrade.onsuccess = () => { upgrade.result.close(); resolve() }
    })
  }, DATABASE)
  const outcome = await page.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    let error = ''
    try { await fixture.wal.listRecoveryDeliveries() } catch (failure) { error = String(failure) }
    await fixture.owner.wake()
    return { error, snapshots: fixture.owner.snapshots(), calls: fixture.calls }
  })
  expect(outcome.error).toContain('reload')
  expect(outcome.snapshots).toContainEqual(expect.objectContaining({ waitReason: 'reload' }))
  expect(outcome.calls).toEqual({ sends: 0, lookups: 0, cancels: [] })
})

test('an aborted native v2 to v3 upgrade preserves both stores and can retry without resetting the database', async ({ context }) => {
  const page = await fixturePage(context)
  const result = await page.evaluate(async databaseName => {
    const fixture = (window as any).deliveryFixture
    const pending = { schemaVersion: 1, pendingInputId: 'synthetic-v2-draft', sessionKey: 'synthetic-session',
      clientRequestId: 'synthetic-v2-request', clientMessageId: 'synthetic-v2-message', text: 'Synthetic retained draft',
      attachments: [], intent: null, state: 'local_only', createdAt: 1, updatedAt: 1 }
    const handoff = { schemaVersion: 1, ownerRequestId: 'synthetic-v2-handoff', requestSessionKey: 'synthetic-session',
      clientRequestId: 'synthetic-v2-handoff', clientMessageId: 'synthetic-message', params: { message: 'Synthetic handoff' },
      composerText: 'Synthetic handoff', recoveryAttachments: [], state: 'submitting', createdAt: 1, updatedAt: 1 }
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 2)
      request.onupgradeneeded = () => {
        const drafts = request.result.createObjectStore('pending_chat_inputs', { keyPath: 'pendingInputId' })
        drafts.createIndex('session_created', ['sessionKey', 'createdAt'])
        drafts.put(pending)
        request.result.createObjectStore('response_handoffs', { keyPath: 'ownerRequestId' }).put(handoff)
      }
      request.onerror = () => reject(request.error)
      request.onsuccess = () => { request.result.close(); resolve() }
    })
    const originalOpen = IDBFactory.prototype.open
    const originalDelete = IDBFactory.prototype.deleteDatabase
    let deleteCalls = 0
    let interrupted = false
    let attemptedIndexes: string[] = []
    IDBFactory.prototype.deleteDatabase = function () {
      deleteCalls += 1
      throw new Error('An interrupted upgrade must never reset the database')
    }
    IDBFactory.prototype.open = function (name: string, version?: number) {
      const request = version === undefined ? originalOpen.call(this, name) : originalOpen.call(this, name, version)
      if (name === databaseName && version === 3) {
        // Let the WAL register its migration callback first, then append an
        // observer before the browser dispatches the native upgrade event.
        queueMicrotask(() => {
          request.addEventListener('upgradeneeded', () => {
            // Abort after production created its indexes. This is a native
            // transaction interruption, not an operating-system crash.
            attemptedIndexes = [...request.transaction!.objectStore('response_handoffs').indexNames]
            interrupted = true
            request.transaction!.abort()
          }, { once: true })
        })
      }
      return request
    }
    async function snapshot(version: number) {
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = originalOpen.call(indexedDB, databaseName, version)
        request.onerror = () => reject(request.error)
        request.onsuccess = () => resolve(request.result)
      })
      try {
        const transaction = database.transaction(['pending_chat_inputs', 'response_handoffs'])
        const drafts = transaction.objectStore('pending_chat_inputs').getAll()
        const handoffStore = transaction.objectStore('response_handoffs')
        const handoffs = handoffStore.getAll()
        const indexes = [...handoffStore.indexNames]
        await new Promise<void>((resolve, reject) => {
          transaction.oncomplete = () => resolve()
          transaction.onabort = () => reject(transaction.error)
        })
        return { version: database.version, drafts: drafts.result, handoffs: handoffs.result, indexes }
      } finally { database.close() }
    }
    try {
      let failure = ''
      try { await fixture.wal.countQuarantinedDeliveries() } catch (error) { failure = (error as DOMException).name }
      const afterAbort = await snapshot(2)
      IDBFactory.prototype.open = originalOpen
      const quarantined = await fixture.wal.countQuarantinedDeliveries()
      const afterRetry = await snapshot(3)
      return { interrupted, attemptedIndexes, failure, afterAbort, afterRetry, quarantined, deleteCalls, pending, handoff }
    } finally {
      IDBFactory.prototype.open = originalOpen
      IDBFactory.prototype.deleteDatabase = originalDelete
      fixture.wal.close()
    }
  }, DATABASE)
  expect(result.interrupted).toBe(true)
  expect(result.attemptedIndexes).toContain('recovery_state')
  expect(result.failure).toBe('AbortError')
  expect(result.afterAbort).toMatchObject({ version: 2, drafts: [result.pending], handoffs: [result.handoff], indexes: [] })
  expect(result.afterRetry).toMatchObject({ version: 3, drafts: [result.pending], handoffs: [result.handoff] })
  expect(result.afterRetry.indexes).toContain('recovery_state')
  expect(result.quarantined).toBe(1)
  expect(result.deleteCalls).toBe(0)
})

test('native IDB quota fault injection preserves a first-send draft and keeps unknown Stop exact and visible', async ({ context }) => {
  const page = await fixturePage(context)
  await installOwner(page)
  try {
    const first = await page.evaluate(async record => {
      const fixture = (window as any).deliveryFixture
      const draft = { schemaVersion: 1, pendingInputId: 'synthetic-quota-draft', sessionKey: 'synthetic-session',
        clientRequestId: 'synthetic-first-send', clientMessageId: 'synthetic-first-message', text: 'Synthetic retained input',
        attachments: [], intent: null, state: 'local_only', draftIds: ['synthetic-annotation'], createdAt: 1, updatedAt: 1 }
      await fixture.wal.put(draft)
      await fixture.wal.prepareDelivery(record)
      fixture.beforeQuota = await fixture.wal.getDelivery(record.ownerRequestId)
      fixture.savedDraft = (await fixture.wal.list(draft.sessionKey))[0]
      const originalPut = IDBObjectStore.prototype.put
      fixture.quotaFaults = 0
      // Inject the native API's failure at the real WAL write boundary. Do not
      // fill disk or claim that this exhausts the browser's physical quota.
      IDBObjectStore.prototype.put = function (value: unknown, key?: IDBValidKey) {
        if (this.name === 'response_handoffs') {
          fixture.quotaFaults += 1
          throw new DOMException('Synthetic quota fault injection', 'QuotaExceededError')
        }
        return key === undefined ? originalPut.call(this, value) : originalPut.call(this, value, key)
      }
      fixture.restoreQuotaPatch = () => { IDBObjectStore.prototype.put = originalPut }
      const request = { kind: 'new-turn', params: { sessionKey: draft.sessionKey, clientRequestId: draft.clientRequestId,
        clientMessageId: draft.clientMessageId, message: draft.text } }
      const frozen = structuredClone(request)
      let failure: { accepted: unknown; failureCode: unknown; message: string } | undefined
      try { await fixture.owner.commands.send(request) } catch (error) {
        const rejected = error as { accepted?: unknown; failureCode?: unknown; message: string }
        failure = { accepted: rejected.accepted, failureCode: rejected.failureCode, message: rejected.message }
      }
      await fixture.owner.requestStop(record.ownerRequestId)
      await fixture.owner.wake()
      return { request, frozen, failure, firstRecord: await fixture.wal.getDelivery(draft.clientRequestId) }
    }, deliveryRecord())
    expect(first.failure).toMatchObject({ accepted: false, failureCode: 'DELIVERY_STORAGE_UNAVAILABLE' })
    expect(first.failure?.message).toContain('Synthetic quota fault injection')
    expect(first.request).toEqual(first.frozen)
    expect(first.firstRecord).toBeNull()
    await expect.poll(() => page.evaluate(() => (window as any).deliveryFixture.calls.cancels.length)).toBe(1)
    const failedStorage = await page.evaluate(async () => {
      const fixture = (window as any).deliveryFixture
      return { calls: fixture.calls, snapshots: fixture.owner.snapshots(), faults: fixture.quotaFaults,
        before: fixture.beforeQuota, after: await fixture.wal.getDelivery('synthetic-delivery'),
        draft: (await fixture.wal.list('synthetic-session'))[0], savedDraft: fixture.savedDraft }
    })
    expect(failedStorage.faults).toBeGreaterThan(0)
    expect(failedStorage.after).toEqual(failedStorage.before)
    expect(failedStorage.draft).toEqual(failedStorage.savedDraft)
    expect(failedStorage.snapshots).toContainEqual(expect.objectContaining({ id: 'synthetic-delivery', waitReason: 'storage', stopPending: false }))
    expect(failedStorage.calls).toEqual({ sends: 0, lookups: 1, cancels: [{
      sessionKey: 'synthetic-handoff-target', taskId: 'synthetic-recovered-task', source: 'webui_stop', scope: 'task',
    }] })
    await page.evaluate(async () => {
      const fixture = (window as any).deliveryFixture
      fixture.restoreQuotaPatch()
      await fixture.owner.wake()
    })
    await expect.poll(() => page.evaluate(async () => (await (window as any).deliveryFixture.wal.getDelivery('synthetic-delivery')).stop?.completed)).toBe(true)
  } finally {
    await page.evaluate(() => {
      const fixture = (window as any).deliveryFixture
      fixture.restoreQuotaPatch?.()
      fixture.owner.dispose()
    })
  }
})
