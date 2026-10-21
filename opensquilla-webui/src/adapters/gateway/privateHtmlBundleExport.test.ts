import { afterEach, describe, expect, it, vi } from 'vitest'
import type { HtmlBundleExportTarget } from '@/modules/artifactWorkbench'
import { htmlBundleExportTarget } from '@/utils/workbench/htmlBundleExport'
import { httpBinaryResponse, httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import { createPrivateHttpTransport, HttpTransportError } from './privateHttpTransport'
import { createHtmlBundleExportAccess } from './privateHtmlBundleExport'

const zipBytes = new Uint8Array(Buffer.from('UEsDBBQAAAAIACaWTV33e7TKDgAAAA4AAAAKAAAAaW5kZXguaHRtbLPJMLTLSM3JybfRB7IAUEsBAhQAFAAAAAgAJpZNXfd7tMoOAAAADgAAAAoAAAAAAAAAAAAAAIABAAAAAGluZGV4Lmh0bWxQSwUGAAAAAAEAAQA4AAAANgAAAAAA', 'base64'))
const sessionKey = 'agent:main:webchat:zip'
const invalidArtifact: HtmlBundleExportTarget = { type: 'artifact', artifactId: 'art_saved' }
const published: HtmlBundleExportTarget = { type: 'artifact', artifactId: 'art-saved' }
afterEach(() => vi.restoreAllMocks())

describe('HTML bundle export transport', () => {
  it.each([
    ['http://127.0.0.1:18836/', published, '/api/v1/artifacts/art-saved?format=zip'],
    ['https://remote.example/control/', { type: 'document', documentId: 'doc_one', revisionId: 'rev_two' }, '/api/v1/artifact-documents/doc_one?format=zip&revisionId=rev_two'],
    ['opensquilla-app://desktop/', { type: 'working', documentId: 'doc_one', pagePath: 'pages/中文.html' }, '/api/v1/artifact-documents/doc_one/working-file?format=zip&pagePath=pages%2F%E4%B8%AD%E6%96%87.html'],
  ] as const)('downloads verified ZIP through the configured %s Gateway with authentication', async (baseUrl, identity, path) => {
    const fetch = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(zipBytes, {
      headers: { 'Content-Type': 'application/zip', 'Content-Disposition': "attachment; filename*=UTF-8''%E7%BD%91%E9%A1%B5.zip" },
    }))
    const http = createPrivateHttpTransport({ baseUrl, authToken: () => 'test-token', fetch })
    const result = await createHtmlBundleExportAccess(http).exportHtmlBundle!({ target: identity, sessionKey })
    expect(String(fetch.mock.calls[0]?.[0])).toBe(new URL(path, baseUrl).href)
    const init = fetch.mock.calls[0]?.[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('authorization')).toBe('Bearer test-token')
    expect(headers.get('x-opensquilla-session-key')).toBe(sessionKey)
    expect(init?.redirect).toBe('error')
    expect(init?.method).toBe('GET')
    expect(result.filename).toBe('网页.zip')
    expect(new Uint8Array(await result.blob.arrayBuffer())).toEqual(zipBytes)
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('never derives an endpoint from an arbitrary URL or a mutable document without a revision', async () => {
    expect(htmlBundleExportTarget({ name: 'index.html', download_url: 'https://external.invalid/index.html' })).toBeNull()
    expect(htmlBundleExportTarget({ name: 'index.html', documentId: 'doc_one', id: 'art-saved' })).toBeNull()
    const http = httpTransportTestDouble({ requestBinary: vi.fn() })
    const access = createHtmlBundleExportAccess(http)
    for (const invalid of [invalidArtifact, { type: 'artifact', artifactId: '../secret' },
      { type: 'document', documentId: 'doc_one', revisionId: '' },
      { type: 'working', documentId: 'doc_one', pagePath: '../other.html' }]) {
      await expect(access.exportHtmlBundle!({ target: invalid as HtmlBundleExportTarget, sessionKey })).rejects.toMatchObject({ code: 'unavailable' })
    }
    expect(http.requestBinary).not.toHaveBeenCalled()
  })

  it('keeps explicitly published entries immutable when they also link to a document', () => {
    const linked = { source: 'artifact', name: 'index.html', id: 'art-saved', document_id: 'doc_one' }
    expect(htmlBundleExportTarget(linked)).toEqual(published)
    expect(htmlBundleExportTarget({ ...linked, source: 'workspace-preview', documentId: 'doc_one' }))
      .toEqual({ type: 'working', documentId: 'doc_one' })
    expect(htmlBundleExportTarget({ ...linked, source: undefined, revisionId: 'rev_two' }))
      .toEqual({ type: 'document', documentId: 'doc_one', revisionId: 'rev_two' })
  })

  it.each(['text/html', 'application/json', undefined])('rejects an old Gateway response with MIME %s before buffering', async contentType => {
    const cancel = vi.fn()
    const body = { metadata: { status: 200, contentType }, stream: () => new ReadableStream({ cancel }), blob: vi.fn() }
    const http = { requestBinary: vi.fn(async () => body) }
    await expect(createHtmlBundleExportAccess(http).exportHtmlBundle!({ target: published, sessionKey })).rejects.toMatchObject({ code: 'unavailable' })
    expect(cancel).toHaveBeenCalledOnce()
    expect(body.blob).not.toHaveBeenCalled()
    expect(http.requestBinary).toHaveBeenCalledOnce()
  })

  it('rejects HTML mislabeled as ZIP and does not retry a single-file download', async () => {
    const http = httpTransportTestDouble({ requestBinary: vi.fn(async () => httpBinaryResponse('<html>old server</html>', { contentType: 'application/zip' })) })
    await expect(createHtmlBundleExportAccess(http).exportHtmlBundle!({ target: published, sessionKey })).rejects.toMatchObject({ code: 'unavailable' })
    expect(http.requestBinary).toHaveBeenCalledOnce()
  })

  it('uses a safe ZIP filename even if the response suggests an executable', async () => {
    const http = httpTransportTestDouble({ requestBinary: vi.fn(async () => httpBinaryResponse(zipBytes, { contentType: 'application/zip', filename: '../run.ps1' })) })
    expect((await createHtmlBundleExportAccess(http).exportHtmlBundle!({ target: published, sessionKey })).filename).toBe('webpage.zip')
  })

  it.each([
    [409, 'ZIP_INCOMPLETE', 'incomplete'], [409, 'ZIP_SOURCE_CHANGED', 'source-changed'], [413, 'ZIP_TOO_LARGE', 'too-large'],
    [415, 'ZIP_UNSUPPORTED', 'unavailable'], [404, '', 'unavailable'],
    [405, '', 'unavailable'], [501, '', 'unavailable'], [403, '', 'failed'],
  ])('maps Gateway failure %s/%s without fallback', async (status, code, expected) => {
    const http = httpTransportTestDouble({ requestBinary: vi.fn(async () => { throw new HttpTransportError('http-status', 'failed', Number(status), { code }) }) })
    await expect(createHtmlBundleExportAccess(http).exportHtmlBundle!({ target: published, sessionKey })).rejects.toMatchObject({ code: expected })
    expect(http.requestBinary).toHaveBeenCalledOnce()
  })

  it('does not replace an unauthorized working export with a saved version', async () => {
    const http = httpTransportTestDouble({ requestBinary: vi.fn(async () => { throw new HttpTransportError('http-status', 'forbidden', 403) }) })
    await expect(createHtmlBundleExportAccess(http).exportHtmlBundle!({ target: { type: 'working', documentId: 'doc_one' }, sessionKey }))
      .rejects.toMatchObject({ code: 'source-forbidden' })
    expect(http.requestBinary).toHaveBeenCalledOnce()
  })

  it('cancels oversized and stalled responses before saving', async () => {
    const cancel = vi.fn()
    const response = { metadata: { status: 200, contentType: 'application/zip', contentLength: 102 * 1024 * 1024 + 1 },
      stream: () => new ReadableStream<Uint8Array>({ cancel }), blob: vi.fn() }
    const access = createHtmlBundleExportAccess({ requestBinary: vi.fn(async () => response) })
    await expect(access.exportHtmlBundle!({ target: published, sessionKey })).rejects.toMatchObject({ code: 'too-large' })
    expect(cancel).toHaveBeenCalledOnce()
    response.metadata.contentLength = 4
    const controller = new AbortController()
    const pending = access.exportHtmlBundle!({ target: published, sessionKey, signal: controller.signal })
    await Promise.resolve()
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancel).toHaveBeenCalledTimes(2)
    expect(response.blob).not.toHaveBeenCalled()
  })
})
