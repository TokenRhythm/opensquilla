import assert from 'node:assert/strict'
import http from 'node:http'

import {
  ArtifactPreviewLeaseBroker,
  parseArtifactPreviewLeaseControlRequest,
  parseArtifactPreviewLeaseCreateRequest,
} from '../dist/artifact-preview-lease-broker.js'

const previewToken = '0123456789abcdef0123456789abcdef'
const previewOrigin = `http://p-${previewToken}.localhost:48721`
const leaseId = 'apl-synthetic_lease'
const scopeId = 'agent:fixture:webchat:session'
const expiresAt = new Date(Date.now() + 60 * 60 * 1000).toISOString()
const requests = []

const server = http.createServer(async (request, response) => {
  const chunks = []
  for await (const chunk of request) chunks.push(chunk)
  const body = Buffer.concat(chunks).toString('utf8')
  requests.push({
    method: request.method,
    url: request.url,
    headers: request.headers,
    body,
  })

  response.setHeader('content-type', 'application/json')
  if (request.url === '/api/v1/artifacts/art-denied/preview-leases') {
    response.statusCode = 429
    response.end(JSON.stringify({
      code: 'PREVIEW_LEASE_LIMIT',
      error: 'Close an existing preview.',
    }))
    return
  }
  if (request.url === '/api/v1/artifacts/art-old-gateway/preview-leases') {
    response.statusCode = 404
    response.end(JSON.stringify({ detail: 'Not Found' }))
    return
  }
  if (request.url === '/api/v1/artifacts/art-invalid/preview-leases') {
    response.statusCode = 201
    response.end(JSON.stringify({
      version: 1,
      lease_id: 'apl-invalid',
      effective_mode: 'full',
      launch_url: 'https://foreign.example/index.html',
      entrypoint: 'index.html',
      expires_at: expiresAt,
      preview_origin: 'https://foreign.example',
      idle_timeout_seconds: 28_800,
      source: {
        kind: 'single_file',
        collection_status: 'not_applicable',
        file_count: 1,
        total_bytes: 1,
        warning_codes: [],
      },
    }))
    return
  }
  if (request.url === '/api/v1/artifacts/art-synthetic/preview-leases') {
    assert.equal(request.method, 'POST')
    assert.equal(request.headers.origin, undefined)
    assert.equal(request.headers['x-opensquilla-session-key'], scopeId)
    assert.deepEqual(JSON.parse(body), {
      version: 1,
      mode: 'full',
      client: 'desktop',
    })
    response.statusCode = 201
    response.end(JSON.stringify({
      version: 1,
      lease_id: leaseId,
      effective_mode: 'full',
      launch_url: `${previewOrigin}/index.html`,
      entrypoint: 'index.html',
      expires_at: expiresAt,
      preview_origin: previewOrigin,
      idle_timeout_seconds: 28_800,
      source: {
        kind: 'bundle',
        collection_status: 'complete',
        file_count: 2,
        total_bytes: 42,
        warning_codes: [],
      },
    }))
    return
  }
  if (request.url === `/api/v1/artifact-preview-leases/${leaseId}/renew`) {
    assert.equal(request.method, 'POST')
    assert.equal(request.headers.origin, undefined)
    assert.equal(request.headers['x-opensquilla-session-key'], scopeId)
    response.statusCode = 200
    response.end(JSON.stringify({
      version: 1,
      lease_id: leaseId,
      expires_at: new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString(),
    }))
    return
  }
  if (request.url === `/api/v1/artifact-preview-leases/${leaseId}`) {
    assert.equal(request.method, 'DELETE')
    assert.equal(request.headers.origin, undefined)
    assert.equal(request.headers['x-opensquilla-session-key'], scopeId)
    response.statusCode = 204
    response.end()
    return
  }
  response.statusCode = 404
  response.end(JSON.stringify({ code: 'NOT_FOUND', error: 'Not found.' }))
})

await new Promise((resolve, reject) => {
  server.once('error', reject)
  server.listen(0, '127.0.0.1', resolve)
})

try {
  const address = server.address()
  assert.equal(typeof address, 'object')
  let gatewayUrl = `http://127.0.0.1:${address.port}`
  const broker = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => gatewayUrl,
  })

  assert.deepEqual(parseArtifactPreviewLeaseCreateRequest({
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'offline',
  }), {
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'offline',
  })
  assert.throws(() => parseArtifactPreviewLeaseCreateRequest({
    version: 1,
    artifactId: '../artifact',
    scopeId,
    mode: 'full',
  }))
  assert.throws(() => parseArtifactPreviewLeaseCreateRequest({
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'full',
    unexpected: true,
  }))
  assert.throws(() => parseArtifactPreviewLeaseControlRequest({
    version: 1,
    leaseId: '../lease',
    scopeId,
  }))

  const created = await broker.create({
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'full',
    authToken: 'synthetic-bearer',
  })
  assert.equal(created.ok, true)
  assert.equal(created.status, 201)
  assert.equal(created.ok && created.payload.lease_id, leaseId)
  assert.equal(requests[0].headers.authorization, 'Bearer synthetic-bearer')

  const exactGrant = {
    launchUrl: `${previewOrigin}/index.html`,
    expectedOrigin: previewOrigin,
    scopeId,
    mode: 'full',
  }
  assert.equal(broker.authorizesSurface(exactGrant), true)
  assert.equal(broker.authorizesSurface({ ...exactGrant, scopeId: `${scopeId}:other` }), false)
  assert.equal(broker.authorizesSurface({ ...exactGrant, mode: 'offline' }), false)
  assert.equal(broker.authorizesSurface({
    ...exactGrant,
    launchUrl: `${previewOrigin}/other.html`,
  }), false)

  const requestCountBeforeWrongScope = requests.length
  assert.deepEqual(await broker.renew({
    version: 1,
    leaseId,
    scopeId: `${scopeId}:other`,
  }), {
    ok: false,
    status: 404,
    code: 'BROKER_LEASE_NOT_FOUND',
    message: 'The Desktop preview lease is unavailable.',
  })
  assert.equal(requests.length, requestCountBeforeWrongScope)

  // A wrong-scope control attempt invalidates the local grant rather than
  // allowing that lease identity to be probed or reused.
  const recreated = await broker.create({
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'full',
    authToken: 'synthetic-bearer',
  })
  assert.equal(recreated.ok, true)

  const renewed = await broker.renew({
    version: 1,
    leaseId,
    scopeId,
    authToken: 'synthetic-bearer',
  })
  assert.equal(renewed.ok, true)
  assert.equal(renewed.ok && renewed.payload.lease_id, leaseId)

  const revoked = await broker.revoke({
    version: 1,
    leaseId,
    scopeId,
    authToken: 'synthetic-bearer',
  })
  assert.equal(revoked.ok, true)
  assert.equal(broker.authorizesSurface(exactGrant), false)

  const denied = await broker.create({
    version: 1,
    artifactId: 'art-denied',
    scopeId,
    mode: 'full',
  })
  assert.deepEqual(denied, {
    ok: false,
    status: 429,
    code: 'PREVIEW_LEASE_LIMIT',
    message: 'Close an existing preview.',
  })
  assert.deepEqual(await broker.create({
    version: 1,
    artifactId: 'art-old-gateway',
    scopeId,
    mode: 'full',
  }), {
    ok: false,
    status: 404,
    code: '',
    message: 'Not Found',
  })

  const invalid = await broker.create({
    version: 1,
    artifactId: 'art-invalid',
    scopeId,
    mode: 'full',
  })
  assert.deepEqual(invalid, {
    ok: false,
    status: 502,
    code: 'INVALID_RESPONSE',
    message: 'The Gateway returned an invalid preview response.',
  })

  const createdAgain = await broker.create({
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'full',
  })
  assert.equal(createdAgain.ok, true)
  gatewayUrl = 'http://127.0.0.1:9'
  assert.equal(broker.authorizesSurface(exactGrant), false)
  gatewayUrl = `http://127.0.0.1:${address.port}`
  assert.equal(
    broker.authorizesSurface(exactGrant),
    false,
    'a grant invalidated by a Gateway identity change must not become valid again',
  )

  const unavailable = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => null,
  })
  assert.deepEqual(await unavailable.create({
    version: 1,
    artifactId: 'art-synthetic',
    scopeId,
    mode: 'full',
  }), {
    ok: false,
    status: 503,
    code: 'OWNED_GATEWAY_UNAVAILABLE',
    message: 'The Desktop-owned Gateway is unavailable.',
  })

  assert.equal(requests.some(request => request.headers.origin !== undefined), false)
} finally {
  await new Promise(resolve => server.close(resolve))
}

console.log('artifact preview lease broker tests passed')
