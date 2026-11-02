// Local WebUI account development host. Keep the existing account and model
// proxy implementation in one process; the Gateway only receives a short-lived
// bridge capability, never a website token or managed model key.
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { AsyncLocalStorage } from 'node:async_hooks'
import { parse as parseToml } from 'smol-toml'
import { TokenRhythmAccount, hasSavedModelConnection } from '../dist/tokenrhythm-account.js'
import { LocalAccountSecrets, LOCAL_ACCOUNT_FILE } from '../dist/local-account-secrets.js'
import { TokenRhythmModelProxy } from '../dist/tokenrhythm-model-proxy.js'

const home = process.env.OPENSQUILLA_HOME
if (!home) throw new Error('Set OPENSQUILLA_HOME to an isolated local preview profile.')
const root = resolve(import.meta.dirname, '../../..')
const profile = resolve(process.env.OPENSQUILLA_WEB_ACCOUNT_PROFILE || join(home, 'default'))
const origin = process.env.OPENSQUILLA_TOKENRHYTHM_ORIGIN || 'https://tokenrhythm.studio'
const uiOrigin = process.env.OPENSQUILLA_WEB_ACCOUNT_UI_ORIGIN || 'http://127.0.0.1:5173'
const accountPort = Number(process.env.OPENSQUILLA_WEB_ACCOUNT_PORT || 18892)
const gatewayPort = Number(process.env.OPENSQUILLA_WEB_ACCOUNT_GATEWAY_PORT || 18891)
const ui = new URL(uiOrigin)
if (ui.protocol !== 'http:' || ui.hostname !== '127.0.0.1' || !ui.port || ui.pathname !== '/' || ui.search || ui.hash || ui.username || ui.password
  || ![accountPort, gatewayPort].every(port => Number.isInteger(port) && port > 0 && port < 65536)
  || accountPort === gatewayPort) throw new Error('Local account preview requires distinct loopback ports and a loopback UI origin.')
const directory = join(profile, 'web-account', createHash('sha256').update(origin).digest('hex').slice(0, 16))
const secrets = new LocalAccountSecrets(directory)
const navigation = new AsyncLocalStorage()
const account = new TokenRhythmAccount({
  directory,
  storageFile: LOCAL_ACCOUNT_FILE,
  origin,
  assertSecureStorage: () => secrets.assertAvailable(),
  encrypt: value => secrets.encrypt(value),
  decrypt: value => secrets.decrypt(value),
  recoverStorage: value => secrets.recover(value),
  openExternal: async url => { const request = navigation.getStore(); if (request) request.url = url },
  returnToWebUI: `${uiOrigin}/control/settings/account`,
  onOpenDesktop: async () => {},
  onSignedIn: async () => {
    const raw = await readFile(join(profile, 'config.toml'), 'utf8').catch(() => '')
    const config = raw ? parseToml(raw) : {}
    if (!hasSavedModelConnection(config) && !account.snapshot().selectedAccountId) await account.selectSource('account')
  },
})
const proxy = new TokenRhythmModelProxy(account)

function send(res, status, data) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' })
  res.end(JSON.stringify(data))
}

async function body(req) {
  let raw = ''
  for await (const chunk of req) {
    raw += chunk
    if (raw.length > 2048) throw new Error('REQUEST_TOO_LARGE')
  }
  return JSON.parse(raw || '{}')
}

const actions = new Set(['login', 'cancel', 'reopen', 'refresh', 'retry-key', 'logout', 'test'])
const links = new Set(['account', 'usage', 'recharge'])
const server = createServer((req, res) => { void (async () => {
  // The Vite development proxy preserves these browser headers. A direct
  // cross-origin page cannot satisfy this boundary or receive CORS approval.
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(req.socket.remoteAddress ?? '')
    || req.headers.origin && req.headers.origin !== uiOrigin
    || req.headers['sec-fetch-site'] && req.headers['sec-fetch-site'] !== 'same-origin'
    || req.method === 'POST' && !req.headers['content-type']?.startsWith('application/json')) {
    send(res, 403, { error: 'ACCOUNT_FORBIDDEN' }); return
  }
  await account.initialize()
  const path = new URL(req.url ?? '/', 'http://127.0.0.1').pathname
  if (req.method === 'GET' && path === '/api/local-account/state') {
    send(res, 200, { state: account.snapshot() }); return
  }
  if (req.method !== 'POST') { send(res, 404, { error: 'ACCOUNT_ROUTE_NOT_FOUND' }); return }
  const value = await body(req)
  const result = await navigation.run({ url: '' }, async () => {
    let state
    if (path === '/api/local-account/action' && actions.has(value.action)) {
      switch (value.action) {
        case 'login': state = await account.login(); break
        case 'cancel': state = account.cancel(); break
        case 'reopen': state = await account.reopen(); break
        case 'refresh': await account.verify(true); state = account.snapshot(); break
        case 'retry-key': state = await account.retryKey(); break
        case 'logout': state = await account.logout(); break
        case 'test': state = await account.testModel(); break
      }
    } else if (path === '/api/local-account/source' && ['manual', 'account'].includes(value.source)) {
      state = await account.selectSource(value.source)
    } else if (path === '/api/local-account/media-source' && ['image', 'video'].includes(value.kind) && ['inherit', 'manual', 'account'].includes(value.source)) {
      state = await account.selectMediaSource(value.kind, value.source)
    } else if (path === '/api/local-account/link' && links.has(value.link)) {
      await account.openLink(value.link)
      state = account.snapshot()
    } else return null
    return { state, url: navigation.getStore().url || null }
  })
  if (!result) { send(res, 400, { error: 'ACCOUNT_BAD_REQUEST' }); return }
  send(res, 200, result)
})().catch(error => send(res, 503, { error: typeof error?.code === 'string' ? error.code : 'ACCOUNT_UNAVAILABLE' })) })

await account.initialize()
const bridge = await proxy.start()
await new Promise((accept, reject) => { server.once('error', reject); server.listen(accountPort, '127.0.0.1', accept) })
const gateway = spawn(join(root, '.venv/bin/opensquilla'), ['gateway', 'run', '--port', String(gatewayPort), '--bind', '127.0.0.1'], {
  cwd: root,
  env: { ...process.env, ...bridge, OPENSQUILLA_DESKTOP: '1' },
  stdio: 'inherit',
})
console.log(`Local account preview ready at ${uiOrigin}/control/settings/account`)
const stop = async () => {
  gateway.kill('SIGTERM')
  server.closeAllConnections()
  await new Promise(accept => server.close(accept))
  await proxy.close()
  await account.close()
}
process.once('SIGINT', () => { void stop().finally(() => process.exit(0)) })
process.once('SIGTERM', () => { void stop().finally(() => process.exit(0)) })
gateway.once('exit', code => { void stop().finally(() => process.exit(code || 0)) })
