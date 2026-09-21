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
