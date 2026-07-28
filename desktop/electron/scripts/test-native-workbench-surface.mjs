import assert from 'node:assert/strict'

import {
  clampNativeWorkbenchSurfaceRect,
  NATIVE_WORKBENCH_CAPABILITIES,
  NATIVE_WORKBENCH_MAX_HTML_BYTES,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchDownloadAllowed,
  nativeWorkbenchNetworkUrlAllowed,
  nativeWorkbenchV2NetworkUrlAllowed,
  parseNativeWorkbenchCreateRequest,
  parseNativeWorkbenchNavigationRequest,
  parseNativeWorkbenchPermissionResponse,
  parseNativeWorkbenchSurfaceId,
  parseNativeWorkbenchSurfaceRectRequest,
} from '../dist/native-workbench-surface-contract.js'

const parsed = parseNativeWorkbenchCreateRequest({
  version: 1,
  surfaceId: 'artifact:synthetic-1',
  kind: 'artifact-html',
  payload: {
    data: new TextEncoder().encode('<!doctype html><title>Fixture</title>'),
    name: '../../fixture.html',
    mime: 'text/html; charset=utf-8',
    scopeId: 'agent:fixture:webchat:fixture',
    allowRemoteResources: false,
  },
})
assert.equal(parsed.payload.name, 'fixture.html')
assert.equal(parsed.payload.mime, 'text/html')
assert.equal(parsed.payload.data.byteLength > 0, true)
assert.equal(
  parseNativeWorkbenchCreateRequest({
    ...parsed,
    payload: { ...parsed.payload, allowRemoteResources: true },
  }).payload.allowRemoteResources,
  true,
)

assert.deepEqual(NATIVE_WORKBENCH_CAPABILITIES, {
  latestVersion: 2,
  protocolVersions: [1, 2],
  versions: [1, 2],
  kinds: ['artifact-html', 'artifact-preview', 'url-preview'],
  modes: ['full', 'offline'],
  navigationActions: ['navigate', 'back', 'forward', 'reload', 'stop', 'open-external'],
  permissionResponses: true,
  maxSurfaces: 8,
})

const previewOrigin = 'http://p-0123456789abcdef0123456789abcdef.localhost:48721'
const parsedArtifactV2 = parseNativeWorkbenchCreateRequest({
  version: 2,
  surfaceId: 'artifact:v2',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
})
assert.deepEqual(parsedArtifactV2, {
  version: 2,
  surfaceId: 'artifact:v2',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
})
assert.deepEqual(
  parseNativeWorkbenchCreateRequest({
    version: 2,
    surfaceId: 'browser:v2',
    kind: 'url-preview',
    payload: {
      url: 'https://example.test/path',
      scopeId: 'synthetic:url',
    },
  }),
  {
    version: 2,
    surfaceId: 'browser:v2',
    kind: 'url-preview',
    payload: {
      url: 'https://example.test/path',
      scopeId: 'synthetic:url',
    },
  },
)
for (const payload of [
  {
    launchUrl: 'https://p-0123456789abcdef0123456789abcdef.localhost:48721/index.html',
    expectedOrigin: 'https://p-0123456789abcdef0123456789abcdef.localhost:48721',
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
  {
    launchUrl: `${previewOrigin}/index.html?token=leak`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
  {
    launchUrl: `${previewOrigin}/index.html`,
    expectedOrigin: 'http://127.0.0.1:48721',
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
]) {
  assert.throws(
    () => parseNativeWorkbenchCreateRequest({
      version: 2,
      surfaceId: 'artifact:v2',
      kind: 'artifact-preview',
      payload,
    }),
    /preview address|preview origin/,
  )
}
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    version: 2,
    surfaceId: 'browser:v2',
    kind: 'url-preview',
    payload: { url: 'file:///synthetic/secret', scopeId: 'synthetic:url' },
  }),
  /HTTP or HTTPS/,
)
assert.deepEqual(
  parseNativeWorkbenchNavigationRequest({
    version: 2,
    surfaceId: 'browser:v2',
    action: 'navigate',
    url: 'http://127.0.0.1:5173/demo',
  }),
  {
    version: 2,
    surfaceId: 'browser:v2',
    action: 'navigate',
    url: 'http://127.0.0.1:5173/demo',
  },
)
assert.throws(
  () => parseNativeWorkbenchNavigationRequest({
    version: 2,
    surfaceId: 'browser:v2',
    action: 'reload',
    url: 'https://example.test',
  }),
  /does not accept/,
)
assert.deepEqual(
  parseNativeWorkbenchPermissionResponse({
    version: 2,
    surfaceId: 'browser:v2',
    requestId: '00000000-0000-4000-8000-000000000000',
    allow: true,
  }),
  {
    version: 2,
    surfaceId: 'browser:v2',
    requestId: '00000000-0000-4000-8000-000000000000',
    allow: true,
  },
)

assert.equal(parseNativeWorkbenchSurfaceId('artifact:one'), 'artifact:one')
assert.throws(() => parseNativeWorkbenchSurfaceId('../artifact'), /valid native Workbench surface/)
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    ...parsed,
    payload: { ...parsed.payload, data: new Uint8Array(NATIVE_WORKBENCH_MAX_HTML_BYTES + 1) },
  }),
  /5 MiB preview limit/,
)
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    ...parsed,
    kind: 'browser',
  }),
  /Unsupported native Workbench request/,
)
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    ...parsed,
    payload: { ...parsed.payload, mime: 'application/javascript' },
  }),
  /Only HTML artifacts/,
)

const rectRequest = parseNativeWorkbenchSurfaceRectRequest({
  surfaceId: parsed.surfaceId,
  x: -12.4,
  y: 50.2,
  width: 900.1,
  height: 700.6,
  visible: true,
})
assert.deepEqual(
  clampNativeWorkbenchSurfaceRect(rectRequest, { width: 800, height: 600 }),
  { x: 0, y: 50, width: 800, height: 550 },
)
assert.equal(
  clampNativeWorkbenchSurfaceRect(
    { x: 900, y: 900, width: 20, height: 20 },
    { width: 800, height: 600 },
  ),
  null,
)
assert.deepEqual(
  nativeWorkbenchCssRectToDip({ x: 400, y: 64, width: 416, height: 560 }, 1.25),
  { x: 500, y: 80, width: 520, height: 700 },
  'DOM CSS pixels scale by Chromium zoom but not OS devicePixelRatio',
)
assert.deepEqual(
  nativeWorkbenchCssRectToDip({ x: 4, y: 5, width: 6, height: 7 }, Number.NaN),
  { x: 4, y: 5, width: 6, height: 7 },
  'invalid zoom factors fail closed to the 1x geometry',
)
assert.equal(
  nativeWorkbenchArtifactUrl('00000000-0000-4000-8000-000000000000'),
  'opensquilla-artifact://00000000-0000-4000-8000-000000000000/index.html',
)
assert.equal(parsed.payload.allowRemoteResources, false)
assert.equal(nativeWorkbenchNetworkUrlAllowed('https://assets.example.test/app.js'), false)
assert.equal(nativeWorkbenchNetworkUrlAllowed('data:image/png;base64,AA=='), true)
assert.equal(nativeWorkbenchNetworkUrlAllowed('blob:null/fixture'), true)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/poster.png',
    true,
    'image',
  ),
  true,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/theme.css',
    true,
    'stylesheet',
  ),
  true,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/app.js',
    true,
    'script',
  ),
  false,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/data.json',
    true,
    'xhr',
  ),
  false,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed('https://assets.example.test/unknown', true),
  false,
)
assert.equal(nativeWorkbenchNetworkUrlAllowed('http://assets.example.test/app.js'), false)
assert.equal(nativeWorkbenchNetworkUrlAllowed('file:///synthetic/secret.txt'), false)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('https://cdn.example.test/app.js', 'full'),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('ws://127.0.0.1:3000/socket', 'full'),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('file:///synthetic/secret.txt', 'full'),
  false,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('opensquilla-artifact://fixture/index.html', 'full'),
  false,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed(`${previewOrigin}/app.js`, 'offline', previewOrigin),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed(
    'ws://p-0123456789abcdef0123456789abcdef.localhost:48721/socket',
    'offline',
    previewOrigin,
  ),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('https://cdn.example.test/app.js', 'offline', previewOrigin),
  false,
)
assert.equal(nativeWorkbenchV2NetworkUrlAllowed('about:config', 'offline', previewOrigin), false)
assert.equal(nativeWorkbenchDownloadAllowed(true), true)
for (const untrustedGesture of [false, undefined, null, 1, 'true']) {
  assert.equal(
    nativeWorkbenchDownloadAllowed(untrustedGesture),
    false,
    "only Electron's exact user-gesture signal may authorize a save dialog",
  )
}
assert.equal(
  nativeWorkbenchArtifactRequestIsDocument(
    'opensquilla-artifact://fixture-handle/index.html',
    'GET',
    'fixture-handle',
  ),
  true,
)
assert.equal(
  nativeWorkbenchArtifactRequestIsDocument(
    'opensquilla-artifact://fixture-handle/assets/app.css',
    'GET',
    'fixture-handle',
  ),
  false,
)
assert.equal(
  nativeWorkbenchArtifactRequestIsDocument(
    'opensquilla-artifact://other-handle/index.html',
    'GET',
    'fixture-handle',
  ),
  false,
)

console.log('native Workbench surface contract checks passed')
