import type {
  WorkspaceDirectoryEntry,
  WorkspaceFile,
  WorkspaceFilePage,
  WorkspaceFiles,
} from '@/modules/workspaceFiles'

interface WorkspaceFileHttp {
  requestJson<T>(endpoint: string, options?:
    | { method?: 'GET'; sessionKey?: string; signal?: AbortSignal }
    | { method: 'POST'; sessionKey: string; json: { paths: string[] }; signal?: AbortSignal }
  ): Promise<T>
  requestBlob(endpoint: string, options: { sessionKey: string; signal?: AbortSignal }): Promise<Blob>
}

const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

function relativePath(value: unknown): value is string {
  return typeof value === 'string' && !!value && value.length <= 4096
    && !/[\\\x00-\x1f\x7f:]/.test(value) && !value.startsWith('/')
    && value.split('/').every(part => !!part && part !== '.' && part !== '..')
}

export function createV4WorkspaceFiles(http: WorkspaceFileHttp): WorkspaceFiles {
  return {
    async resolve(sessionKey, paths, signal) {
      if (!sessionKey || !paths.length) return []
      const raw = await http.requestJson<unknown>('/api/v1/workspace-files/resolve', {
        method: 'POST', sessionKey, json: { paths }, signal,
      })
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workspace files')
      const result = raw as Record<string, unknown>
      if (!Array.isArray(result.files) || typeof result.workspaceBinding !== 'string'
        || !result.workspaceBinding || result.workspaceBinding.length > 512) throw new Error('Invalid workspace files')
      const requested = new Set(paths)
      return result.files.map((value): WorkspaceFile => {
        const item = value as Record<string, unknown>
        if (!item || typeof item !== 'object' || typeof item.requestedPath !== 'string'
          || !requested.has(item.requestedPath) || !relativePath(item.path)
          || typeof item.name !== 'string' || item.name !== item.path.split('/').pop()
          || typeof item.mime !== 'string' || !Number.isSafeInteger(item.size) || Number(item.size) < 0
          || !['text', 'image', 'download'].includes(String(item.kind))) throw new Error('Invalid workspace file')
        return {
          requestedPath: item.requestedPath, path: item.path, name: item.name,
          mime: item.mime, size: Number(item.size), workspaceBinding: result.workspaceBinding as string,
          textPaging: item.textPaging === true,
          nativeActions: item.nativeActions === true,
          kind: item.kind === 'image' && !IMAGE_MIMES.has(item.mime) ? 'download' : item.kind as WorkspaceFile['kind'],
        }
      })
    },
    async read(sessionKey, file, signal) {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding) throw new Error('Invalid workspace file')
      // Construct the endpoint from validated identity; never navigate to model or server supplied URLs.
      const query = new URLSearchParams({ path: file.path, workspaceBinding: file.workspaceBinding })
      return http.requestBlob(`/api/v1/workspace-files/content?${query}`, { sessionKey, signal })
    },
    async readPage(sessionKey, file, startLine, endLine, signal): Promise<WorkspaceFilePage> {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding
        || !Number.isSafeInteger(startLine) || !Number.isSafeInteger(endLine)
        || startLine < 1 || endLine < startLine || endLine - startLine >= 200) {
        throw new Error('Invalid workspace file page')
      }
      const query = new URLSearchParams({
        path: file.path, workspaceBinding: file.workspaceBinding,
        startLine: String(startLine), endLine: String(endLine),
      })
      const raw = await http.requestJson<unknown>(`/api/v1/workspace-files/page?${query}`, {
        method: 'GET', sessionKey, signal,
      })
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workspace file page')
      const value = raw as Record<string, unknown>
      if (value.relativePath !== file.path || typeof value.content !== 'string'
        || !Number.isSafeInteger(value.totalLines) || Number(value.totalLines) < 1
        || value.startLine !== startLine || !Number.isSafeInteger(value.endLine)
        || Number(value.endLine) !== Math.min(endLine, Number(value.totalLines))
        || Number(value.endLine) < startLine || value.content.length > 1024 * 1024) {
        throw new Error('Invalid workspace file page')
      }
      if (value.content.includes('\u0000')) throw new Error('Invalid workspace file page')
      return { relativePath: file.path, content: value.content, totalLines: Number(value.totalLines),
        startLine, endLine: Number(value.endLine) }
    },
    async search(sessionKey, file, query, signal) {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding
        || !query.trim() || query.length > 512) throw new Error('Invalid workspace file search')
      const params = new URLSearchParams({ path: file.path, workspaceBinding: file.workspaceBinding, query })
      const raw = await http.requestJson<unknown>(`/api/v1/workspace-files/search?${params}`, {
        method: 'GET', sessionKey, signal,
      })
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workspace file search')
      const value = raw as Record<string, unknown>
      if (value.relativePath !== file.path || !Number.isSafeInteger(value.totalLines)
        || Number(value.totalLines) < 1 || (value.matchLine !== null && (
          !Number.isSafeInteger(value.matchLine) || Number(value.matchLine) < 1
          || Number(value.matchLine) > Number(value.totalLines)
        ))) throw new Error('Invalid workspace file search')
      return { relativePath: file.path, totalLines: Number(value.totalLines), matchLine: value.matchLine as number | null }
    },
    async listDir(sessionKey, path = '', cursor = '0', limit = 200, workspaceBinding = '', signal) {
      if (!sessionKey || typeof path !== 'string' || path.length > 4096
        || (path !== '' && !relativePath(path)) || !/^\d{1,12}$/.test(cursor)
        || !Number.isSafeInteger(limit) || limit < 1 || limit > 200
        || workspaceBinding.length > 512) throw new Error('Invalid workspace directory page')
      const params = new URLSearchParams({ path, cursor, limit: String(limit) })
      if (workspaceBinding) params.set('workspaceBinding', workspaceBinding)
      const raw = await http.requestJson<unknown>(`/api/v1/workspace-files/list?${params}`, {
        method: 'GET', sessionKey, signal,
      })
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workspace directory page')
      const value = raw as Record<string, unknown>
      if (typeof value.workspaceBinding !== 'string' || !value.workspaceBinding || value.workspaceBinding.length > 512
        || typeof value.relativePath !== 'string' || value.relativePath !== (path || '')
        || !Array.isArray(value.entries) || (value.nextCursor !== null
          && (typeof value.nextCursor !== 'string' || !/^\d{1,12}$/.test(value.nextCursor)))
        || typeof value.hasMore !== 'boolean' || typeof value.truncated !== 'boolean') {
        throw new Error('Invalid workspace directory page')
      }
      if (workspaceBinding && value.workspaceBinding !== workspaceBinding) throw new Error('Invalid workspace directory page')
      const entries = value.entries.map((entry): WorkspaceDirectoryEntry => {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)) throw new Error('Invalid workspace directory entry')
        const item = entry as Record<string, unknown>
        if (typeof item.name !== 'string' || !item.name || item.name.length > 4096
          || !relativePath(item.path) || item.name !== item.path.split('/').pop()
          || item.path.split('/').slice(0, -1).join('/') !== (path || '')
          || typeof item.type !== 'string'
          || !['directory', 'file'].includes(item.type)
          || typeof item.kind !== 'string' || !['directory', 'text', 'image', 'download'].includes(item.kind)
          || typeof item.mime !== 'string' || typeof item.mtimeNs !== 'string'
          || (item.size !== null && (!Number.isSafeInteger(item.size) || Number(item.size) < 0))) {
          throw new Error('Invalid workspace directory entry')
        }
        if (item.type === 'directory' && item.kind !== 'directory') throw new Error('Invalid workspace directory entry')
        if (item.type === 'file' && item.kind === 'directory') throw new Error('Invalid workspace directory entry')
        return {
          name: item.name, path: item.path, type: item.type as WorkspaceDirectoryEntry['type'],
          kind: item.kind as WorkspaceDirectoryEntry['kind'], mime: item.mime,
          size: item.size === null ? null : Number(item.size), mtimeNs: item.mtimeNs,
        }
      })
      return {
        workspaceBinding: value.workspaceBinding,
        relativePath: value.relativePath,
        entries,
        nextCursor: value.nextCursor as string | null,
        hasMore: value.hasMore,
        truncated: value.truncated,
      }
    },
  }
}
