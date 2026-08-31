import { describe, expect, it, vi } from 'vitest'

import { WorkspaceFilesError } from '@/modules/workspaceFiles'
import { HttpTransportError } from './privateHttpTransport'
import { createWorkspaceFiles, type WorkspaceFilesHttpTransport } from './workspaceFiles'

function harness() {
  const requestJson = vi.fn(async (
    _endpoint: string,
    _options?: { method?: 'GET'; signal?: AbortSignal; timeoutMs?: number },
  ): Promise<unknown> => {
    if (_endpoint.startsWith('/api/v1/files/content?')) {
      return {
        path: 'README.md',
        size: 11,
        binary: false,
        truncated: false,
        content: 'README root\n',
      }
    }
    return {
      path: '',
      entries: [
        { name: 'src', path: 'src', type: 'directory' },
        { name: 'README.md', path: 'README.md', type: 'file', size: 11, mtime: 123 },
      ],
    }
  })
  const http: WorkspaceFilesHttpTransport = {
    requestJson: requestJson as unknown as WorkspaceFilesHttpTransport['requestJson'],
  }
  const adapter = createWorkspaceFiles({ http })
  return { adapter, requestJson }
}

describe('workspaceFiles gateway adapter', () => {
  it('lists a directory with the workspace query parameter', async () => {
    const { adapter, requestJson } = harness()
    const listing = await adapter.listDir('ws-1', '')

    expect(requestJson).toHaveBeenCalledWith(
      '/api/v1/files?workspace=ws-1',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(listing.entries.map((entry) => entry.path)).toEqual(['src', 'README.md'])
    expect(listing.entries[1]).toMatchObject({ name: 'README.md', type: 'file', size: 11 })
  })

  it('includes the path parameter for nested directories', async () => {
    const { adapter, requestJson } = harness()

    await adapter.listDir('ws-1', 'src/lib')

    expect(requestJson).toHaveBeenCalledWith(
      '/api/v1/files?workspace=ws-1&path=src%2Flib',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('reads bounded text content', async () => {
    const { adapter, requestJson } = harness()
    const content = await adapter.readFile('ws-1', 'README.md')

    expect(requestJson).toHaveBeenCalledWith(
      '/api/v1/files/content?workspace=ws-1&path=README.md',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(content).toMatchObject({
      path: 'README.md',
      binary: false,
      truncated: false,
      content: 'README root\n',
    })
  })

  it('maps http-status failures onto domain error kinds with the gateway detail', async () => {
    const { adapter, requestJson } = harness()
    requestJson.mockImplementation(async () => {
      throw new HttpTransportError(
        'http-status',
        'Gateway HTTP request failed with status 404.',
        404,
        { error: 'path not found' },
      )
    })

    const error = await adapter.readFile('ws-1', 'missing.txt').catch(
      (caught: unknown) => caught,
    )
    expect(error).toBeInstanceOf(WorkspaceFilesError)
    expect(error).toMatchObject({ kind: 'not-found', message: 'path not found' })
  })

  it('maps non-status transport failures onto the unavailable kind', async () => {
    const { adapter, requestJson } = harness()
    requestJson.mockImplementation(async () => {
      throw new HttpTransportError('network', 'Gateway HTTP transport is unavailable.')
    })

    const error = await adapter.listDir('ws-1', '').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(WorkspaceFilesError)
    expect(error).toMatchObject({ kind: 'unavailable' })
  })

  it('rejects malformed listings with a domain error', async () => {
    const { adapter, requestJson } = harness()
    requestJson.mockImplementation(async () => ({ unexpected: true }))

    const error = await adapter.listDir('ws-1', '').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(WorkspaceFilesError)
    expect(error).toMatchObject({ kind: 'unavailable' })
  })
})
