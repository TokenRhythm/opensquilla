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
  if (['/api/v1/artifacts/art-subpage/preview-leases',
    '/api/v1/artifacts/art-ignores-page/preview-leases'].includes(request.url)) {
    const { pagePath } = JSON.parse(body)
    assert.equal(pagePath, 'pages/editorial.html')
    const selected = request.url.includes('art-subpage')
    response.statusCode = 201
    response.end(JSON.stringify({
      version: 1,
      lease_id: leaseId,
      effective_mode: 'full',
      launch_url: `${previewOrigin}/${selected ? pagePath : 'index.html'}`,
      entrypoint: 'index.html',
      ...(selected ? { page_path: pagePath } : {}),
      expires_at: expiresAt,
      preview_origin: previewOrigin,
      idle_timeout_seconds: 28_800,
      source: {
        kind: 'bundle', collection_status: 'complete',
        file_count: 2, total_bytes: 42, warning_codes: [],
      },
    }))
    return
  }
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
  for (const pagePath of ['', '../secret.html', '/index.html', 'a/../index.html',
    'a\\index.html', 'index.html?x=1', '%2e%2e/secret.html', 'style.css', null]) {
    assert.throws(() => parseArtifactPreviewLeaseCreateRequest({
      version: 1, artifactId: 'art-subpage', scopeId, mode: 'full', pagePath,
    }))
  }

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

  const revoked = await broker.revoke({
    version: 1,
    leaseId,
    scopeId,
    authToken: 'synthetic-bearer',
  })
  assert.equal(revoked.ok, true)
  assert.equal(revoked.status, 204)
  assert.equal(broker.authorizesSurface(exactGrant), false)
  assert.equal(broker.resolveSurfaceArtifactId(exactGrant), null)

  const selectedPage = await broker.create({
    version: 1, artifactId: 'art-subpage', scopeId, mode: 'full',
    pagePath: 'pages/editorial.html',
  })
  assert.equal(selectedPage.ok, true)
  assert.equal(selectedPage.ok && selectedPage.payload.entrypoint, 'index.html')
  assert.equal(selectedPage.ok && selectedPage.payload.page_path, 'pages/editorial.html')
  const pageGrant = { ...exactGrant, launchUrl: `${previewOrigin}/pages/editorial.html` }
  assert.equal(broker.authorizesSurface(pageGrant), true)
  assert.equal(broker.authorizesSurface(exactGrant), false)
  await broker.revoke({ version: 1, leaseId, scopeId })

  const ignoredPage = await broker.create({
    version: 1, artifactId: 'art-ignores-page', scopeId, mode: 'full',
    pagePath: 'pages/editorial.html',
  })
  assert.equal(ignoredPage.ok, false)
  assert.equal(ignoredPage.code, 'PREVIEW_PAGE_UNSUPPORTED')
  assert.equal(requests.at(-1).method, 'DELETE')
  assert.equal(broker.authorizesSurface(pageGrant), false)

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
  cleanupBroker.clear()

  let markDeferredPostStarted
  const deferredPostStarted = new Promise(resolve => {
    markDeferredPostStarted = resolve
  })
  let releaseDeferredPost
  const deferredPostPending = new Promise(resolve => {
    releaseDeferredPost = resolve
  })
  let markRetiredOldDeleteStarted
  const retiredOldDeleteStarted = new Promise(resolve => {
    markRetiredOldDeleteStarted = resolve
  })
  let releaseRetiredOldDelete
  const retiredOldDeletePending = new Promise(resolve => {
    releaseRetiredOldDelete = resolve
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
        if (url.pathname.endsWith('/apl-retired_old')) {
          markRetiredOldDeleteStarted()
          await retiredOldDeletePending
        }
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

  let retiredCleanupSettled = false
  const retiredCleanup = retiredBroker.revokeAll().then(() => {
    retiredCleanupSettled = true
  })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(
    retiredCleanupSettled,
    false,
    'revokeAll must join creates admitted before renderer retirement',
  )
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
  await retiredOldDeleteStarted
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(
    retiredCleanupSettled,
    false,
    'revokeAll must keep waiting through the stale create compensating DELETE',
  )
  releaseRetiredOldDelete()
  await retiredCleanup
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
  const staleCleanup = retiredBroker.revokeAll()
  releaseStalePost()
  await staleCleanup
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

// Exercise the strict authenticated response parser through the actual broker.
for (const marker of [undefined, 'document-fixture', null, 4, '', ' document-fixture', 'document-fixture\n', 'x'.repeat(513)]) {
  const identityBroker = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => 'http://127.0.0.1:18791',
    fetchImpl: async (_input, init) => {
      if (init.method === 'DELETE') return new Response(null, { status: 204 })
      assert.equal(init.headers['x-opensquilla-session-key'], scopeId)
      assert.equal(init.headers['x-opensquilla-preview-working-document'], '1')
      return new Response(JSON.stringify({
        version: 1, lease_id: leaseId, effective_mode: 'full',
        launch_url: `${previewOrigin}/index.html`, entrypoint: 'index.html',
        expires_at: expiresAt, preview_origin: previewOrigin, idle_timeout_seconds: 28800,
        source: { kind: 'single_file', collection_status: 'not_applicable',
          file_count: 1, total_bytes: 42, warning_codes: [] },
        ...(marker !== undefined ? { workingDocumentId: marker } : {}),
      }), { status: 201, headers: { 'content-type': 'application/json' } })
    },
  })
  const result = await identityBroker.create({version: 1, artifactId: 'art-identity', scopeId, mode: 'full'})
  if (marker === undefined || marker === 'document-fixture') {
    assert.equal(result.ok, true, JSON.stringify(result))
    assert.equal(result.payload.workingDocumentId, marker)
    if (marker === undefined) assert.equal(Object.hasOwn(result.payload, 'workingDocumentId'), false)
  } else assert.equal(result.ok, false, 'Invalid server working identity must fail closed')
  await identityBroker.revokeAll()
}
console.log('working document lease identity tests passed (8 cases)')

function retryHarness() {
  let now = Date.parse('2026-01-01T00:00:00Z')
  let timer = null
  let sequence = 0
  let deleteReply = async () => 429
  let renewalReply = null
  let postGate = null
  let ownedGatewayUrl = 'http://127.0.0.1:18791'
  const deletes = []
  const remoteLeases = new Set()
  const broker = new ArtifactPreviewLeaseBroker({
    getOwnedGatewayUrl: () => ownedGatewayUrl,
    now: () => now,
    scheduleRetry(callback, delayMs) {
      assert.equal(timer, null, 'all pending cleanup must share one timer')
      assert.ok(delayMs >= 0 && delayMs <= 60_000)
      const scheduled = { callback, at: now + delayMs, delayMs }
      timer = scheduled
      return () => { if (timer === scheduled) timer = null }
    },
    fetchImpl: async (input, init) => {
      const url = new URL(input)
      if (init.method === 'DELETE') {
        const leaseId = url.pathname.split('/').at(-1)
        deletes.push({ leaseId, origin: url.origin,
          scopeId: init.headers['x-opensquilla-session-key'],
          authorization: init.headers.Authorization })
        const status = await deleteReply(leaseId)
        if ([204, 404, 410].includes(status)) remoteLeases.delete(leaseId)
        return new Response(status === 204 ? null : JSON.stringify({ code: 'SYNTHETIC' }),
          { status, headers: { 'content-type': 'application/json' } })
      }
      assert.equal(init.method, 'POST')
      if (url.pathname.endsWith('/renew')) {
        assert.ok(renewalReply, 'revoked cleanup records must never allow a renewal request')
        return renewalReply()
      }
      if (postGate) await postGate
      const leaseId = `apl-retry_${++sequence}`
      const origin = `http://p-${String(sequence).padStart(32, '0')}.localhost:48721`
      remoteLeases.add(leaseId)
      return new Response(JSON.stringify({
        version: 1, lease_id: leaseId, effective_mode: 'full',
        launch_url: `${origin}/index.html`, entrypoint: 'index.html',
        expires_at: new Date(now + 28_800_000).toISOString(), preview_origin: origin,
        idle_timeout_seconds: 28_800, source: { kind: 'single_file',
          collection_status: 'not_applicable', file_count: 1, total_bytes: 42, warning_codes: [] },
      }), { status: 201, headers: { 'content-type': 'application/json' } })
    },
  })
  const flush = () => new Promise(resolve => setImmediate(resolve))
  return {
    broker, deletes, remoteLeases, flush,
    timer: () => timer,
    setDeleteReply: reply => { deleteReply = reply },
    setRenewalReply: reply => { renewalReply = reply },
    setPostGate: gate => { postGate = gate },
    setGateway: url => { ownedGatewayUrl = url },
    advanceNow: milliseconds => { now += milliseconds },
    async runTimer({ advanceMs = 0 } = {}) {
      assert.ok(timer, 'a retry must be scheduled')
      now = Math.max(now + advanceMs, timer.at)
      const callback = timer.callback
      timer = null
      callback()
      await flush()
    },
    async create(extra = {}) {
      const result = await broker.create({ version: 1, artifactId: 'art-retry',
        scopeId, mode: 'full', authToken: 'synthetic-retry-token', ...extra })
      assert.equal(result.ok, true, JSON.stringify(result))
      return {
        control: { version: 1, leaseId: result.payload.lease_id, scopeId },
        grant: { launchUrl: result.payload.launch_url, expectedOrigin: result.payload.preview_origin,
          scopeId, mode: 'full' },
      }
    },
  }
}

// A caller awaits only the first attempt, even when a remote DELETE fails.
for (const transient of [408, 429, 503, 'network']) {
  const h = retryHarness()
  h.setDeleteReply(async () => {
    if (transient === 'network') throw new Error('synthetic network failure')
    return transient
  })
  const { control, grant } = await h.create()
  const revoked = h.broker.revoke(control)
  assert.equal(h.broker.authorizesSurface(grant), false, 'local authority is removed synchronously')
  assert.equal((await revoked).ok, false)
  assert.equal(h.remoteLeases.has(control.leaseId), true)
  assert.equal((await h.broker.renew(control)).status, 404)
  assert.equal((await h.broker.revoke({ ...control, scopeId: 'synthetic:wrong-scope' })).status, 404)
  assert.equal(h.deletes.length, 1, 'wrong-scope retry cannot use stored credentials')
  h.setDeleteReply(async () => 204)
  // Resource reads can extend backend idle expiry without updating the broker.
  await h.runTimer({ advanceMs: 9 * 60 * 60 * 1000 })
  assert.equal(h.remoteLeases.size, 0, 'retry must not discard cleanup using cached expiry')
  assert.equal(h.deletes.length, 2)
  assert.ok(h.deletes.every(request => request.scopeId === scopeId
    && request.authorization === 'Bearer synthetic-retry-token'))
  assert.equal(h.broker.authorizesSurface(grant), false)
  assert.equal(h.timer(), null)
  h.broker.clear()
}

for (const terminalStatus of [404, 410]) {
  const h = retryHarness()
  const { control } = await h.create()
  await h.broker.revoke(control)
  h.setDeleteReply(async () => terminalStatus)
  await h.runTimer()
  assert.equal(h.remoteLeases.size, 0)
  assert.equal(h.timer(), null, 'already-gone leases finish cleanup')
  assert.equal((await h.broker.revoke(control)).code, 'BROKER_LEASE_NOT_FOUND')
  h.broker.clear()
}

{
  const h = retryHarness()
  const { control } = await h.create()
  await h.broker.revoke(control)
  for (let attempt = 0; attempt < 9; attempt += 1) {
    const baseDelay = Math.min(1000 * 2 ** Math.min(attempt, 6), 60_000)
    assert.ok(h.timer().delayMs >= baseDelay * 0.8 && h.timer().delayMs <= baseDelay,
      'retry delays must back off with bounded jitter')
    await h.runTimer()
  }
  h.broker.clear()
  assert.equal(h.timer(), null, 'clear cancels the pending timer')
}

// Bulk retirement does not wait for backoff, and retries one lease at a time.
{
  const h = retryHarness()
  const first = await h.create()
  const second = await h.create()
  await h.broker.revokeAll()
  assert.equal(h.deletes.length, 2)
  assert.equal(h.broker.authorizesSurface(first.grant), false)
  assert.equal(h.broker.authorizesSurface(second.grant), false)
  const replacement = await h.create()
  let releaseDelete
  const deletePending = new Promise(resolve => { releaseDelete = resolve })
  h.setDeleteReply(async () => { await deletePending; return 204 })
  await h.runTimer({ advanceMs: 60_000 })
  assert.equal(h.deletes.length, 3, 'only one background DELETE may be in flight')
  assert.equal(h.timer(), null)
  releaseDelete()
  await h.flush()
  assert.equal(h.deletes.length, 4)
  assert.deepEqual([...h.remoteLeases], [replacement.control.leaseId])
  assert.equal(h.broker.authorizesSurface(replacement.grant), true,
    'cleanup must not consume replacement renderer leases')
  h.broker.clear()
}

// Slow failures at the front of the queue must not starve other due cleanup.
{
  const h = retryHarness()
  const leases = await Promise.all(Array.from({ length: 10 }, () => h.create()))
  await h.broker.revokeAll()
  const slowLeases = new Set(leases.slice(0, 5).map(lease => lease.control.leaseId))
  let attempts = 0
  h.setDeleteReply(async leaseId => {
    attempts += 1
    // Bound the simulation even if the queue keeps selecting failed leases.
    if (attempts === 100) h.broker.clear()
    if (slowLeases.has(leaseId)) {
      h.advanceNow(15_000)
      return 503
    }
    return 204
  })
  await h.runTimer({ advanceMs: 60_000 })
  assert.deepEqual(h.remoteLeases, slowLeases,
    'healthy cleanup must progress while earlier requests repeatedly fail slowly')
  h.broker.clear()
}

// A stale in-flight create and its failed compensating DELETE share retry ownership.
for (const retire of ['revokeAll', 'clear']) {
  const h = retryHarness()
  let releasePost
  h.setPostGate(new Promise(resolve => { releasePost = resolve }))
  const created = h.broker.create({ version: 1, artifactId: 'art-retry', scopeId, mode: 'full' })
  const cleanup = h.broker[retire]()
  releasePost()
  assert.equal((await created).code, 'PREVIEW_LEASE_RETIRED')
  await cleanup
  assert.equal(h.deletes.length, 1)
  if (retire === 'revokeAll') {
    h.setDeleteReply(async () => 204)
    await h.runTimer()
    assert.equal(h.remoteLeases.size, 0)
  } else {
    assert.equal(h.timer(), null, 'a late create cannot reinstall retries after lifecycle clear')
  }
  h.broker.clear()
}

for (const change of ['clear', 'gateway']) {
  const h = retryHarness()
  const { control } = await h.create()
  await h.broker.revoke(control)
  if (change === 'clear') {
    h.broker.clear()
  } else {
    h.setGateway('http://127.0.0.1:18792')
    await h.runTimer()
  }
  assert.equal(h.deletes.length, 1, 'retired credentials must never be retried against another Gateway')
  assert.equal(h.timer(), null)
  h.broker.clear()
}

// Clear also fences an already-running retry from scheduling itself again.
{
  const h = retryHarness()
  const { control } = await h.create()
  await h.broker.revoke(control)
  let releaseDelete
  h.setDeleteReply(() => new Promise(resolve => { releaseDelete = resolve }))
  await h.runTimer()
  h.broker.clear()
  releaseDelete(429)
  await h.flush()
  assert.equal(h.timer(), null)
}

for (const status of [400, 401, 403]) {
  const h = retryHarness()
  h.setDeleteReply(async () => status)
  const { control } = await h.create()
  assert.equal((await h.broker.revoke(control)).status, status)
  if (status === 400) assert.equal(h.timer(), null, 'invalid requests remain dormant')
  else assert.ok(h.timer().delayMs >= 48_000, 'authorization failures retry slowly')
  h.setDeleteReply(async () => 204)
  assert.equal((await h.broker.revoke({ ...control, authToken: 'synthetic-refreshed-token' })).ok, true)
  assert.equal(h.deletes.at(-1).authorization, 'Bearer synthetic-refreshed-token')
  assert.equal(h.remoteLeases.size, 0)
  assert.equal(h.timer(), null)
  h.broker.clear()
}

for (const bodyFailure of ['network', 'abort', 'malformed']) {
  const h = retryHarness()
  const { control, grant } = await h.create()
  h.setRenewalReply(() => new Response(bodyFailure === 'malformed' ? '{broken-json' : new ReadableStream({
    start(controller) {
      controller.error(bodyFailure === 'network'
        ? new TypeError('synthetic connection reset during body read')
        : new DOMException('synthetic body read cancellation', 'AbortError'))
    },
  }), { status: 200, headers: { 'content-type': 'application/json' } }))
  const result = await h.broker.renew(control)
  assert.equal(result.ok, false)
  assert.equal(result.status, bodyFailure === 'malformed' ? 502 : 503)
  assert.equal(result.code, bodyFailure === 'malformed' ? 'INVALID_RESPONSE' : 'PREVIEW_BROKER_UNAVAILABLE')
  assert.equal(h.broker.authorizesSurface(grant), true,
    'a failed renewal response must not discard the existing local grant')
  h.broker.clear()
}

console.log('artifact preview lease broker tests passed, including background revocation recovery')
