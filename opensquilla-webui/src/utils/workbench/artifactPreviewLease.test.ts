import { describe, expect, it, vi } from 'vitest'
import {
  ArtifactPreviewLeaseError,
  createArtifactPreviewLease,
  parseArtifactPreviewLease,
  parseArtifactPreviewLeaseRenewal,
  renewArtifactPreviewLease,
  revokeArtifactPreviewLease,
} from './artifactPreviewLease'

const lease = {
  version: 1,
  lease_id: 'lease-1',
  effective_mode: 'full',
  launch_url: 'http://p-token.localhost:43123/index.html',
  entrypoint: 'index.html',
  expires_at: '2026-07-29T00:00:00Z',
  preview_origin: 'http://p-token.localhost:43123',
  idle_timeout_seconds: 28_800,
  source: {
    kind: 'bundle',
    collection_status: 'partial',
    file_count: 3,
    total_bytes: 42,
    warning_codes: ['missing_local_resource'],
  },
}

describe('artifact preview lease client', () => {
  it('creates Desktop leases through the native broker without browser fetch', async () => {
    const fetchImpl = vi.fn()
    const create = vi.fn(async () => ({
      ok: true as const,
      status: 201,
      payload: lease,
    }))
    const result = await createArtifactPreviewLease(
      { id: 'art-fixture' },
      'full',
      'desktop',
      {
        authToken: 'token',
        baseOrigin: 'http://127.0.0.1:18791',
        fetchImpl: fetchImpl as typeof fetch,
        nativeBroker: {
          createArtifactPreviewLease: create,
        },
        sessionKey: 'agent:main:webchat:1',
      },
    )

    expect(result.source.collection_status).toBe('partial')
    expect(create).toHaveBeenCalledWith({
      version: 1,
      artifactId: 'art-fixture',
      mode: 'full',
      scopeId: 'agent:main:webchat:1',
      authToken: 'token',
    })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('fails explicitly instead of issuing a Desktop fetch without a broker', async () => {
    const fetchImpl = vi.fn()
    await expect(createArtifactPreviewLease(
      { id: 'art-fixture' },
      'full',
      'desktop',
      {
        baseOrigin: 'http://127.0.0.1:18791',
        fetchImpl: fetchImpl as typeof fetch,
        sessionKey: 'agent:main:webchat:1',
      },
    )).rejects.toMatchObject({
      status: 0,
      code: 'DESKTOP_PREVIEW_BROKER_UNAVAILABLE',
    })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('renews and revokes without putting credentials in URLs', async () => {
    const renewal = {
      version: 1,
      lease_id: 'lease-1',
      expires_at: '2026-07-29T00:15:00Z',
    }
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(renewal), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
    const context = {
      authToken: 'secret',
      baseOrigin: 'https://control.example',
      fetchImpl: fetchImpl as typeof fetch,
      sessionKey: 'session-a',
    }

    expect(await renewArtifactPreviewLease('lease-1', context)).toEqual(renewal)
    await revokeArtifactPreviewLease('lease-1', context)

    expect(fetchImpl.mock.calls[0]?.[0]).toBe(
      'https://control.example/api/v1/artifact-preview-leases/lease-1/renew',
    )
    expect(fetchImpl.mock.calls[1]?.[0]).toBe(
      'https://control.example/api/v1/artifact-preview-leases/lease-1',
    )
    expect(String(fetchImpl.mock.calls[0]?.[0])).not.toContain('secret')
  })

  it('renews and revokes Desktop leases through the same native broker', async () => {
    const fetchImpl = vi.fn()
    const renew = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      payload: {
        version: 1,
        lease_id: 'lease-1',
        expires_at: '2026-07-29T00:15:00Z',
      },
    }))
    const revoke = vi.fn(async () => ({
      ok: true as const,
      status: 204,
      payload: undefined,
    }))
    const context = {
      authToken: 'secret',
      baseOrigin: 'http://127.0.0.1:18791',
      fetchImpl: fetchImpl as typeof fetch,
      nativeBroker: {
        renewArtifactPreviewLease: renew,
        revokeArtifactPreviewLease: revoke,
      },
      sessionKey: 'session-a',
    }

    expect(await renewArtifactPreviewLease('lease-1', context)).toMatchObject({
      lease_id: 'lease-1',
    })
    await revokeArtifactPreviewLease('lease-1', context)

    expect(renew).toHaveBeenCalledWith({
      version: 1,
      leaseId: 'lease-1',
      scopeId: 'session-a',
      authToken: 'secret',
    })
    expect(revoke).toHaveBeenCalledWith({
      version: 1,
      leaseId: 'lease-1',
      scopeId: 'session-a',
      authToken: 'secret',
    })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('rejects malformed launch URLs and preserves HTTP failure status', async () => {
    expect(() => parseArtifactPreviewLease({
      ...lease,
      launch_url: 'file:///etc/passwd',
    })).toThrow(ArtifactPreviewLeaseError)
    expect(() => parseArtifactPreviewLease({
      ...lease,
      effective_mode: 'future',
    })).toThrow(ArtifactPreviewLeaseError)
    expect(() => parseArtifactPreviewLeaseRenewal({
      version: 1,
      lease_id: 'lease-1',
    })).toThrow(ArtifactPreviewLeaseError)

    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      code: 'LEASE_LIMIT',
      detail: 'Close an existing preview.',
    }), {
      status: 429,
      headers: { 'content-type': 'application/json' },
    }))
    await expect(createArtifactPreviewLease(
      { id: 'artifact-1' },
      'full',
      'web',
      {
        baseOrigin: 'https://control.example',
        fetchImpl: fetchImpl as typeof fetch,
      },
    )).rejects.toMatchObject({
      status: 429,
      code: 'LEASE_LIMIT',
    })
  })

  it('resolves the remote offline capability path against the trusted gateway', () => {
    expect(parseArtifactPreviewLease({
      ...lease,
      effective_mode: 'offline',
      launch_url: '/api/v1/artifact-preview/capability/index.html',
      preview_origin: null,
    }, 'https://control.example').launch_url).toBe(
      'https://control.example/api/v1/artifact-preview/capability/index.html',
    )
    expect(() => parseArtifactPreviewLease({
      ...lease,
      effective_mode: 'offline',
      launch_url: 'https://foreign.example/index.html',
      preview_origin: null,
    }, 'https://control.example')).toThrow(ArtifactPreviewLeaseError)
  })
})
