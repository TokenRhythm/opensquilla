import { describe, expect, it, vi } from 'vitest'
import type { ArtifactPayload } from '@/types/artifacts'
import {
  HttpTransportError,
} from '@/adapters/gateway/privateHttpTransport'
import {
  httpTransportTestDouble,
  type TestHttpBinaryResponse,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
import {
  artifactAccessUrl,
  artifactGatewayOpenUrl,
  artifactOpenFailureMessage,
  createV4ArtifactContentAccess,
  fetchArtifactBlob,
  isActiveDocumentArtifact,
  isActiveDocumentArtifactCandidate,
  openArtifactBlobUrl,
  openArtifactViaGateway,
} from '@/adapters/gateway/artifactAccessV4'

function binaryResponse(
  body: BlobPart = '',
  options: { status?: number; type?: string } = {},
): TestHttpBinaryResponse {
  const blob = new Blob([body], options.type ? { type: options.type } : undefined)
  return {
    metadata: {
      status: options.status ?? 200,
      contentLength: blob.size,
      ...(options.type ? { contentType: options.type } : {}),
    },
    blob: async () => blob,
    stream: () => blob.stream(),
  }
}

function httpTransport(requestBinary: TestHttpTransport['requestBinary']): TestHttpTransport {
  return httpTransportTestDouble({ requestBinary })
}

function successfulHttp(body: BlobPart = 'hello', type = ''): TestHttpTransport {
  return httpTransport(vi.fn(async () => binaryResponse(body, { type })))
}

function artifact(overrides: Partial<ArtifactPayload> = {}): ArtifactPayload {
  return {
    id: 'art-report',
    name: 'report.md',
    mime: 'text/markdown',
    download_url: '/api/v1/artifacts/art-report?token=old-token&sessionKey=old-session&session_key=old-session-snake',
    ...overrides,
  }
}

describe('artifactAccessUrl', () => {
  it('removes token and session query values for same-origin artifact URLs', () => {
    expect(artifactAccessUrl(artifact(), 'http://127.0.0.1:18793')).toBe('/api/v1/artifacts/art-report')
  })

  it('keeps cross-origin URLs absolute and does not rewrite their query string', () => {
    const url = artifactAccessUrl(
      artifact({ download_url: 'https://files.example.test/artifacts/art-report?token=share-token' }),
      'http://127.0.0.1:18793',
    )

    expect(url).toBe('https://files.example.test/artifacts/art-report?token=share-token')
  })

  it('builds the default artifact route from the artifact id', () => {
    expect(artifactAccessUrl(artifact({ download_url: undefined }), 'http://127.0.0.1:18793')).toBe(
      '/api/v1/artifacts/art-report',
    )
  })

  it('treats only the exact Desktop proxy authority as same-origin', () => {
    expect(artifactAccessUrl(artifact(), 'opensquilla-app://desktop')).toBe(
      '/api/v1/artifacts/art-report',
    )
    expect(artifactAccessUrl(
      artifact({ download_url: 'other-app://desktop/api/v1/artifacts/art-report?token=keep' }),
      'opensquilla-app://desktop',
    )).toBe('other-app://desktop/api/v1/artifacts/art-report?token=keep')
  })
})

describe('artifactGatewayOpenUrl', () => {
  it('builds the same-origin native-open endpoint from artifact id', () => {
    expect(artifactGatewayOpenUrl(artifact(), 'http://127.0.0.1:18793')).toBe(
      '/api/v1/artifacts/art-report/open',
    )
  })

  it('falls back to a same-origin artifact download URL when id is absent', () => {
    expect(
      artifactGatewayOpenUrl(
        artifact({
          id: undefined,
          download_url: '/api/v1/artifacts/art-from-url?sessionKey=old-session',
        }),
        'http://127.0.0.1:18793',
      ),
    ).toBe('/api/v1/artifacts/art-from-url/open')
  })

  it('does not build a native-open endpoint for cross-origin artifacts', () => {
    expect(
      artifactGatewayOpenUrl(
        artifact({ id: undefined, download_url: 'https://files.example.test/artifacts/art-report' }),
        'http://127.0.0.1:18793',
      ),
    ).toBe('')
  })
})

describe('openArtifactViaGateway', () => {
  it('posts to the owner-only native-open endpoint through the private transport', async () => {
    const requestBinary = vi.fn(async () => binaryResponse('', { status: 202 }))
    const http = httpTransport(requestBinary)

    const result = await openArtifactViaGateway(http, artifact({ name: 'page.html', mime: 'text/html' }), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
    })

    expect(result.ok).toBe(true)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/art-report/open', {
      method: 'POST',
      sessionKey: 'agent:main:webchat:ok',
      timeoutMs: 0,
    })
  })

  it('returns a failure message when native open is not authorized', async () => {
    const http = httpTransport(vi.fn(async () => {
      throw new HttpTransportError('http-status', 'Forbidden', 403, {
        code: 'OWNER_REQUIRED',
      })
    }))

    const result = await openArtifactViaGateway(http, artifact({ name: 'page.html', mime: 'text/html' }), {
      baseOrigin: 'http://127.0.0.1:18793',
    })

    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.status).toBe(403)
      expect(result.message).toBe('Artifact open is not authorized. Refresh the page and try again.')
    }
  })
})

describe('fetchArtifactBlob', () => {
  it('allows authenticated same-origin fetches through the Desktop proxy', async () => {
    const requestBinary = vi.fn(async () => binaryResponse('desktop preview', {
      type: 'text/markdown',
    }))
    const http = httpTransport(requestBinary)

    const result = await fetchArtifactBlob(http, artifact(), {
      baseOrigin: 'opensquilla-app://desktop',
      sessionKey: 'agent:main:webchat:ok',
      requireSameOrigin: true,
    })

    expect(result.ok).toBe(true)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/art-report', expect.objectContaining({
      sessionKey: 'agent:main:webchat:ok',
    }))
  })

  it('forwards an AbortSignal to the private transport', async () => {
    const controller = new AbortController()
    const requestBinary = vi.fn(async () => binaryResponse('hello'))
    const http = httpTransport(requestBinary)

    await fetchArtifactBlob(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      signal: controller.signal,
    })

    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/art-report', expect.objectContaining({
      signal: controller.signal,
    }))
  })

  it('preserves the DOM AbortError contract when the transport aborts', async () => {
    const http = httpTransport(vi.fn(async () => {
      throw new HttpTransportError('aborted', 'request aborted')
    }))

    await expect(fetchArtifactBlob(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
    })).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('fetches the sanitized artifact URL with session scope', async () => {
    const requestBinary = vi.fn(async () => binaryResponse('hello', {
      type: 'text/markdown',
    }))
    const http = httpTransport(requestBinary)

    const result = await fetchArtifactBlob(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
    })

    expect(result.ok).toBe(true)
    expect(requestBinary).toHaveBeenCalledWith('/api/v1/artifacts/art-report', {
      sessionKey: 'agent:main:webchat:ok',
      signal: undefined,
      timeoutMs: 0,
    })
    if (result.ok) {
      expect(await result.blob.text()).toBe('hello')
      expect(result.blob.type).toBe('text/markdown')
    }
  })

  it('fetches cross-origin artifacts through the credential-free private capability', async () => {
    const requestBinary = vi.fn(async () => binaryResponse('unexpected'))
    const fetchExternalArtifact = vi.fn(async () => binaryResponse('hello', {
      type: 'text/markdown',
    }))
    const http = httpTransportTestDouble({ fetchExternalArtifact, requestBinary })
    const endpoint = 'https://files.example.test/artifacts/art-report?token=share-token'

    const result = await fetchArtifactBlob(
      http,
      artifact({ download_url: endpoint }),
      {
        baseOrigin: 'http://127.0.0.1:18793',
        sessionKey: 'agent:main:webchat:ok',
      },
    )

    expect(result.ok).toBe(true)
    expect(fetchExternalArtifact).toHaveBeenCalledWith(endpoint, undefined)
    expect(requestBinary).not.toHaveBeenCalled()
  })

  it('returns a user-facing failure result when the server rejects the request', async () => {
    const http = httpTransport(vi.fn(async () => {
      throw new HttpTransportError('http-status', 'Not found', 404, { code: 'NOT_FOUND' })
    }))

    const result = await fetchArtifactBlob(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:missing',
    })

    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.status).toBe(404)
      expect(result.message).toBe('Artifact is unavailable in this session: report.md')
    }
  })
})

describe('openArtifactBlobUrl', () => {
  it('opens a blob URL created from the authenticated artifact response', async () => {
    const http = successfulHttp('hello', 'text/markdown')
    const createObjectUrl = vi.fn(() => 'blob:artifact-report')
    const revokeObjectUrl = vi.fn()
    const opened = { opener: {}, location: { href: '' }, close: vi.fn() }
    const openWindow = vi.fn(() => opened)
    const scheduleRevoke = vi.fn((_url: string, revoke: () => void) => revoke())

    const result = await openArtifactBlobUrl(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      createObjectUrl,
      revokeObjectUrl,
      openWindow,
      scheduleRevoke,
    })

    expect(result.ok).toBe(true)
    expect(createObjectUrl).toHaveBeenCalledOnce()
    expect(openWindow).toHaveBeenCalledWith('', '_blank', '')
    expect(opened.opener).toBeNull()
    expect(opened.location.href).toBe('blob:artifact-report')
    expect(scheduleRevoke).toHaveBeenCalledOnce()
    expect(scheduleRevoke.mock.calls[0][0]).toBe('blob:artifact-report')
    expect(typeof scheduleRevoke.mock.calls[0][1]).toBe('function')
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:artifact-report')
  })

  it('returns failure and revokes immediately when the browser blocks the new tab', async () => {
    const requestBinary = vi.fn(async () => binaryResponse('hello', { type: 'text/markdown' }))
    const http = httpTransport(requestBinary)
    const createObjectUrl = vi.fn(() => 'blob:artifact-report')
    const revokeObjectUrl = vi.fn()
    const openWindow = vi.fn(() => null)
    const scheduleRevoke = vi.fn()

    const result = await openArtifactBlobUrl(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      createObjectUrl,
      revokeObjectUrl,
      openWindow,
      scheduleRevoke,
    })

    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.status).toBe(0)
      expect(result.message).toBe('Artifact open failed. Use Download instead: report.md')
    }
    expect(requestBinary).not.toHaveBeenCalled()
    expect(createObjectUrl).not.toHaveBeenCalled()
    expect(revokeObjectUrl).not.toHaveBeenCalled()
    expect(scheduleRevoke).not.toHaveBeenCalled()
  })

  it('fails closed when opener isolation throws', async () => {
    const opened = {
      get opener() { return {} },
      set opener(_value: unknown) { throw new Error('nope') },
      location: { href: '' },
      close: vi.fn(),
    }
    const requestBinary = vi.fn(async () => binaryResponse('hello'))
    const http = httpTransport(requestBinary)
    const openWindow = vi.fn(() => opened)

    const result = await openArtifactBlobUrl(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      openWindow,
    })

    expect(result.ok).toBe(false)
    expect(opened.close).toHaveBeenCalledOnce()
    expect(requestBinary).not.toHaveBeenCalled()
  })

  it('fails closed when opener isolation cannot be verified', async () => {
    const opened = { opener: {}, location: { href: '' }, close: vi.fn(() => undefined) }
    Object.defineProperty(opened, 'opener', {
      configurable: true,
      get: () => ({}),
      set: () => {},
    })
    const requestBinary = vi.fn(async () => binaryResponse('hello'))
    const http = httpTransport(requestBinary)
    const openWindow = vi.fn(() => opened)

    const result = await openArtifactBlobUrl(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      openWindow,
    })

    expect(result.ok).toBe(false)
    expect(opened.close).toHaveBeenCalledOnce()
    expect(requestBinary).not.toHaveBeenCalled()
  })

  it('closes the pre-opened tab when the authenticated fetch fails', async () => {
    const opened = { opener: {}, location: { href: '' }, close: vi.fn() }
    const http = httpTransport(vi.fn(async () => {
      throw new HttpTransportError('http-status', 'Not found', 404)
    }))
    const openWindow = vi.fn(() => opened)

    const result = await openArtifactBlobUrl(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:missing',
      openWindow,
    })

    expect(result.ok).toBe(false)
    expect(opened.close).toHaveBeenCalledOnce()
    expect(opened.location.href).toBe('')
  })

  it('revokes and closes when blob navigation fails', async () => {
    const location = {
      get href() { return '' },
      set href(_value: string) { throw new Error('navigation blocked') },
    }
    const opened = { opener: {}, location, close: vi.fn() }
    const http = successfulHttp('hello', 'text/markdown')
    const createObjectUrl = vi.fn(() => 'blob:artifact-report')
    const revokeObjectUrl = vi.fn()
    const openWindow = vi.fn(() => opened)
    const scheduleRevoke = vi.fn()

    const result = await openArtifactBlobUrl(http, artifact(), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      createObjectUrl,
      revokeObjectUrl,
      openWindow,
      scheduleRevoke,
    })

    expect(result.ok).toBe(false)
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:artifact-report')
    expect(opened.close).toHaveBeenCalledOnce()
    expect(scheduleRevoke).not.toHaveBeenCalled()
  })

  it('does not open active HTML artifacts as same-origin blob documents', async () => {
    const opened = { opener: {}, location: { href: '' }, close: vi.fn() }
    const http = successfulHttp('<script>window.__x = 1</script>', 'text/html')
    const createObjectUrl = vi.fn(() => 'blob:artifact-html')
    const openWindow = vi.fn(() => opened)
    const scheduleRevoke = vi.fn()

    const result = await openArtifactBlobUrl(http, artifact({ name: 'page.html', mime: 'text/html' }), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      createObjectUrl,
      openWindow,
      scheduleRevoke,
    })

    expect(result.ok).toBe(false)
    expect(createObjectUrl).not.toHaveBeenCalled()
    expect(opened.close).toHaveBeenCalledOnce()
    expect(opened.location.href).toBe('')
    expect(scheduleRevoke).not.toHaveBeenCalled()
  })

  it('blocks active HTML artifacts even when the response content type is missing', async () => {
    const opened = { opener: {}, location: { href: '' }, close: vi.fn() }
    const http = successfulHttp('<html></html>')
    const createObjectUrl = vi.fn(() => 'blob:artifact-html')
    const openWindow = vi.fn(() => opened)

    const result = await openArtifactBlobUrl(http, artifact({ name: 'page.html', mime: '' }), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      createObjectUrl,
      openWindow,
    })

    expect(result.ok).toBe(false)
    expect(createObjectUrl).not.toHaveBeenCalled()
    expect(opened.close).toHaveBeenCalledOnce()
  })

  it.each([
    ['notes.md', 'text/markdown'],
    ['notes.txt', 'text/plain'],
    ['report.pdf', 'application/pdf'],
  ])('opens passive document artifacts: %s', async (name, mime) => {
    const opened = { opener: {}, location: { href: '' }, close: vi.fn() }
    const http = successfulHttp('hello', mime)
    const createObjectUrl = vi.fn(() => `blob:${name}`)
    const openWindow = vi.fn(() => opened)
    const scheduleRevoke = vi.fn()

    const result = await openArtifactBlobUrl(http, artifact({ name, mime }), {
      baseOrigin: 'http://127.0.0.1:18793',
      sessionKey: 'agent:main:webchat:ok',
      createObjectUrl,
      openWindow,
      scheduleRevoke,
    })

    expect(result.ok).toBe(true)
    expect(opened.location.href).toBe(`blob:${name}`)
    expect(opened.close).not.toHaveBeenCalled()
  })
})

describe('isActiveDocumentArtifact', () => {
  it.each([
    ['page.html', 'text/plain'],
    ['page.xhtml', 'text/plain'],
    ['report.txt', 'text/html'],
    ['report.txt', 'application/xhtml+xml'],
  ])('flags active document candidates before fetching: %s', (name, mime) => {
    expect(isActiveDocumentArtifactCandidate(artifact({ name, mime }))).toBe(true)
  })

  it.each([
    ['page.html', 'text/plain', 'text/plain'],
    ['report.txt', 'text/html; charset=utf-8', 'text/plain'],
    ['report.txt', 'text/plain', 'application/xhtml+xml'],
  ])('flags active document artifacts for native open guards: %s', (name, responseMime, artifactMime) => {
    expect(isActiveDocumentArtifact(
      artifact({ name, mime: artifactMime }),
      new Blob(['<html></html>'], { type: responseMime }),
    )).toBe(true)
  })

  it('allows passive document artifacts for native open guards', () => {
    expect(isActiveDocumentArtifact(
      artifact({ name: 'notes.md', mime: 'text/markdown' }),
      new Blob(['hello'], { type: 'text/markdown' }),
    )).toBe(false)
  })
})

describe('artifactOpenFailureMessage', () => {
  it('distinguishes auth, session, and network failures', () => {
    expect(artifactOpenFailureMessage(401, 'report.md')).toBe('Artifact open is not authorized. Refresh the page and try again.')
    expect(artifactOpenFailureMessage(403, 'report.md')).toBe('Artifact open is not authorized. Refresh the page and try again.')
    expect(artifactOpenFailureMessage(404, 'report.md')).toBe('Artifact is unavailable in this session: report.md')
    expect(artifactOpenFailureMessage(0, 'report.md')).toBe('Artifact open failed. Use Download instead: report.md')
  })
})

describe('createV4ArtifactContentAccess', () => {
  it('delegates preview storage cleanup to the named private capability', async () => {
    const clearPreviewOrigin = vi.fn(async () => undefined)
    const http = {
      ...successfulHttp(),
      clearPreviewOrigin,
    }
    const access = createV4ArtifactContentAccess(http)
    const previewOrigin =
      'http://p-0123456789abcdef0123456789abcdef.localhost:48721'

    await access.clearPreviewStorage(previewOrigin)

    expect(clearPreviewOrigin).toHaveBeenCalledWith(previewOrigin)
  })

  it('keeps preview storage cleanup best-effort', async () => {
    const clearPreviewOrigin = vi.fn(async () => {
      throw new HttpTransportError('network', 'offline')
    })
    const access = createV4ArtifactContentAccess({
      ...successfulHttp(),
      clearPreviewOrigin,
    })

    await expect(access.clearPreviewStorage(
      'http://p-0123456789abcdef0123456789abcdef.localhost:48721',
    )).resolves.toBeUndefined()
  })
})
