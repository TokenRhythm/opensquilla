import { describe, expect, it, vi } from 'vitest'
import { createWorkingFileAccess } from './privateWorkingFileAccess'
import { HttpTransportError } from './privateHttpTransport'

const request = { sessionKey: 'agent:main:webchat:fixture', documentId: 'doc_one', pagePath: 'pages/editorial.html' }
const info = { documentId: 'doc_one', pagePath: request.pagePath, workspace: '/task',
  sourcePath: '/task/site/pages/editorial.html', name: 'editorial.html', mime: 'text/html', size: 20 }
describe('working file transport', () => {
  it('binds page, document, session and cancellation to the authenticated transport', async () => {
    const blob = new Blob([JSON.stringify(info)], { type: 'application/json' })
    const http = { requestBinary: vi.fn().mockResolvedValue({ blob: async () => blob }) }
    const content = createWorkingFileAccess(http)
    const signal = new AbortController().signal
    expect(await content.workingFileMetadata!({ ...request, signal })).toEqual(info)
    expect(http.requestBinary).toHaveBeenCalledWith(
      '/api/v1/artifact-documents/doc_one/working-file?format=metadata&pagePath=pages%2Feditorial.html',
      { sessionKey: request.sessionKey, signal })
    expect(await content.fetchWorkingFile!({ ...request, signal })).toBe(blob)
    expect(http.requestBinary.mock.lastCall![0]).toContain('format=content')
  })
  it.each([404, 405, 501])('handles missing old Gateway capability (%s) without changing sources', async status => {
    const http = { requestBinary: vi.fn().mockRejectedValue(new HttpTransportError('http-status', 'unavailable', status)) }
    expect(await createWorkingFileAccess(http).workingFileMetadata!(request)).toBeNull()
    expect(http.requestBinary).toHaveBeenCalledOnce()
  })
  it.each([{ documentId: 'doc_other' }, { pagePath: 'index.html' }, { size: -1 }])('rejects mismatched metadata %j', async patch => {
    const http = { requestBinary: vi.fn().mockResolvedValue({ blob: async () => new Blob([JSON.stringify({ ...info, ...patch })]) }) }
    await expect(createWorkingFileAccess(http).workingFileMetadata!(request)).rejects.toThrow('Invalid working file metadata')
  })
  it('does not fetch untrusted paths or hide authorization failures', async () => {
    const http = { requestBinary: vi.fn().mockRejectedValue(new HttpTransportError('http-status', 'forbidden', 403)) }
    const content = createWorkingFileAccess(http)
    await expect(content.workingFileMetadata!({ ...request, pagePath: '../secret.html' })).rejects.toThrow('Invalid working file identity')
    expect(http.requestBinary).not.toHaveBeenCalled()
    await expect(content.workingFileMetadata!(request)).rejects.toThrow('forbidden')
  })
})
