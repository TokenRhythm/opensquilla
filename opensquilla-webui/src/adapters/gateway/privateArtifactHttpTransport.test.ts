import { describe, expect, it, vi } from 'vitest'
import type { ArtifactPayload } from '@/types/artifacts'
import {
  httpBinaryResponse,
  httpTransportTestDouble,
} from '@/testing/httpTransport.test-helper'
import {
  artifactHttpAttachmentUrl,
  artifactHttpBrokerAuthToken,
  artifactHttpGatewayOpenUrl,
  isSameArtifactHttpOrigin,
  isTrustedArtifactHttpUrl,
  createArtifactPreviewLeaseHttp,
  renewArtifactPreviewLeaseHttp,
  resolveArtifactPreviewLaunch,
  revokeArtifactPreviewLeaseHttp,
  runtimeArtifactHttpBaseOrigin,
  bindAttachmentBinaryRequest,
  bindArtifactBinaryRequest,
  bindArtifactOpenRequest,
  uploadArtifactAttachment,
} from './privateArtifactHttpTransport'

function artifact(overrides: Partial<ArtifactPayload> = {}): ArtifactPayload {
  return {
    id: 'artifact-1',
    name: 'report.md',
    mime: 'text/markdown',
    download_url: '/api/v1/artifacts/artifact-1?token=old&sessionKey=old',
    ...overrides,
  }
}

describe('private Artifact HTTP transport', () => {
  it('owns sanitized same-origin Artifact routing and scoped binary requests', async () => {
    const response = httpBinaryResponse('report', { contentType: 'text/markdown' })
    const requestBinary = vi.fn(async () => response)
    const http = httpTransportTestDouble({ requestBinary })

    const request = bindArtifactBinaryRequest(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18791',
      policy: 'allow-external',
    })

    expect(request?.url).toBe('/api/v1/artifacts/artifact-1')
    await expect(request?.execute({
      sessionKey: 'agent:main:webchat:test',
      timeoutMs: 0,
    })).resolves.toBe(response)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/artifact-1', {
      sessionKey: 'agent:main:webchat:test',
      signal: undefined,
      timeoutMs: 0,
    })
  })

  it('derives the native-open route only from trusted Artifact identity', () => {
    expect(artifactHttpGatewayOpenUrl(
      artifact({ id: 'artifact / report', download_url: undefined }),
      'http://127.0.0.1:18791',
    )).toBe('/api/v1/artifacts/artifact%20%2F%20report/open')
    expect(artifactHttpGatewayOpenUrl(
      artifact({
        id: undefined,
        download_url: 'https://files.example.test/artifact-1',
      }),
      'http://127.0.0.1:18791',
    )).toBe('')
  })

  it('owns native-open request routing', async () => {
    const response = httpBinaryResponse('', { status: 202 })
    const requestBinary = vi.fn(async () => response)
    const http = httpTransportTestDouble({ requestBinary })
    const request = bindArtifactOpenRequest(http, artifact(), {
      baseOrigin: 'https://control.example',
    })

    await expect(request?.execute({ sessionKey: 'session-a', timeoutMs: 0 }))
      .resolves.toBe(response)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/artifact-1/open', {
      method: 'POST',
      sessionKey: 'session-a',
      timeoutMs: 0,
    })
  })

  it('owns same-origin attachment sanitization and scoped downloads', async () => {
    const response = httpBinaryResponse('attachment')
    const requestBinary = vi.fn(async () => response)
    const http = httpTransportTestDouble({ requestBinary })
    const raw = '/api/v1/attachments/item?token=old&sessionKey=old&variant=download#secret'

    expect(artifactHttpAttachmentUrl(raw, 'https://control.example')).toBe(
      '/api/v1/attachments/item?variant=download',
    )
    const request = bindAttachmentBinaryRequest(http, raw, {
      baseOrigin: 'https://control.example',
    })
    await expect(request?.execute({ sessionKey: 'session-a', timeoutMs: 0 }))
      .resolves.toBe(response)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/attachments/item?variant=download', {
      sessionKey: 'session-a',
      signal: undefined,
      timeoutMs: 0,
    })
  })

  it('owns the staged attachment upload endpoint', async () => {
    const requestJson = vi.fn(async () => ({ file_uuid: 'file-1' }))
    const http = httpTransportTestDouble({ requestJson })
    const form = new FormData()
    form.append('file', new File(['report'], 'report.txt'))

    await expect(uploadArtifactAttachment(http, form)).resolves.toEqual({ file_uuid: 'file-1' })
    expect(requestJson).toHaveBeenCalledWith('/api/v1/files/upload', {
      method: 'POST',
      form,
    })
  })

  it('owns preview lease routes and Desktop broker credential provenance', async () => {
    vi.stubGlobal('sessionStorage', {
      getItem: vi.fn((key: string) => key === 'opensquilla.wsToken' ? ' runtime-token ' : null),
    })
    const renewal = { version: 1, lease_id: 'lease-1' }
    const requestJson = vi.fn(async () => renewal)
    const requestBlob = vi.fn(async () => new Blob())
    const http = httpTransportTestDouble({ requestBlob, requestJson })
    try {
      expect(artifactHttpBrokerAuthToken()).toBe('runtime-token')
      await createArtifactPreviewLeaseHttp(http, 'artifact / 1', 'full', 'web', {
        baseOrigin: 'https://control.example',
        sessionKey: 'session-a',
      })
      await renewArtifactPreviewLeaseHttp(http, 'lease-1', {
        baseOrigin: 'https://control.example',
        sessionKey: 'session-a',
      })
      await revokeArtifactPreviewLeaseHttp(http, 'lease-1', {
        baseOrigin: 'https://control.example',
        sessionKey: 'session-a',
      })
    } finally {
      vi.unstubAllGlobals()
    }

    expect(requestJson).toHaveBeenNthCalledWith(
      1,
      'https://control.example/api/v1/artifacts/artifact%20%2F%201/preview-leases',
      {
        method: 'POST',
        json: { version: 1, mode: 'full', client: 'web' },
        sessionKey: 'session-a',
        timeoutMs: 0,
      },
    )
    expect(requestJson).toHaveBeenNthCalledWith(
      2,
      'https://control.example/api/v1/artifact-preview-leases/lease-1/renew',
      { method: 'POST', sessionKey: 'session-a', timeoutMs: 0 },
    )
    expect(requestBlob).toHaveBeenCalledWith(
      'https://control.example/api/v1/artifact-preview-leases/lease-1',
      {
        keepalive: true,
        method: 'DELETE',
        sessionKey: 'session-a',
        timeoutMs: 0,
      },
    )
  })

  it('recognizes only the exact Desktop proxy as a trusted opaque origin', () => {
    expect(isSameArtifactHttpOrigin(
      '/api/v1/artifacts/artifact-1',
      'opensquilla-app://desktop',
    )).toBe(true)
    expect(isTrustedArtifactHttpUrl(
      '/api/v1/artifacts/artifact-1',
      'opensquilla-app://desktop',
    )).toBe(true)
    expect(isTrustedArtifactHttpUrl(
      'other-app://desktop/api/v1/artifacts/artifact-1',
      'opensquilla-app://desktop',
    )).toBe(false)
  })

  it('uses a deterministic server-side Artifact origin fallback', () => {
    expect(runtimeArtifactHttpBaseOrigin()).toBe('http://localhost')
  })

  it('validates preview launch origins inside the private transport', () => {
    expect(resolveArtifactPreviewLaunch(
      '/api/v1/artifact-preview/capability/index.html',
      null,
      'https://control.example',
    )).toEqual({
      ok: true,
      url: 'https://control.example/api/v1/artifact-preview/capability/index.html',
    })
    expect(resolveArtifactPreviewLaunch(
      'https://foreign.example/index.html',
      null,
      'https://control.example',
    )).toEqual({ ok: false, reason: 'origin' })
    expect(resolveArtifactPreviewLaunch(
      'file:///etc/passwd',
      null,
      'https://control.example',
    )).toEqual({ ok: false, reason: 'url' })
  })
})
