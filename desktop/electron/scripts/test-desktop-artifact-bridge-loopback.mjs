import assert from 'node:assert/strict'

import { DesktopArtifactBridge } from '../dist/desktop-artifact-bridge.js'
import {
  DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV,
  DESKTOP_ARTIFACT_BRIDGE_URL_ENV,
  DesktopArtifactBridgeLoopbackTransport,
} from '../dist/desktop-artifact-bridge-loopback.js'

let reloadCount = 0
let slowReload = false
let releaseSlowReload
let slowReloadAbortObserved = false
let lateReloadCount = 0
const slowReloadGate = new Promise(resolve => { releaseSlowReload = resolve })
const annotationDigest = 'b'.repeat(64)
const annotationElementProof = 'c'.repeat(64)
const activePreviewArtifactId = 'art-loopback-preview'
const annotationPath = JSON.stringify([
  ['', 'html', 1],
  ['', 'body', 1],
  ['', 'button', 1],
])
const target = {
  capabilities: {
    captureSelection: false,
    resolveAnnotationSelection: true,
    focusAnnotation: true,
    browserInspect: false,
    browserAct: false,
    bindCandidatePreview: false,
    restoreCanonicalPreview: false,
    screenshot: true,
    officeFlush: false,
    reloadSurface: true,
  },
  isCurrent: () => true,
  resolveAnnotationSelection: async request => ({
    activePreviewArtifactId: request.activePreviewArtifactId,
    selectionId: request.selectionId,
    tagName: request.tagName,
    elementPath: request.elementPath,
    ...(request.domSha256 === undefined ? {} : { domSha256: request.domSha256 }),
    elementProofSha256: request.elementProofSha256,
    scopeId: 'synthetic:scope',
    rect: { x: 10, y: 20, width: 80, height: 24 },
  }),
  focusAnnotation: async request => ({
    focused: true,
    activePreviewArtifactId: request.activePreviewArtifactId,
  }),
  screenshot: async () => ({
    mime: 'image/png',
    data: Uint8Array.from([137, 80, 78, 71]),
    width: 32,
    height: 16,
  }),
  reloadSurface: async (_request, signal) => {
    if (slowReload) {
      await slowReloadGate
      if (signal.aborted) {
        slowReloadAbortObserved = true
        throw new Error('The delayed reload was cancelled.')
      }
      lateReloadCount += 1
      return { reloaded: true }
    }
    reloadCount += 1
    return { reloaded: true }
  },
}

const audit = []
const bridge = new DesktopArtifactBridge({ getActiveTarget: () => target })
const transport = new DesktopArtifactBridgeLoopbackTransport(bridge, {
  audit: entry => audit.push(entry),
})
const environment = await transport.start()
assert.deepEqual(await transport.start(), environment, 'start must be idempotent')

const endpoint = environment[DESKTOP_ARTIFACT_BRIDGE_URL_ENV]
const token = environment[DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV]
const parsed = new URL(endpoint)
assert.equal(parsed.protocol, 'http:')
assert.equal(parsed.hostname, '127.0.0.1')
assert.ok(Number(parsed.port) > 0)
assert.equal(Buffer.from(token, 'base64url').length, 32)

async function post(path, body, options = {}) {
  const headers = {
    authorization: `Bearer ${token}`,
    'content-type': 'application/json',
    'x-opensquilla-deadline-at-ms': String(Date.now() + 2_000),
    ...options.headers,
  }
  return fetch(`${endpoint}${path}`, {
    method: options.method ?? 'POST',
    headers,
    body: typeof body === 'string' ? body : JSON.stringify(body),
  })
}

const capabilitiesResponse = await post('/v1/capabilities', { version: 3 })
assert.equal(capabilitiesResponse.status, 200)
const capabilities = await capabilitiesResponse.json()
assert.deepEqual(capabilities, {
  ok: true,
  value: {
    version: 4,
    available: true,
    captureSelection: false,
    resolveAnnotationSelection: true,
    focusAnnotation: true,
    browserInspect: false,
    browserAct: false,
    bindCandidatePreview: false,
    restoreCanonicalPreview: false,
    screenshot: true,
    officeFlush: false,
    reloadSurface: true,
  },
})

const resolvedAnnotationResponse = await post('/v1/invoke', {
  version: 3,
  method: 'resolveAnnotationSelection',
  request: {
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_loopback',
    tagName: 'button',
    elementPath: annotationPath,
    domSha256: annotationDigest,
    elementProofSha256: annotationElementProof,
  },
})
assert.equal(resolvedAnnotationResponse.status, 200)
assert.deepEqual(await resolvedAnnotationResponse.json(), {
  ok: true,
  method: 'resolveAnnotationSelection',
  value: {
    activePreviewArtifactId,
    selectionId: 'selection_loopback',
    tagName: 'button',
    elementPath: annotationPath,
    domSha256: annotationDigest,
    elementProofSha256: annotationElementProof,
    scopeId: 'synthetic:scope',
    rect: { x: 10, y: 20, width: 80, height: 24 },
  },
})

const resolvedAnnotationWithoutDomResponse = await post('/v1/invoke', {
  version: 3,
  method: 'resolveAnnotationSelection',
  request: {
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_loopback_without_dom',
    tagName: 'button',
    elementPath: annotationPath,
    elementProofSha256: annotationElementProof,
  },
})
assert.equal(resolvedAnnotationWithoutDomResponse.status, 200)
assert.deepEqual(await resolvedAnnotationWithoutDomResponse.json(), {
  ok: true,
  method: 'resolveAnnotationSelection',
  value: {
    activePreviewArtifactId,
    selectionId: 'selection_loopback_without_dom',
    tagName: 'button',
    elementPath: annotationPath,
    elementProofSha256: annotationElementProof,
    scopeId: 'synthetic:scope',
    rect: { x: 10, y: 20, width: 80, height: 24 },
  },
})

const focusAnnotationResponse = await post('/v1/invoke', {
  version: 3,
  method: 'focusAnnotation',
  request: {
    version: 3,
    activePreviewArtifactId,
    annotationId: 'annotation_loopback',
    scopeId: 'synthetic:scope',
    tagName: 'button',
    elementPath: annotationPath,
    elementProofSha256: annotationElementProof,
  },
})
assert.equal(focusAnnotationResponse.status, 200)
assert.deepEqual(await focusAnnotationResponse.json(), {
  ok: true,
  method: 'focusAnnotation',
  value: { focused: true, activePreviewArtifactId },
})

const reloadResponse = await post('/v1/invoke', {
  version: 3,
  method: 'reloadSurface',
  request: { version: 3 },
})
assert.equal(reloadResponse.status, 200)
assert.deepEqual(await reloadResponse.json(), {
  ok: true,
  method: 'reloadSurface',
  value: { reloaded: true },
})
assert.equal(reloadCount, 1)

slowReload = true
const deadlineResponse = await post(
  '/v1/invoke',
  {
    version: 3,
    method: 'reloadSurface',
    request: { version: 3 },
  },
  { headers: { 'x-opensquilla-deadline-at-ms': String(Date.now() + 100) } },
)
assert.equal(deadlineResponse.status, 408)
assert.equal((await deadlineResponse.json()).code, 'deadline-exceeded')

// The expired operation must release the bridge queue before its handler
// settles, while the propagated signal prevents a late side effect.
slowReload = false
const postDeadlineReloadResponse = await post('/v1/invoke', {
  version: 3,
  method: 'reloadSurface',
  request: { version: 3 },
})
assert.equal(postDeadlineReloadResponse.status, 200)
assert.equal(reloadCount, 2)
releaseSlowReload()
await new Promise(resolve => setTimeout(resolve, 0))
assert.equal(slowReloadAbortObserved, true)
assert.equal(lateReloadCount, 0)

const screenshotResponse = await post('/v1/invoke', {
  version: 3,
  method: 'screenshot',
  request: { version: 3 },
})
assert.equal(screenshotResponse.status, 200)
assert.deepEqual(await screenshotResponse.json(), {
  ok: true,
  method: 'screenshot',
  value: {
    mime: 'image/png',
    dataBase64: Buffer.from([137, 80, 78, 71]).toString('base64'),
    width: 32,
    height: 16,
  },
})

const unsupportedResponse = await post('/v1/invoke', {
  version: 3,
  method: 'captureSelection',
  request: { version: 3 },
})
assert.equal(unsupportedResponse.status, 200)
assert.equal((await unsupportedResponse.json()).code, 'unsupported')

const unauthorizedResponse = await fetch(`${endpoint}/v1/capabilities`, {
  method: 'POST',
  headers: {
    authorization: 'Bearer wrong-token',
    'content-type': 'application/json',
    'x-opensquilla-deadline-at-ms': String(Date.now() + 2_000),
  },
  body: JSON.stringify({ version: 3 }),
})
assert.equal(unauthorizedResponse.status, 401)
assert.equal((await unauthorizedResponse.json()).code, 'unauthorized')

const wrongMethodResponse = await post(
  '/v1/capabilities',
  { version: 3 },
  { method: 'PUT' },
)
assert.equal(wrongMethodResponse.status, 405)

const wrongContentTypeResponse = await post(
  '/v1/capabilities',
  { version: 3 },
  { headers: { 'content-type': 'text/plain' } },
)
assert.equal(wrongContentTypeResponse.status, 415)

const expiredResponse = await post(
  '/v1/capabilities',
  { version: 3 },
  { headers: { 'x-opensquilla-deadline-at-ms': String(Date.now() - 1_000) } },
)
assert.equal(expiredResponse.status, 408)

const unknownMethodResponse = await post('/v1/invoke', {
  version: 3,
  method: 'rawCdp',
  request: { version: 3, expression: 'process.exit()' },
})
assert.equal(unknownMethodResponse.status, 400)
assert.equal((await unknownMethodResponse.json()).code, 'invalid-request')

const mismatchedProtocolResponse = await post('/v1/invoke', {
  version: 3,
  method: 'reloadSurface',
  request: { version: 4 },
})
assert.equal(mismatchedProtocolResponse.status, 400)
assert.equal((await mismatchedProtocolResponse.json()).code, 'invalid-request')

const oversizedResponse = await post('/v1/capabilities', 'x'.repeat(64 * 1024 + 1))
assert.equal(oversizedResponse.status, 413)

assert.ok(audit.length >= 9)
assert.ok(audit.every(entry => !JSON.stringify(entry).includes(token)))
assert.ok(audit.every(entry => !('payload' in entry)))

await transport.close()
await assert.rejects(() => fetch(`${endpoint}/v1/capabilities`, {
  method: 'POST',
  headers: {
    authorization: `Bearer ${token}`,
    'content-type': 'application/json',
    'x-opensquilla-deadline-at-ms': String(Date.now() + 2_000),
  },
  body: JSON.stringify({ version: 3 }),
}))

console.log('desktop artifact bridge loopback transport tests passed')
