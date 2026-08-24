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
  assert.equal(broker.resolveSurfaceArtifactId(exactGrant), 'art-synthetic')
  assert.equal(broker.authorizesSurface({ ...exactGrant, scopeId: `${scopeId}:other` }), false)
  assert.equal(
    broker.resolveSurfaceArtifactId({ ...exactGrant, scopeId: `${scopeId}:other` }),
    null,
  )
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

  const releaseSurfacePin = broker.pinSurface(exactGrant)
  assert.equal(typeof releaseSurfacePin?.release, 'function')
  const revoked = await broker.revoke({
    version: 1,
    leaseId,
    scopeId,
    authToken: 'synthetic-bearer',
  })
  assert.equal(revoked.ok, true)
  assert.equal(revoked.status, 202)
  assert.equal(broker.authorizesSurface(exactGrant), true)
  await releaseSurfacePin.release()
  assert.equal(broker.authorizesSurface(exactGrant), false)
  assert.equal(broker.resolveSurfaceArtifactId(exactGrant), null)

  let replacementNow = Date.now()
  let replacementSequence = 0
  const replacementDeletes = []
  const replacementBroker = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => gatewayUrl,
    now: () => replacementNow,
    fetchImpl: async (input, init) => {
      const url = new URL(String(input))
      if (init?.method === 'DELETE') {
        replacementDeletes.push(url.pathname)
        return new Response(null, { status: 204 })
      }
      replacementSequence += 1
      const token = String(replacementSequence).padStart(32, 'e')
      const origin = `http://p-${token}.localhost:48721`
      return new Response(JSON.stringify({
        version: 1,
        lease_id: `apl-replacement_${replacementSequence}`,
        effective_mode: 'offline',
        launch_url: `${origin}/index.html`,
        entrypoint: 'index.html',
        expires_at: new Date(
          replacementNow + (replacementSequence === 1 ? 1_000 : 60 * 60 * 1_000),
        ).toISOString(),
        preview_origin: origin,
        idle_timeout_seconds: 28_800,
        source: {
          kind: 'single_file',
          collection_status: 'not_applicable',
          file_count: 1,
          total_bytes: 42,
          warning_codes: [],
        },
      }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      })
    },
  })
  const replacementCreated = await replacementBroker.create({
    version: 1,
    artifactId: 'art-replacement',
    scopeId: `${scopeId}:replacement`,
    mode: 'offline',
  })
  assert.equal(replacementCreated.ok, true)
  const replacementOriginalGrant = {
    launchUrl: replacementCreated.payload.launch_url,
    expectedOrigin: replacementCreated.payload.preview_origin,
    scopeId: `${scopeId}:replacement`,
    mode: 'offline',
  }
  const replacementPin = replacementBroker.pinSurface(replacementOriginalGrant)
  assert.ok(replacementPin)
  replacementNow += 2_000
  const recoveredGrant = await replacementPin.ensureCurrent()
  assert.ok(recoveredGrant)
  assert.notEqual(recoveredGrant.launchUrl, replacementOriginalGrant.launchUrl)
  assert.equal(replacementSequence, 2)
  const retiredRevoke = await replacementBroker.revoke({
    version: 1,
    leaseId: replacementCreated.payload.lease_id,
    scopeId: `${scopeId}:replacement`,
  })
  assert.equal(retiredRevoke.ok, true)
  assert.equal(retiredRevoke.status, 202)
  await replacementPin.release()
  assert.deepEqual(replacementDeletes, ['/api/v1/artifact-preview-leases/apl-replacement_2'])

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

  const cleanupDeletes = []
  let cleanupLeaseSequence = 0
  let releaseFirstDelete
  const firstDeletePending = new Promise(resolve => {
    releaseFirstDelete = resolve
  })
  const cleanupBroker = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => gatewayUrl,
    fetchImpl: async (input, init) => {
      const url = new URL(String(input))
      if (init?.method === 'DELETE') {
        cleanupDeletes.push({
          url: url.href,
          authorization: init.headers.Authorization,
          scopeId: init.headers['x-opensquilla-session-key'],
        })
        if (url.pathname.endsWith('/apl-cleanup_1')) {
          await firstDeletePending
          return new Response(null, { status: 204 })
        }
        throw new Error('synthetic DELETE failure')
      }

      assert.equal(init?.method, 'POST')
      cleanupLeaseSequence += 1
      const suffix = String(cleanupLeaseSequence)
      const token = suffix.padStart(32, '0')
      const origin = `http://p-${token}.localhost:48721`
      return new Response(JSON.stringify({
        version: 1,
        lease_id: `apl-cleanup_${suffix}`,
        effective_mode: 'full',
        launch_url: `${origin}/index.html`,
        entrypoint: 'index.html',
        expires_at: expiresAt,
        preview_origin: origin,
        idle_timeout_seconds: 28_800,
        source: {
          kind: 'single_file',
          collection_status: 'not_applicable',
          file_count: 1,
          total_bytes: 42,
          warning_codes: [],
        },
      }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      })
    },
  })
  const cleanupGrant = suffix => {
    const token = String(suffix).padStart(32, '0')
    const origin = `http://p-${token}.localhost:48721`
    return {
      launchUrl: `${origin}/index.html`,
      expectedOrigin: origin,
      scopeId: `${scopeId}:cleanup-${suffix}`,
      mode: 'full',
    }
  }
  for (const suffix of [1, 2]) {
    const result = await cleanupBroker.create({
      version: 1,
      artifactId: `art-cleanup-${suffix}`,
      scopeId: `${scopeId}:cleanup-${suffix}`,
      mode: 'full',
      authToken: `cleanup-token-${suffix}`,
    })
    assert.equal(result.ok, true)
    assert.equal(cleanupBroker.authorizesSurface(cleanupGrant(suffix)), true)
  }

  const cleanup = cleanupBroker.revokeAll()
  assert.equal(
    cleanupBroker.authorizesSurface(cleanupGrant(1)),
    false,
    'revokeAll must remove local authority before its DELETE requests settle',
  )
  assert.equal(cleanupBroker.authorizesSurface(cleanupGrant(2)), false)

  const replacement = await cleanupBroker.create({
    version: 1,
    artifactId: 'art-cleanup-3',
    scopeId: `${scopeId}:cleanup-3`,
    mode: 'full',
    authToken: 'cleanup-token-3',
  })
  assert.equal(replacement.ok, true)
  assert.equal(cleanupBroker.authorizesSurface(cleanupGrant(3)), true)

  releaseFirstDelete()
  await cleanup
  assert.deepEqual(cleanupDeletes, [
    {
      url: `${gatewayUrl}/api/v1/artifact-preview-leases/apl-cleanup_1`,
      authorization: 'Bearer cleanup-token-1',
      scopeId: `${scopeId}:cleanup-1`,
    },
    {
      url: `${gatewayUrl}/api/v1/artifact-preview-leases/apl-cleanup_2`,
      authorization: 'Bearer cleanup-token-2',
      scopeId: `${scopeId}:cleanup-2`,
    },
  ])
  assert.equal(
    cleanupBroker.authorizesSurface(cleanupGrant(3)),
    true,
    'cleanup for an old renderer generation must not revoke a concurrent replacement lease',
  )

  let markDeferredPostStarted
  const deferredPostStarted = new Promise(resolve => {
    markDeferredPostStarted = resolve
  })
  let releaseDeferredPost
  const deferredPostPending = new Promise(resolve => {
    releaseDeferredPost = resolve
  })
  let markStalePostStarted
  const stalePostStarted = new Promise(resolve => {
    markStalePostStarted = resolve
  })
  let releaseStalePost
  const stalePostPending = new Promise(resolve => {
    releaseStalePost = resolve
  })
  let markClearPostStarted
  const clearPostStarted = new Promise(resolve => {
    markClearPostStarted = resolve
  })
  let releaseClearPost
  const clearPostPending = new Promise(resolve => {
    releaseClearPost = resolve
  })
  const retiredDeletes = []
  let retiredGatewayUrl = gatewayUrl
  const retiredBroker = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => retiredGatewayUrl,
    fetchImpl: async (input, init) => {
      const url = new URL(String(input))
      if (init?.method === 'DELETE') {
        retiredDeletes.push({
          url: url.href,
          authorization: init.headers.Authorization,
          scopeId: init.headers['x-opensquilla-session-key'],
        })
        return new Response(null, { status: 204 })
      }

      assert.equal(init?.method, 'POST')
      const isOldGeneration = url.pathname.includes('art-retired-old')
      const isStaleInflight = url.pathname.includes('art-stale-inflight')
      const isClearInflight = url.pathname.includes('art-clear-inflight')
      if (isOldGeneration) {
        markDeferredPostStarted()
        await deferredPostPending
      }
      if (isStaleInflight) {
        markStalePostStarted()
        await stalePostPending
      }
      if (isClearInflight) {
        markClearPostStarted()
        await clearPostPending
      }
      const suffix = isOldGeneration
        ? 'old'
        : isStaleInflight
          ? 'stale'
          : isClearInflight
            ? 'clear'
            : 'new'
      const token = isOldGeneration
        ? 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        : isStaleInflight
          ? 'cccccccccccccccccccccccccccccccc'
          : isClearInflight
            ? 'dddddddddddddddddddddddddddddddd'
            : 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
      const origin = `http://p-${token}.localhost:48721`
      return new Response(JSON.stringify({
        version: 1,
        lease_id: `apl-retired_${suffix}`,
        effective_mode: 'full',
        launch_url: `${origin}/index.html`,
        entrypoint: 'index.html',
        expires_at: expiresAt,
        preview_origin: origin,
        idle_timeout_seconds: 28_800,
        source: {
          kind: 'single_file',
          collection_status: 'not_applicable',
          file_count: 1,
          total_bytes: 42,
          warning_codes: [],
        },
      }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      })
    },
  })
  const oldOrigin = 'http://p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.localhost:48721'
  const oldGrant = {
    launchUrl: `${oldOrigin}/index.html`,
    expectedOrigin: oldOrigin,
    scopeId: `${scopeId}:retired-old`,
    mode: 'full',
  }
  const oldCreate = retiredBroker.create({
    version: 1,
    artifactId: 'art-retired-old',
    scopeId: oldGrant.scopeId,
    mode: 'full',
    authToken: 'retired-old-token',
  })
  await deferredPostStarted

  await retiredBroker.revokeAll()
  const newOrigin = 'http://p-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.localhost:48721'
  const newGrant = {
    launchUrl: `${newOrigin}/index.html`,
    expectedOrigin: newOrigin,
    scopeId: `${scopeId}:retired-new`,
    mode: 'full',
  }
  const newCreate = await retiredBroker.create({
    version: 1,
    artifactId: 'art-retired-new',
    scopeId: newGrant.scopeId,
    mode: 'full',
    authToken: 'retired-new-token',
  })
  assert.equal(newCreate.ok, true)
  assert.equal(retiredBroker.authorizesSurface(newGrant), true)

  releaseDeferredPost()
  assert.deepEqual(await oldCreate, {
    ok: false,
    status: 409,
    code: 'PREVIEW_LEASE_RETIRED',
    message: 'The Desktop preview request was retired.',
  })
  assert.equal(retiredBroker.authorizesSurface(oldGrant), false)
  assert.equal(retiredBroker.authorizesSurface(newGrant), true)
  assert.deepEqual(retiredDeletes, [{
    url: `${gatewayUrl}/api/v1/artifact-preview-leases/apl-retired_old`,
    authorization: 'Bearer retired-old-token',
    scopeId: `${scopeId}:retired-old`,
  }])

  const staleOrigin = 'http://p-cccccccccccccccccccccccccccccccc.localhost:48721'
  const staleGrant = {
    launchUrl: `${staleOrigin}/index.html`,
    expectedOrigin: staleOrigin,
    scopeId: `${scopeId}:stale-inflight`,
    mode: 'full',
  }
  const staleCreate = retiredBroker.create({
    version: 1,
    artifactId: 'art-stale-inflight',
    scopeId: staleGrant.scopeId,
    mode: 'full',
    authToken: 'stale-inflight-token',
  })
  await stalePostStarted

  retiredGatewayUrl = 'http://127.0.0.1:9'
  await retiredBroker.revokeAll()
  releaseStalePost()
  assert.deepEqual(await staleCreate, {
    ok: false,
    status: 409,
    code: 'PREVIEW_LEASE_RETIRED',
    message: 'The Desktop preview request was retired.',
  })
  assert.deepEqual(
    retiredDeletes,
    [{
      url: `${gatewayUrl}/api/v1/artifact-preview-leases/apl-retired_old`,
      authorization: 'Bearer retired-old-token',
      scopeId: `${scopeId}:retired-old`,
    }],
    'stored credentials must not be sent after the owned Gateway origin changes',
  )
  retiredGatewayUrl = gatewayUrl
  assert.equal(retiredBroker.authorizesSurface(staleGrant), false)
  assert.equal(retiredBroker.authorizesSurface(newGrant), false)

  const clearOrigin = 'http://p-dddddddddddddddddddddddddddddddd.localhost:48721'
  const clearGrant = {
    launchUrl: `${clearOrigin}/index.html`,
    expectedOrigin: clearOrigin,
    scopeId: `${scopeId}:clear-inflight`,
    mode: 'full',
  }
  const clearCreate = retiredBroker.create({
    version: 1,
    artifactId: 'art-clear-inflight',
    scopeId: clearGrant.scopeId,
    mode: 'full',
    authToken: 'clear-inflight-token',
  })
  await clearPostStarted
  retiredBroker.clear()
  releaseClearPost()
  assert.deepEqual(await clearCreate, {
    ok: false,
    status: 409,
    code: 'PREVIEW_LEASE_RETIRED',
    message: 'The Desktop preview request was retired.',
  })
  assert.equal(retiredBroker.authorizesSurface(clearGrant), false)
  assert.deepEqual(retiredDeletes.at(-1), {
    url: `${gatewayUrl}/api/v1/artifact-preview-leases/apl-retired_clear`,
    authorization: 'Bearer clear-inflight-token',
    scopeId: `${scopeId}:clear-inflight`,
  })
} finally {
  await new Promise(resolve => server.close(resolve))
}

console.log('artifact preview lease broker tests passed')
