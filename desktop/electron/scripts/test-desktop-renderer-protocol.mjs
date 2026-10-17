import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { join, resolve } from 'node:path'
import vm from 'node:vm'
import ts from '@typescript/typescript6'
import { desktopControlUiBasePath } from '../dist/desktop-control-ui-config.js'
import {
  DESKTOP_RENDERER_URL,
  isDesktopRendererDocumentUrl,
  isDesktopRendererUrl,
  resolveDesktopRendererFile,
  routeDesktopRendererRequest,
} from '../dist/desktop-renderer-protocol.js'

for (const [config, expected] of [
  [null, '/control'], ['', '/control'], ['[control_ui]\nenabled = true', '/control'],
  ['[control_ui]\nbase_path = "/"', ''],
  ['["control_ui"]\nbase_path = "/custom/ui/" # imported profile', '/custom/ui'],
  ['control_ui = { base_path = "/imported" }', '/imported'],
]) assert.equal(desktopControlUiBasePath(config), expected)
assert.equal(desktopControlUiBasePath(null, { OPENSQUILLA_CONTROL_UI_BASE_PATH: '/' }), '')
assert.equal(desktopControlUiBasePath(null, { opensquilla_control_ui_base_path: '/' }), '')
assert.equal(desktopControlUiBasePath(null, {
  opensquilla_gateway_control_ui: '{"base_path":"/lowercase-json"}',
}), '/lowercase-json')
assert.equal(desktopControlUiBasePath(null, {
  opensquilla_gateway_control_ui__base_path: '/lowercase-nested',
  opensquilla_control_ui_base_path: '/lowercase-control',
}), '/lowercase-nested')
assert.equal(desktopControlUiBasePath(null, {
  OPENSQUILLA_CONTROL_UI_BASE_PATH: '/canonical',
  opensquilla_control_ui_base_path: '/alternate',
}), '/canonical', 'canonical environment access retains Windows process.env semantics')
assert.equal(desktopControlUiBasePath(null, {
  OPENSQUILLA_CONTROL_UI_BASE_PATH: '/low-priority',
  OPENSQUILLA_GATEWAY_CONTROL_UI: '{"base_path":"/json"}',
}), '/json')
assert.equal(desktopControlUiBasePath(null, {
  OPENSQUILLA_GATEWAY_CONTROL_UI: '{"base_path":"/json"}',
  OPENSQUILLA_GATEWAY_CONTROL_UI__BASE_PATH: '/nested',
}), '/nested')
assert.equal(desktopControlUiBasePath('[control_ui]\nbase_path = "/"', {
  OPENSQUILLA_GATEWAY_CONTROL_UI__BASE_PATH: '/nested',
  OPENSQUILLA_CONTROL_UI_BASE_PATH: '/environment',
}), '', 'explicit imported profile retains Gateway configuration authority')
assert.throws(() => desktopControlUiBasePath('[control_ui]\nbase_path = 42'), /must be a string/)

assert.equal(isDesktopRendererUrl(DESKTOP_RENDERER_URL), true)
assert.equal(isDesktopRendererUrl('opensquilla-app://desktop.evil/chat/new'), false)
assert.equal(isDesktopRendererUrl('https://desktop/chat/new'), false)
assert.equal(isDesktopRendererDocumentUrl(DESKTOP_RENDERER_URL), true)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/settings/runtime'), true)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/agents'), false)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/changelog'), false)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/api/system/status'), false)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/assets/app.js'), false)

// Legacy diagnostic URLs remain trusted SPA documents long enough for the
// renderer router to redirect them to Usage or Settings support.
for (const path of ['/overview', '/health', '/logs']) {
  const url = `opensquilla-app://desktop${path}`
  assert.equal(isDesktopRendererDocumentUrl(url), true, path)
  assert.deepEqual(
    routeDesktopRendererRequest(url),
    { kind: 'spa', relativePath: 'desktop.html' },
    path,
  )
}

assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/api/v1/files?q=1', 'POST'),
  { kind: 'gateway', pathAndQuery: '/api/v1/files?q=1' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/assets/app-123.js'),
  { kind: 'file', relativePath: 'assets/app-123.js' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/static/img/QRcode.png'),
  { kind: 'gateway', pathAndQuery: '/static/img/QRcode.png' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/static/img/QRcode.png', 'POST'),
  { kind: 'reject' },
)
for (const basePath of ['', '/control', '/custom/ui']) {
  for (const method of ['GET', 'HEAD']) {
    for (const name of ['QRcode.png', 'opensquilla-mark.png', '%51Rcode.png']) {
      const path = `/static/img/${name}?v=1&v=2&cache=a%2Fb`
      assert.deepEqual(
        routeDesktopRendererRequest(`opensquilla-app://desktop${path}`, method, basePath),
        { kind: 'gateway', pathAndQuery: `${basePath}${path}` },
      )
    }
  }
  assert.deepEqual(
    routeDesktopRendererRequest('opensquilla-app://desktop/static/img/QRcode.png', 'POST', basePath),
    { kind: 'reject' },
  )
  // The mount prefix applies only to the existing image allowlist, never API
  // requests or newly invented Desktop resource paths.
  assert.deepEqual(
    routeDesktopRendererRequest('opensquilla-app://desktop/api/v1/files?q=1', 'POST', basePath),
    { kind: 'gateway', pathAndQuery: '/api/v1/files?q=1' },
  )
  for (const path of ['/control/static/img/QRcode.png', '/static/%69mg/QRcode.png']) {
    assert.deepEqual(
      routeDesktopRendererRequest(`opensquilla-app://desktop${path}`, 'GET', basePath),
      { kind: 'spa', relativePath: 'desktop.html' },
    )
  }
}
for (const basePath of ['https://evil.invalid', '//evil.invalid', '/x?query=', '/x#fragment',
  '/x\\escape', '/x/../api', '/x/./api', '/x/%2e%2e', '/x\u0000', '/x\n', '/x//path']) {
  assert.deepEqual(
    routeDesktopRendererRequest('opensquilla-app://desktop/static/img/QRcode.png', 'GET', basePath),
    { kind: 'reject' },
    `an invalid mount prefix cannot widen the static resource route: ${JSON.stringify(basePath)}`,
  )
}
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/chat/new'),
  { kind: 'spa', relativePath: 'desktop.html' },
)
// Removed background-music URLs use the ordinary unknown-route SPA fallback.
for (const path of ['/music/playlist.json', '/music/playlist.local.json', '/music/private.mp3']) {
  assert.deepEqual(
    routeDesktopRendererRequest(`opensquilla-app://desktop${path}`),
    { kind: 'spa', relativePath: 'desktop.html' },
  )
}
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/opensquilla-mark.png'),
  { kind: 'file', relativePath: 'opensquilla-mark.png' },
)
// Retired routes can show the SPA's Not Found page without gaining document trust.
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/agents'),
  { kind: 'spa', relativePath: 'desktop.html' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/assets/%00secret'),
  { kind: 'reject' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/assets/app.js', 'DELETE'),
  { kind: 'reject' },
)

const root = resolve('/tmp/opensquilla-control-ui-dist')
assert.equal(
  resolveDesktopRendererFile(root, 'assets/app.js'),
  join(root, 'assets', 'app.js'),
)
assert.equal(resolveDesktopRendererFile(root, '../secret'), null)

// Exercise the returned URL against an actual loopback HTTP server. Status
// alone is insufficient: an accidental SPA fallback can also return 200.
const images = new Map(await Promise.all(['QRcode.png', 'opensquilla-mark.png'].map(async name => [
  name, await readFile(new URL(`../../../src/opensquilla/gateway/static/img/${name}`, import.meta.url)),
])))
const mainSource = await readFile(new URL('../src/main.ts', import.meta.url), 'utf8')
const parsed = ts.createSourceFile('main.ts', mainSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const snapshot = parsed.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === 'desktopGatewayConnectionSnapshot')
assert.ok(snapshot, 'test the production IPC descriptor consumed by session link copying')
const compiledSnapshot = ts.transpileModule(`${snapshot.getText(parsed)}\nglobalThis.snapshot = desktopGatewayConnectionSnapshot`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
for (const [config, expectedBasePath] of [
  [null, '/control'],
  ['[control_ui]\nbase_path = "/"', ''],
  ['[control_ui]\nbase_path = "/custom/ui/"', '/custom/ui'],
]) {
  const gatewayState = { owned: true, status: 'ready', url: 'http://127.0.0.1:8765' }
  const context = vm.createContext({
    gatewayState,
    ownedGatewayControlUiBasePath: desktopControlUiBasePath(config),
    gatewayConnectionRevision: 1,
    gatewayConnectionInstanceId: 'instance-a',
    gatewayProcess: null,
    desktopGatewayConnectionSuspendedForExit: () => false,
    activeDesktopProfile: () => ({ home: 'synthetic-profile' }),
    desktopProfileFingerprint: () => 'synthetic-fingerprint',
  })
  vm.runInContext(compiledSnapshot, context)
  const descriptor = context.snapshot()
  assert.equal(descriptor.controlUiBasePath, expectedBasePath)
  assert.equal(descriptor.httpUrl, gatewayState.url, 'Control UI prefix must not alter API origin')
  assert.equal(descriptor.wsUrl, `${gatewayState.url.replace(/^http/, 'ws')}/ws`)
  assert.equal(descriptor.schemaVersion, 1, 'the optional field remains compatible with older renderers')
  gatewayState.owned = false
  assert.equal(Object.hasOwn(context.snapshot(), 'controlUiBasePath'), false,
    'an external Gateway must not inherit a previous owned profile mount prefix')
}
const handler = parsed.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === 'handleDesktopRendererRequest')
assert.ok(handler, 'test the production protocol handler, including its selected mount prefix')
const compiled = ts.transpileModule(`${handler.getText(parsed)}\nglobalThis.handle = handleDesktopRendererRequest`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText
for (const { owned, config, basePath } of [
  { owned: true, config: null, basePath: '/control' },
  { owned: true, config: '[control_ui]\nbase_path = "/"', basePath: '' },
  { owned: true, config: '[control_ui]\nbase_path = "/custom/ui/"', basePath: '/custom/ui' },
  { owned: false, config: '[control_ui]\nbase_path = "/control"', basePath: '' },
]) {
  const requests = []
  const server = createServer((request, response) => {
    requests.push({ method: request.method, url: request.url })
    const url = new URL(request.url, 'http://127.0.0.1')
    const prefix = `${basePath}/static/img/`
    if (!url.pathname.startsWith(prefix)) {
      response.writeHead(200, { 'content-type': 'text/html' })
      response.end('<!doctype html><title>SPA fallback</title>')
      return
    }
    const image = images.get(decodeURIComponent(url.pathname.slice(prefix.length)))
    if (!image) {
      response.writeHead(404, { 'content-type': 'text/plain' })
      response.end('Not found')
      return
    }
    response.writeHead(200, { 'content-type': 'image/png', 'content-length': image.length })
    response.end(request.method === 'HEAD' ? undefined : image)
  })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  try {
    const origin = `http://127.0.0.1:${server.address().port}`
    const context = vm.createContext({
      Response, routeDesktopRendererRequest, gatewayState: { owned },
      ownedGatewayControlUiBasePath: desktopControlUiBasePath(config),
      proxyDesktopRendererRequest: (request, pathAndQuery) => fetch(`${origin}${pathAndQuery}`, {
        method: request.method,
      }),
    })
    vm.runInContext(compiled, context)
    for (const [name, image] of images) {
      for (const method of ['GET', 'HEAD']) {
        const path = `/static/img/${name}?cache=a%2Fb&v=1&v=2`
        const route = routeDesktopRendererRequest(`opensquilla-app://desktop${path}`, method, basePath)
        assert.equal(route.kind, 'gateway')
        const response = await context.handle(new Request(`opensquilla-app://desktop${path}`, { method }))
        assert.equal(response.status, 200)
        assert.equal(response.headers.get('content-type'), 'image/png')
        assert.equal(response.headers.get('content-length'), String(image.length))
        const body = Buffer.from(await response.arrayBuffer())
        assert.deepEqual(body, method === 'HEAD' ? Buffer.alloc(0) : image)
        assert.deepEqual(requests.at(-1), { method, url: `${basePath}${path}` })
      }
    }
    const missing = routeDesktopRendererRequest('opensquilla-app://desktop/static/img/missing.png', 'GET', basePath)
    assert.equal(missing.kind, 'gateway')
    assert.equal((await fetch(`${origin}${missing.pathAndQuery}`)).status, 404)
  } finally {
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
}

console.log('Desktop renderer protocol tests passed.')
