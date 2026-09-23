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
        import { useChatPendingQueue } from ${source('src/composables/chat/useChatPendingQueue.ts')};
        import { PARKED_PENDING_QUEUE_LIMITS } from ${source('src/utils/chat/parkedPendingQueueCache.ts')};
        import * as recoveryPagination from ${source('e2e/support/recovery-pagination.ts')};
        import { effectScope, nextTick, ref } from 'vue';
        function createOfflinePendingQueue(initialSessionKey) {
          const scope = effectScope();
          const sessionKey = ref(initialSessionKey);
          const wal = createPendingInputWal();
          const errors = [];
          const queue = scope.run(() => useChatPendingQueue({
            sessionKey, inputText: ref(''), pendingAttachments: ref([]),
            pendingSessionIntent: ref(null), isStreaming: ref(false),
            connectionState: ref('disconnected'), deliveryIdentity: ref('synthetic-identity'),
            isBlocked: () => true, hasComposer: () => true,
            autoResizeTextarea() {}, resetInputHistory() {},
            sendCurrentInput() { throw new Error('Offline pressure fixture must not dispatch'); },
            onPendingPersistenceError: reason => errors.push(reason),
            pendingInputWal: wal, pendingInputQueue: null,
          }));
          return { queue, wal, errors, sessionKey,
            async switchTo(key) {
              await queue.switchPendingQueue(key);
              sessionKey.value = key;
              await nextTick();
              await queue.hydratePendingQueue(key);
              await nextTick();
            },
            cleanup() { queue.cleanup(); scope.stop(); },
          };
        }
        window.deliveryFixture = { createPendingInputWal, createDurableDelivery, TurnCommandError,
          createOfflinePendingQueue, PARKED_PENDING_QUEUE_LIMITS, recoveryPagination, nextTick };
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

for (const count of [1_000, 10_000]) {
  test(`native recovery pagination visits ${count} persistent pending rows linearly after reopening`, async ({ context }) => {
    const page = await fixturePage(context)
    const result = await page.evaluate(async count => {
      const fixture = (window as any).deliveryFixture
      const ids = Array.from({ length: count }, (_, index) => `pending-${String(index).padStart(6, '0')}`)
      await fixture.recoveryPagination.seedRecoveryRows(fixture.wal, ids)
      fixture.wal.close()
      fixture.wal = fixture.createPendingInputWal()
      return { expected: ids, measured: await fixture.recoveryPagination.measureRecoveryTraversal(fixture.wal) }
    }, count)
    expect(result.measured.ids).toEqual(result.expected)
    expect(new Set(result.measured.ids).size).toBe(count)
    expect(result.measured.pageSizes).toHaveLength(Math.ceil(count / 16))
    expect(result.measured.pageSizes.every((size: number) => size > 0 && size <= 16)).toBe(true)
    expect(result.measured.cursorCallbacks).toBeLessThanOrEqual(count + 3 * Math.ceil(count / 16))
    expect(result.measured.seekCalls).toBeGreaterThan(0)
  })
}

test('settled history does not amplify a fixed pending recovery page', async ({ context }) => {
  const page = await fixturePage(context)
  const result = await page.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    const ids = Array.from({ length: 16 }, (_, index) => `pending-${index}`)
      .sort((left, right) => indexedDB.cmp(left, right))
    await fixture.recoveryPagination.seedRecoveryRows(fixture.wal, ids)
    const before = await fixture.recoveryPagination.measureRecoveryTraversal(fixture.wal)
    const settled = Array.from({ length: 10_000 }, (_, index) => `${index % 2 ? 'a' : 'z'}-settled-${index}`)
    await fixture.recoveryPagination.seedRecoveryRows(fixture.wal, ids, settled)
    const after = await fixture.recoveryPagination.measureRecoveryTraversal(fixture.wal)
    return { ids, before, after }
  })
  expect(result.before.ids).toEqual(result.ids)
  expect(result.after.ids).toEqual(result.ids)
  expect(result.after.cursorCallbacks).toBe(result.before.cursorCallbacks)
})

test('native recovery seek handles absent, removed and settled page boundaries', async ({ context }) => {
  const page = await fixturePage(context)
  const results = await page.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    const wal = fixture.wal
    const ids = ['a', 'c', 'e', 'g', 'i']
    const outcomes = []
    for (const mutation of ['delete', 'settle']) {
      await fixture.recoveryPagination.seedRecoveryRows(wal, ids)
      const first = await wal.listRecoveryDeliveries(undefined, 2)
      const boundary = await wal.getDelivery(first.next)
      await wal.compareAndSwapDelivery(boundary.ownerRequestId, boundary.revision,
        mutation === 'delete' ? null : { ...boundary, phase: 'not-sent', revision: boundary.revision + 1 })
      const second = await wal.listRecoveryDeliveries(first.next, 2)
      const third = await wal.listRecoveryDeliveries(second.next, 2)
      outcomes.push({ first: first.records.map((r: any) => r.ownerRequestId),
        second: second.records.map((r: any) => r.ownerRequestId),
        third: third.records.map((r: any) => r.ownerRequestId), next: third.next })
    }
    await fixture.recoveryPagination.seedRecoveryRows(wal, ids)
    const gaps = []
    for (const after of ['0', 'd', 'i', 'z']) {
      const page = await wal.listRecoveryDeliveries(after, 2)
      gaps.push(page.records.map((r: any) => r.ownerRequestId))
    }
    return { outcomes, gaps }
  })
  expect(results.outcomes).toEqual(Array(2).fill({ first: ['a', 'c'], second: ['e', 'g'], third: ['i'], next: undefined }))
  expect(results.gaps).toEqual([['a', 'c'], ['e', 'g'], [], []])
})

test('recovery pagination sees later inserts and revisits earlier inserts on the next sweep', async ({ context }) => {
  const page = await fixturePage(context)
  const result = await page.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    const wal = fixture.wal
    await fixture.recoveryPagination.seedRecoveryRows(wal, ['a', 'c', 'e', 'g'])
    const first = await wal.listRecoveryDeliveries(undefined, 2)
    for (const id of ['b', 'd']) await wal.prepareDelivery(fixture.recoveryPagination.recoveryRecord(id))
    const tail = await wal.listRecoveryDeliveries(first.next, 16)
    const nextSweep = await fixture.recoveryPagination.measureRecoveryTraversal(wal)
    return { first: first.records.map((r: any) => r.ownerRequestId), tail: tail.records.map((r: any) => r.ownerRequestId), nextSweep }
  })
  expect(result.first).toEqual(['a', 'c'])
  expect(result.tail).toEqual(['d', 'e', 'g'])
  expect(result.nextSweep.ids).toEqual(['a', 'b', 'c', 'd', 'e', 'g'])
})

test('native recovery pagination preserves empty pages, limit clamps and IndexedDB key order', async ({ context }) => {
  const page = await fixturePage(context)
  const results = await page.evaluate(async () => {
    const fixture = (window as any).deliveryFixture
    const outcomes = []
    for (const count of [0, 1, 15, 16, 17, 80]) {
      const ids = Array.from({ length: count }, (_, index) => `${['a', 'é', '中', '😀'][index % 4]}-${index}`)
        .sort((left, right) => indexedDB.cmp(left, right))
      await fixture.recoveryPagination.seedRecoveryRows(fixture.wal, ids)
      for (const limit of [-1, 0, 1, 15, 16, 17, 64, 65]) {
        const measured = await fixture.recoveryPagination.measureRecoveryTraversal(fixture.wal, limit)
        outcomes.push({ count, limit, expected: ids, measured })
      }
    }
    return outcomes
  })
  for (const result of results) {
    expect(result.measured.ids).toEqual(result.expected)
    expect(result.measured.pageSizes.every((size: number) => size <= Math.max(1, Math.min(result.limit, 64)))).toBe(true)
    expect(result.measured.pageSizes).toHaveLength(Math.max(1, Math.ceil(result.count / Math.max(1, Math.min(result.limit, 64)))))
  }
})

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

test('500 real pending queues obey retention budgets and recover evicted attachment bytes after reopening', async ({ context }) => {
  // This exercises retained domain accounting and native persisted bytes, not
  // process RSS, forced garbage collection, or a full application mount loop.
  test.setTimeout(60_000)
  const page = await fixturePage(context)
  const pressure = await page.evaluate(async databaseName => {
    const fixture = (window as any).deliveryFixture
    const MiB = 1024 * 1024
    const session = (index: number) => `agent:main:webchat:synthetic-pressure-${index}`
    const harness = fixture.createOfflinePendingQueue(session(0))
    const originalRevoke = URL.revokeObjectURL
    const revoked = new Set<string>()
    URL.revokeObjectURL = function (url: string) { revoked.add(url); originalRevoke.call(URL, url) }
    const selected: Array<{
      sessionKey: string; pendingInputId: string; clientRequestId: string; clientMessageId: string;
      ownerRequestId?: string; draftIds: string[]; textLength: number; textHash: string;
      attachment?: { name: string; size: number; sha256: string; originalUrl: string };
    }> = []
    const samples: Record<string, unknown> = {}
    async function hash(bytes: ArrayBuffer | Uint8Array<ArrayBuffer>): Promise<string> {
      return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
        .map(value => value.toString(16).padStart(2, '0')).join('')
    }
    try {
      await harness.queue.hydratePendingQueue(session(0))
      for (let index = 0; index < 500; index += 1) {
        const text = index === 2 ? 'x'.repeat(9 * MiB) : `Synthetic retained draft ${index}`
        const size = index < 2 ? 17 * MiB : index === 3 ? 2 * MiB : 0
        const bytes = size ? new Uint8Array(size).fill(index + 1) : undefined
        const name = `synthetic-attachment-${index}.bin`
        const file = bytes ? new File([bytes], name, { type: 'application/octet-stream', lastModified: 1 }) : undefined
        const url = file ? URL.createObjectURL(file) : ''
        const attachments = file ? [{ kind: 'staged', local_id: index + 1, name, mime: file.type,
          size: file.size, file, dataUrl: url }] : []
        const owner = index === 3 ? { ownerRequestId: 'synthetic-protected-handoff' } : undefined
        const saved = await harness.queue.enqueuePendingPayload({ text, attachments,
          deliveryIdentity: 'synthetic-identity', draftIds: [`synthetic-annotation-${index}`] }, owner)
        if (!saved || harness.errors.length) throw new Error(`Synthetic queue failed to persist at ${index}`)
        const item = harness.queue.pendingQueue.value.find((value: { ownerSessionKey: string }) => value.ownerSessionKey === session(index))
        if (!item) throw new Error(`Synthetic queue lost its active draft at ${index}`)
        if (index < 4) selected.push({
          sessionKey: session(index), pendingInputId: item.pendingInputId,
          clientRequestId: item.pendingClientRequestId, clientMessageId: item.pendingClientMessageId,
          ...(item.ownerRequestId ? { ownerRequestId: item.ownerRequestId } : {}),
          draftIds: [...item.draftIds], textLength: text.length, textHash: await hash(new TextEncoder().encode(text)),
          ...(bytes ? { attachment: { name, size, sha256: await hash(bytes), originalUrl: url } } : {}),
        })
        await harness.switchTo(session(index + 1))
        const usage = harness.queue.getParkedQueueUsage()
        if (usage.sessions > 16 || usage.payloadBytes > 16 * MiB || usage.blobBytes > 32 * MiB) {
          throw new Error(`Synthetic parked queue exceeded its retention budget at ${index}`)
        }
        if (index === 0) samples.firstBlob = usage
        if (index === 1) samples.blobPressure = { ...usage, earlyBlobRevoked: revoked.has(selected[0]!.attachment!.originalUrl) }
        if (index === 2) samples.payloadPressure = usage
      }
      const usage = harness.queue.getParkedQueueUsage()
      const persistedCount = await new Promise<number>((resolve, reject) => {
        const opening = indexedDB.open(databaseName, 3)
        opening.onerror = () => reject(opening.error)
        opening.onsuccess = () => {
          const database = opening.result
          const transaction = database.transaction('pending_chat_inputs')
          const count = transaction.objectStore('pending_chat_inputs').count()
          transaction.oncomplete = () => { database.close(); resolve(count.result) }
          transaction.onabort = () => { database.close(); reject(transaction.error) }
        }
      })
      // Only identity/digest metadata crosses the page boundary. No test-side
      // array retains the evicted File objects or the oversized draft text.
      harness.cleanup()
      return { selected, samples, usage, persistedCount, limits: fixture.PARKED_PENDING_QUEUE_LIMITS,
        afterCleanup: harness.queue.getParkedQueueUsage(), activeItems: harness.queue.pendingQueue.value.length,
        errors: harness.errors, revoked: [...revoked] }
    } finally {
      harness.cleanup()
      URL.revokeObjectURL = originalRevoke
    }
  }, DATABASE)
  const MiB = 1024 * 1024
  expect(pressure.limits).toEqual({ sessions: 16, payloadBytes: 16 * MiB, blobBytes: 32 * MiB })
  expect(pressure.samples.firstBlob).toMatchObject({ sessions: 1, blobBytes: 17 * MiB, protectedSessions: 0 })
  expect(pressure.samples.blobPressure).toMatchObject({ sessions: 1, blobBytes: 17 * MiB, earlyBlobRevoked: true })
  expect(pressure.samples.payloadPressure).toMatchObject({ sessions: 0, payloadBytes: 0, blobBytes: 0 })
  expect(pressure.usage).toMatchObject({ sessions: 16, protectedSessions: 1, protectedBlobBytes: 2 * MiB,
    reclaimableSessions: 15, reclaimableBlobBytes: 0, blobBytes: 2 * MiB })
  expect(pressure.usage.protectedPayloadBytes).toBeGreaterThan(0)
  expect(pressure.usage.payloadBytes).toBe(pressure.usage.protectedPayloadBytes + pressure.usage.reclaimablePayloadBytes)
  expect(pressure.persistedCount).toBe(500)
  expect(pressure.errors).toEqual([])
  expect(pressure.activeItems).toBe(0)
  expect(pressure.afterCleanup).toMatchObject({ sessions: 0, payloadBytes: 0, blobBytes: 0 })
  await page.close()

  const reopened = await fixturePage(context)
  try {
    const restored = await reopened.evaluate(async selected => {
      const fixture = (window as any).deliveryFixture
      const harness = fixture.createOfflinePendingQueue(selected[0]!.sessionKey)
      async function hash(bytes: ArrayBuffer | Uint8Array<ArrayBuffer>): Promise<string> {
        return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
          .map(value => value.toString(16).padStart(2, '0')).join('')
      }
      const recovered = []
      try {
        for (const expected of selected) {
          await harness.switchTo(expected.sessionKey)
          const item = harness.queue.pendingQueue.value.find((value: { pendingInputId: string }) => value.pendingInputId === expected.pendingInputId)
          if (!item) throw new Error('Native WAL did not restore the original synthetic input identity')
          const attachment = item.attachments[0]
          let attachmentResult
          if (expected.attachment) {
            if (!(attachment?.file instanceof Blob)) throw new Error('Native WAL did not retain the synthetic attachment bytes')
            if (!attachment.dataUrl?.startsWith('blob:')) throw new Error('Restored attachment has no display URL')
            const displayBytes = await (await fetch(attachment.dataUrl)).arrayBuffer()
            attachmentResult = { name: attachment.name, size: attachment.file.size,
              sha256: await hash(await attachment.file.arrayBuffer()), displayHash: await hash(displayBytes),
              newDisplayUrl: attachment.dataUrl !== expected.attachment.originalUrl }
          }
          recovered.push({ pendingInputId: item.pendingInputId, clientRequestId: item.pendingClientRequestId,
            clientMessageId: item.pendingClientMessageId, ownerRequestId: item.ownerRequestId,
            deliveryIdentity: item.pendingDeliveryIdentity, draftIds: [...item.draftIds],
            textLength: item.text.length, textHash: await hash(new TextEncoder().encode(item.text)), attachment: attachmentResult })
        }
        return { recovered, errors: harness.errors }
      } finally { harness.cleanup() }
    }, pressure.selected)
    expect(restored.errors).toEqual([])
    expect(restored.recovered).toHaveLength(4)
    for (const [index, expected] of pressure.selected.entries()) {
      expect(restored.recovered[index]).toMatchObject({ pendingInputId: expected.pendingInputId,
        clientRequestId: expected.clientRequestId, clientMessageId: expected.clientMessageId,
        deliveryIdentity: 'synthetic-identity', draftIds: expected.draftIds, textLength: expected.textLength, textHash: expected.textHash })
      expect(restored.recovered[index]?.ownerRequestId).toBe(expected.ownerRequestId)
      if (expected.attachment) expect(restored.recovered[index]?.attachment).toEqual({ name: expected.attachment.name,
        size: expected.attachment.size, sha256: expected.attachment.sha256, displayHash: expected.attachment.sha256, newDisplayUrl: true })
    }
  } finally { await reopened.close() }
})


test('follower notices acceptance committed by another window', async ({ context }) => {
  const first = await fixturePage(context)
  const second = await fixturePage(context)
  await second.clock.install()
  await first.evaluate(async record => (window as any).deliveryFixture.wal.prepareDelivery(record), deliveryRecord())
  await installOwner(first, 'synthetic-identity', true)
  await installOwner(second)
  await first.evaluate(() => { void (window as any).deliveryFixture.owner.wake() })
  await first.waitForFunction(() => (window as any).deliveryFixture.calls.lookups === 1)
  await second.evaluate(() => (window as any).deliveryFixture.owner.wake())
  const before = await second.evaluate(() => (window as any).deliveryFixture.owner.snapshots()[0])
  expect(before).toMatchObject({ phase: 'unknown', waitReason: 'lease' })
  await first.evaluate(async () => {
    const f = (window as any).deliveryFixture
    f.resolveLookup()
    await f.owner.wake()
  })
  // The existing lease wake must observe completion even though the record
  // has left the pending index; no focus event or manual retry is needed.
  await second.clock.fastForward(60_100)
  await second.waitForFunction(() => (window as any).deliveryFixture.owner.snapshots()[0]?.phase === 'accepted')
  const result = await second.evaluate(async () => {
    const f = (window as any).deliveryFixture
    return { disk: (await f.wal.getDelivery('synthetic-delivery')).phase, ui: f.owner.snapshots()[0], calls: f.calls }
  })
  expect(result.disk).toBe('accepted')
  expect(result.ui.phase).toBe('accepted')
  expect(result.ui.waitReason).toBeUndefined()
  expect(result.calls).toEqual({ sends: 0, lookups: 0, cancels: [] })
})

test('finished Stop clears the stale offline notification', async ({ context }) => {
  const page = await fixturePage(context)
  const result = await page.evaluate(async record => {
    const f = (window as any).deliveryFixture
    let available = false
    await f.wal.prepareDelivery({...record, phase: 'accepted', response: { taskId: 'synthetic-task', sessionKey: 'synthetic-session' }, stop: {requested: true}})
    const owner = f.createDurableDelivery({ wal: f.wal,
      access: {identity: () => 'synthetic-identity', available: () => available, generation: () => 1},
      commands: {send: async () => { throw new Error('No send permitted') }, steer: async () => {throw new Error('No steer permitted')},
        supports: () => true, cancel: async () => ({aborted: true})} })
    await owner.wake()
    const before = owner.snapshots()[0]
    available = true
    await owner.wake()
    const stored = await f.wal.getDelivery(record.ownerRequestId)
    const after = owner.snapshots()[0]
    owner.dispose()
    return {before, completed: stored.stop.completed, after}
  }, deliveryRecord())
  expect(result.before.waitReason).toBe('offline')
  expect(result.completed).toBe(true)
  expect(result.after.stopPending).toBe(false)
  expect(result.after.waitReason).toBeUndefined()
})
