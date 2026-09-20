import type { WorkspaceFile, WorkspaceFiles } from '@/modules/workspaceFiles'

interface WorkspaceFileHttp {
  requestJson<T>(endpoint: string, options: {
    method: 'POST'; sessionKey: string; json: { paths: string[] }; signal?: AbortSignal
  }): Promise<T>
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
  }
}
