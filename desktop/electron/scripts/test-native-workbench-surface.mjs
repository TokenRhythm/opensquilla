import assert from 'node:assert/strict'

import {
  clampNativeWorkbenchSurfaceRect,
  NATIVE_WORKBENCH_MAX_HTML_BYTES,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchNetworkUrlAllowed,
  parseNativeWorkbenchCreateRequest,
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
