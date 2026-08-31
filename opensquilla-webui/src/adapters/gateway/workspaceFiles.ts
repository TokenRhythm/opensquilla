import {
  WorkspaceFilesError,
  type WorkspaceFileContent,
  type WorkspaceFileEntry,
  type WorkspaceFileListing,
  type WorkspaceFiles,
  type WorkspaceFilesRequestOptions,
} from '@/modules/workspaceFiles'
import { HttpTransportError } from './privateHttpTransport'

/**
 * Narrow HTTP port private to this Adapter. The composition root passes the
 * richer private transport structurally without leaking a Gateway symbol
 * through an exported declaration.
 */
export interface WorkspaceFilesHttpTransport {
  requestJson<T = unknown>(
    endpoint: string,
    options?: { method?: 'GET'; signal?: AbortSignal; timeoutMs?: number },
  ): Promise<T>
}

export interface WorkspaceFilesAdapterOptions {
  http: WorkspaceFilesHttpTransport
}

const LIST_ENDPOINT = '/api/v1/files'
const CONTENT_ENDPOINT = '/api/v1/files/content'

function filesEndpoint(
  endpoint: string,
  workspaceId: string,
  path: string,
): string {
  const query = new URLSearchParams({ workspace: workspaceId })
  if (path) query.set('path', path)
  return `${endpoint}?${query.toString()}`
}

function errorKind(status: number | undefined): WorkspaceFilesError['kind'] {
  if (status === 404) return 'not-found'
  if (status === 400) return 'invalid'
  return 'unavailable'
}

function toWorkspaceFilesError(error: unknown): WorkspaceFilesError {
  if (error instanceof WorkspaceFilesError) return error
  if (error instanceof HttpTransportError) {
    const payload = error.payload as { error?: unknown } | undefined
    const detail =
      typeof payload?.error === 'string' && payload.error
        ? payload.error
        : error.message
    return new WorkspaceFilesError(errorKind(error.status), detail, error)
  }
  return new WorkspaceFilesError('unavailable', String(error), error)
}

function normalizeEntry(raw: unknown): WorkspaceFileEntry {
  if (typeof raw !== 'object' || raw === null) {
    throw new WorkspaceFilesError('unavailable', 'Malformed file listing entry.')
  }
  const record = raw as Record<string, unknown>
  const name = typeof record.name === 'string' ? record.name : ''
  const path = typeof record.path === 'string' ? record.path : ''
  const type = record.type === 'directory' ? 'directory' : 'file'
  if (!name || !path) {
    throw new WorkspaceFilesError('unavailable', 'Malformed file listing entry.')
  }
  const entry: WorkspaceFileEntry = { name, path, type }
  if (typeof record.size === 'number') entry.size = record.size
  if (typeof record.mtime === 'number') entry.mtime = record.mtime
  return entry
}

function normalizeListing(value: unknown): WorkspaceFileListing {
  if (typeof value !== 'object' || value === null) {
    throw new WorkspaceFilesError('unavailable', 'Malformed file listing response.')
  }
  const record = value as Record<string, unknown>
  if (!Array.isArray(record.entries)) {
    throw new WorkspaceFilesError('unavailable', 'Malformed file listing response.')
  }
  return {
    path: typeof record.path === 'string' ? record.path : '',
    entries: record.entries.map(normalizeEntry),
  }
}

function normalizeContent(value: unknown): WorkspaceFileContent {
  if (typeof value !== 'object' || value === null) {
    throw new WorkspaceFilesError('unavailable', 'Malformed file content response.')
  }
  const record = value as Record<string, unknown>
  return {
    path: typeof record.path === 'string' ? record.path : '',
    size: typeof record.size === 'number' ? record.size : 0,
    binary: record.binary === true,
    truncated: record.truncated === true,
    content: typeof record.content === 'string' ? record.content : null,
  }
}

/** Gateway Adapter exposing read-only workspace file access. */
export function createWorkspaceFiles(
  options: WorkspaceFilesAdapterOptions,
): WorkspaceFiles {
  const http = options.http

  return {
    async listDir(
      workspaceId: string,
      path: string,
      requestOptions?: WorkspaceFilesRequestOptions,
    ): Promise<WorkspaceFileListing> {
      try {
        const value = await http.requestJson<unknown>(
          filesEndpoint(LIST_ENDPOINT, workspaceId, path),
          {
            method: 'GET',
            signal: requestOptions?.signal,
            timeoutMs: requestOptions?.timeoutMs,
          },
        )
        return normalizeListing(value)
      } catch (error) {
        throw toWorkspaceFilesError(error)
      }
    },

    async readFile(
      workspaceId: string,
      path: string,
      requestOptions?: WorkspaceFilesRequestOptions,
    ): Promise<WorkspaceFileContent> {
      try {
        const value = await http.requestJson<unknown>(
          filesEndpoint(CONTENT_ENDPOINT, workspaceId, path),
          {
            method: 'GET',
            signal: requestOptions?.signal,
            timeoutMs: requestOptions?.timeoutMs,
          },
        )
        return normalizeContent(value)
      } catch (error) {
        throw toWorkspaceFilesError(error)
      }
    },
  }
}
