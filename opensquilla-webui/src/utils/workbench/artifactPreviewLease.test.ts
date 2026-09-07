import { describe, expect, it, vi } from 'vitest'
import { ArtifactPreviewLeaseError } from '@/modules/artifactWorkbench'
import {
  HttpTransportError,
} from '@/adapters/gateway/privateHttpTransport'
import {
  httpTransportTestDouble,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
import {
  createArtifactPreviewLease,
  parseArtifactPreviewLease,
  parseArtifactPreviewLeaseRenewal,
  renewArtifactPreviewLease,
  revokeArtifactPreviewLease,
} from '@/adapters/gateway/artifactPreviewLeaseV4'

function httpTransport(
  overrides: Parameters<typeof httpTransportTestDouble>[0] = {},
): TestHttpTransport {
  return httpTransportTestDouble({
    requestBlob: vi.fn(async () => new Blob()),
    requestJson: vi.fn(async () => ({})),
    ...overrides,
  })
}

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
    const http = httpTransport()
    const create = vi.fn(async () => ({
      ok: true as const,
      status: 201,
      payload: lease,
    }))
    const result = await createArtifactPreviewLease(
      http,
      { id: 'art-fixture' },
      'full',
      'desktop',
      {
        authToken: 'token',
        baseOrigin: 'http://127.0.0.1:18791',
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
    expect(http.requestJson).not.toHaveBeenCalled()
  })

  it('resolves the Desktop broker credential inside the gateway adapter', async () => {
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn((key: string) => key === 'opensquilla.wsToken' ? 'runtime-token' : null),
    })
    const create = vi.fn(async () => ({
      ok: true as const,
      status: 201,
      payload: lease,
    }))
    try {
      await createArtifactPreviewLease(
        httpTransport(),
        { id: 'art-runtime' },
        'offline',
        'desktop',
        {
          baseOrigin: 'http://127.0.0.1:18791',
          nativeBroker: {
            createArtifactPreviewLease: create,
          },
          sessionKey: 'agent:main:webchat:runtime',
        },
      )
    } finally {
      vi.unstubAllGlobals()
    }

    expect(create).toHaveBeenCalledWith({
      version: 1,
      artifactId: 'art-runtime',
      mode: 'offline',
      scopeId: 'agent:main:webchat:runtime',
      authToken: 'runtime-token',
    })
  })

  it('fails explicitly instead of issuing a Desktop fetch without a broker', async () => {
    const http = httpTransport()
    await expect(createArtifactPreviewLease(
      http,
      { id: 'art-fixture' },
      'full',
      'desktop',
      {
        baseOrigin: 'http://127.0.0.1:18791',
        sessionKey: 'agent:main:webchat:1',
      },
    )).rejects.toMatchObject({
      status: 0,
      code: 'DESKTOP_PREVIEW_BROKER_UNAVAILABLE',
    })
    expect(http.requestJson).not.toHaveBeenCalled()
  })

  it('renews and revokes without putting credentials in URLs', async () => {
    const renewal = {
      version: 1,
      lease_id: 'lease-1',
      expires_at: '2026-07-29T00:15:00Z',
    }
    const requestJson = vi.fn(async (_endpoint: string, _options?: unknown) => renewal)
    const requestBlob = vi.fn(async (_endpoint: string, _options?: unknown) => new Blob())
    const http = httpTransport({ requestBlob, requestJson })
    const context = {
      authToken: 'secret',
      baseOrigin: 'https://control.example',
      sessionKey: 'session-a',
    }

    expect(await renewArtifactPreviewLease(http, 'lease-1', context)).toEqual(renewal)
    await revokeArtifactPreviewLease(http, 'lease-1', context)

    expect(requestJson.mock.calls[0]?.[0]).toBe(
      'https://control.example/api/v1/artifact-preview-leases/lease-1/renew',
    )
    expect(requestBlob.mock.calls[0]?.[0]).toBe(
      'https://control.example/api/v1/artifact-preview-leases/lease-1',
    )
    expect(requestJson).toHaveBeenCalledWith(expect.any(String), {
      method: 'POST',
      sessionKey: 'session-a',
      timeoutMs: 0,
    })
    expect(requestBlob).toHaveBeenCalledWith(expect.any(String), {
      keepalive: true,
      method: 'DELETE',
      sessionKey: 'session-a',
      timeoutMs: 0,
    })
    expect(String(requestJson.mock.calls[0]?.[0])).not.toContain('secret')
  })

  it.each([404, 410])('treats Web lease revoke HTTP %s as idempotent', async (status) => {
    const requestBlob = vi.fn(async (_endpoint: string, _options?: unknown) => {
      throw new HttpTransportError('http-status', 'lease already gone', status)
    })

    await expect(revokeArtifactPreviewLease(
      httpTransport({ requestBlob }),
      'lease-1',
      { baseOrigin: 'https://control.example', sessionKey: 'session-a' },
    )).resolves.toBeUndefined()
  })

  it('maps non-idempotent Web lease revoke failures to the domain error', async () => {
    const requestBlob = vi.fn(async (_endpoint: string, _options?: unknown) => {
      throw new HttpTransportError('http-status', 'service unavailable', 503, {
        code: 'PREVIEW_UNAVAILABLE',
        detail: 'Preview service is unavailable.',
      })
    })

    await expect(revokeArtifactPreviewLease(
      httpTransport({ requestBlob }),
      'lease-1',
      { baseOrigin: 'https://control.example', sessionKey: 'session-a' },
    )).rejects.toMatchObject({
      name: 'ArtifactPreviewLeaseError',
      status: 503,
      code: 'PREVIEW_UNAVAILABLE',
      message: 'Preview service is unavailable.',
    })
  })

  it('renews and revokes Desktop leases through the same native broker', async () => {
    const http = httpTransport()
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
      nativeBroker: {
        renewArtifactPreviewLease: renew,
        revokeArtifactPreviewLease: revoke,
      },
      sessionKey: 'session-a',
    }

    expect(await renewArtifactPreviewLease(http, 'lease-1', context)).toMatchObject({
      lease_id: 'lease-1',
    })
    await revokeArtifactPreviewLease(http, 'lease-1', context)

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
    expect(http.requestJson).not.toHaveBeenCalled()
    expect(http.requestBlob).not.toHaveBeenCalled()
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

    const http = httpTransport({
      requestJson: vi.fn(async () => {
        throw new HttpTransportError('http-status', 'Too many leases', 429, {
          code: 'LEASE_LIMIT',
          detail: 'Close an existing preview.',
        })
      }),
    })
    await expect(createArtifactPreviewLease(
      http,
      { id: 'artifact-1' },
      'full',
      'web',
      {
        baseOrigin: 'https://control.example',
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
