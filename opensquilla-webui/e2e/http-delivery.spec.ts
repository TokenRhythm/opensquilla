import { expect, test, type BrowserContext, type Page } from '@playwright/test'
import { build, normalizePath } from 'vite'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { helloOkResponse } from './support/gateway-fixture'

// A non-loopback HTTP origin intentionally exposes neither randomUUID,
// SubtleCrypto nor Web Locks. Use the real store/RPC client/access/WAL/owner;
// only the Gateway Hello and command results are synthetic.
const ORIGIN = 'http://nas-delivery.test'
let fixtureModule: string

type Fixture = {
  identity(): string | null
  send(id: string, loseAck?: boolean): Promise<void>
  steer(): Promise<void>
  stop(id: string): Promise<void>
  recover(): Promise<void>
  state(): { sends: number; steers: number; cancels: number; receipts: number; cancelledTask: string | null }
  phase(id: string): Promise<string | undefined>
}
declare global { interface Window { httpDeliveryFixture: Fixture } }

test.beforeAll(async () => {
  const root = fileURLToPath(new URL('..', import.meta.url))
  const source = (name: string) => JSON.stringify(normalizePath(resolve(root, 'src', name)))
  const entry = '\0http-delivery-fixture'
  const result = await build({
    root, configFile: false, logLevel: 'silent',
    resolve: { alias: { '@': resolve(root, 'src') } },
    define: { __VUE_I18N_FULL_INSTALL__: 'true', __VUE_I18N_LEGACY_API__: 'false', __INTLIFY_PROD_DEVTOOLS__: 'false' },
    plugins: [{ name: 'http-delivery-fixture', resolveId: id => id === entry ? entry : undefined,
      load: id => id === entry ? `
        import { createPinia, setActivePinia } from 'pinia';
        import { useRpcStore } from ${source('stores/rpc.ts')};
        import { createV4GatewayAccess } from ${source('adapters/gateway/gatewayAccessV4.ts')};
        import { createPendingInputWal } from ${source('utils/chat/pendingInputWal.ts')};
        import { createDurableDelivery } from ${source('runtime/durableDelivery.ts')};
        import { TurnCommandError } from ${source('modules/turnCommands.ts')};
        setActivePinia(createPinia());
        const store = useRpcStore(), access = createV4GatewayAccess(store), wal = createPendingInputWal();
        let sends = 0, steers = 0, cancels = 0, receipts = 0, cancelledTask = null, loseAck = false, offline = false;
        const response = id => ({ taskId: 'task-' + id, sessionKey: 'session-http' });
        const checkGeneration = options => {
          if (!(access.subscriptionEpoch > 0) || options?.expectedGeneration !== access.subscriptionEpoch)
            throw new Error('Command lost its connection generation fence');
        };
        const owner = createDurableDelivery({ wal, access: {
          identity: () => access.deliveryIdentity, available: () => access.isAvailable && !offline,
          generation: () => access.subscriptionEpoch,
        }, commands: {
          supports: () => true, supportsReceiptLookup: () => true,
          send: async (request, options) => { checkGeneration(options); sends++; if (loseAck) { offline = true; throw new TurnCommandError('transport', 'lost acknowledgment'); }
            return response(request.params.clientRequestId); },
          steer: async (_request, options) => { checkGeneration(options); steers++; return { accepted: true, disposition: 'applied', taskId: 'task-http' }; },
          cancel: async (request, options) => { checkGeneration(options); cancels++; cancelledTask = request.taskId; return { aborted: true }; },
          lookupReceipt: async (request, options) => { checkGeneration(options); receipts++; return { status: 'found', response: response(request.request.params.clientRequestId) }; },
        }});
        window.httpDeliveryFixture = {
          identity: () => access.deliveryIdentity,
          async send(id, lost = false) { loseAck = lost; await owner.commands.send({ kind: 'new-turn', params: {
            sessionKey: 'session-http', message: 'HTTP regression', clientRequestId: id, clientMessageId: 'msg-' + id,
          }}); },
          async steer() { await owner.commands.steer({ key: 'session-http', message: 'continue',
            expectedTurnId: 'task-http', clientRequestId: 'steer-http', clientMessageId: 'msg-steer-http' }); },
          async stop(id) { await owner.commands.cancel({ sessionKey: 'session-http', taskId: 'task-' + id, scope: 'task' }); },
          recover: () => owner.wake(),
          state: () => ({ sends, steers, cancels, receipts, cancelledTask }),
          phase: async id => (await wal.getDelivery(id))?.phase,
        };
        store.init();
      ` : undefined }],
    build: { write: false, minify: false, rollupOptions: { input: entry, output: { format: 'es', inlineDynamicImports: true } } },
  })
  const chunks = (Array.isArray(result) ? result : [result]).flatMap(output =>
    'output' in output ? output.output.filter(asset => asset.type === 'chunk') : [])
  expect(chunks).toHaveLength(1)
  fixtureModule = chunks[0]!.code
})

test.beforeEach(async ({ context }) => {
  await context.route(`${ORIGIN}/**`, route => route.fulfill({
    contentType: route.request().url().endsWith('.mjs') ? 'text/javascript' : 'text/html',
    body: route.request().url().endsWith('.mjs') ? fixtureModule
      : '<!doctype html><title>HTTP delivery regression</title><script type="module" src="/fixture.mjs"></script>',
  }))
  await context.routeWebSocket('ws://nas-delivery.test/ws', socket => {
    socket.send(JSON.stringify({ type: 'event', event: 'connect.challenge', payload: {} }))
    socket.onMessage(raw => {
      const frame = JSON.parse(String(raw))
      if (frame.method === 'connect') socket.send(helloOkResponse())
      else if (frame.type === 'ping') socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce }))
    })
  })
})

async function open(context: BrowserContext): Promise<Page> {
  const page = await context.newPage()
  await page.goto(ORIGIN)
  await expect.poll(() => page.evaluate(() => window.httpDeliveryFixture?.identity())).toBeTruthy()
  return page
}

test('fresh HTTP origin can initialize, send, steer and stop without secure-context APIs', async ({ context }) => {
  const page = await open(context)
  expect(await page.evaluate(() => ({ secure: isSecureContext, uuid: typeof crypto.randomUUID,
    subtle: typeof crypto.subtle, locks: typeof navigator.locks }))).toEqual({
    secure: false, uuid: 'undefined', subtle: 'undefined', locks: 'undefined',
  })
  await page.evaluate(async () => {
    await window.httpDeliveryFixture.send('http')
    await window.httpDeliveryFixture.steer()
    await window.httpDeliveryFixture.stop('http')
  })
  expect(await page.evaluate(() => window.httpDeliveryFixture.state())).toMatchObject({
    sends: 1, steers: 1, cancels: 1, cancelledTask: 'task-http',
  })
})

test('imports an existing salt without changing identity across reload', async ({ context }) => {
  const salt = '1234567890abcdef1234567890abcdef'
  await context.addInitScript(value => localStorage.setItem('opensquilla.deliverySalt.v1', value), salt)
  const page = await open(context)
  const identity = await page.evaluate(() => window.httpDeliveryFixture.identity())
  const target = JSON.stringify(['browser', 'ws://nas-delivery.test/ws', ''])
  expect(JSON.parse(identity!)[1]).toBe(createHash('sha256').update(JSON.stringify([salt, target])).digest('hex'))
  await page.reload()
  await expect.poll(() => page.evaluate(() => window.httpDeliveryFixture?.identity())).toBe(identity)
  await page.evaluate(() => window.httpDeliveryFixture.send('legacy-http'))
})

test('concurrent first tabs share identity and recover a lost acknowledgment without resending', async ({ context }) => {
  const [first, second] = await Promise.all([open(context), open(context)])
  const identity = await first.evaluate(() => window.httpDeliveryFixture.identity())
  expect(await second.evaluate(() => window.httpDeliveryFixture.identity())).toBe(identity)
  expect(await first.evaluate(async () => {
    try { await window.httpDeliveryFixture.send('lost-http', true); return 'unexpected acceptance' }
    catch (error) { return (error as Error).message }
  })).toBe('lost acknowledgment')
  expect(await first.evaluate(() => window.httpDeliveryFixture.phase('lost-http'))).toBe('unknown')
  await first.close()
  await second.evaluate(() => window.httpDeliveryFixture.recover())
  expect(await second.evaluate(() => window.httpDeliveryFixture.phase('lost-http'))).toBe('accepted')
  await second.evaluate(() => window.httpDeliveryFixture.stop('lost-http'))
  expect(await second.evaluate(() => window.httpDeliveryFixture.state())).toMatchObject({
    sends: 0, receipts: 1, cancels: 1, cancelledTask: 'task-lost-http',
  })
})
