import type {
  WorkspaceDirectoryEntry,
  WorkspaceFile,
  WorkspaceFilePage,
  WorkspaceFiles,
  WorkspaceAppCatalogue,
  WorkspaceRootScope,
  WorkspaceRoot,
} from '@/modules/workspaceFiles'
import { WorkspaceFilesChangedError, WorkspaceTextLimitError } from '@/modules/workspaceFiles'
import type { WorkspaceFileReferenceV1 } from '@/types/references'
import { HttpTransportError } from './privateHttpTransport'

interface WorkspaceFileHttp {
  requestJson<T>(endpoint: string, options?:
    | { method?: 'GET'; sessionKey?: string; signal?: AbortSignal }
    | { method: 'POST'; sessionKey?: string; json: { paths: string[]; reference?: WorkspaceFileReferenceV1 } | { path: string; workspaceBinding: string; expectedRevision: string; appId: string } | { workspaceBinding: string; appId: string; projectId?: string }; signal?: AbortSignal }
  ): Promise<T>
  requestBlob(endpoint: string, options: { sessionKey: string; signal?: AbortSignal }): Promise<Blob>
}

const IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
const CURSOR = /^[A-Za-z0-9_.:-]{1,512}$/
function revision(value: unknown): string | undefined {
  if (value === undefined) return undefined
  if (typeof value !== 'string' || !value || value.length > 256) throw new Error('Invalid file revision')
  return value
}
function pinned(query: URLSearchParams, file: WorkspaceFile): URLSearchParams {
  if (file.revision) query.set('expectedRevision', revision(file.revision)!)
  return query
}
async function translate<T>(operation: Promise<T>): Promise<T> {
  try { return await operation } catch (error) {
    if (error instanceof HttpTransportError && error.status === 409) throw new WorkspaceFilesChangedError()
    if (error instanceof HttpTransportError && error.status === 413) {
      const payload = error.payload as Record<string, unknown> | undefined
      if (payload?.code === 'WORKSPACE_TEXT_LIMIT' && (payload.limit === 'line' || payload.limit === 'page')) {
        throw new WorkspaceTextLimitError(payload.limit)
      }
    }
    throw error
  }
}

function relativePath(value: unknown): value is string {
  return typeof value === 'string' && !!value && value.length <= 4096
    && !/[\\\x00-\x1f\x7f:]/.test(value) && !value.startsWith('/')
    && value.split('/').every(part => !!part && part !== '.' && part !== '..')
}
function annotationMetadata(item: Record<string, unknown>): Pick<WorkspaceFile, 'documentTarget' | 'annotationCapability'> {
  const result: Pick<WorkspaceFile, 'documentTarget' | 'annotationCapability'> = {}
  if (item.documentTarget !== undefined) {
    const target = item.documentTarget as Record<string, unknown> | null
    if (!target || typeof target.documentId !== 'string' || !target.documentId
      || target.documentId.length > 512 || /[\x00-\x1f\x7f]/.test(target.documentId)
      || (target.pagePath !== undefined && !relativePath(target.pagePath))) throw new Error('Invalid annotation document target')
    result.documentTarget = { documentId: target.documentId, ...(target.pagePath ? { pagePath: target.pagePath as string } : {}) }
  }
  if (item.annotationCapability !== undefined) {
    const capability = item.annotationCapability as Record<string, unknown> | null
    if (!capability || typeof capability.supported !== 'boolean'
      || (capability.reasonCode !== undefined && (typeof capability.reasonCode !== 'string' || capability.reasonCode.length > 128))) {
      throw new Error('Invalid annotation capability')
    }
    result.annotationCapability = { supported: capability.supported,
      ...(typeof capability.reasonCode === 'string' ? { reasonCode: capability.reasonCode } : {}) }
  }
  return result
}

function rootScope(scope: WorkspaceRootScope): { sessionKey?: string; projectId?: string } {
  if (!scope || typeof scope !== 'object' || Object.keys(scope).length !== 1) throw new Error('Invalid workspace scope')
  if ('projectId' in scope && typeof scope.projectId === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(scope.projectId)) {
    return { projectId: scope.projectId }
  }
  if ('sessionKey' in scope && typeof scope.sessionKey === 'string' && scope.sessionKey.trim()
    && scope.sessionKey.length <= 512 && !/[\x00-\x1f\x7f]/.test(scope.sessionKey)) return { sessionKey: scope.sessionKey }
  throw new Error('Invalid workspace scope')
}
function rootEndpoint(endpoint: string, scope: { projectId?: string }): string {
  return scope.projectId ? `${endpoint}?${new URLSearchParams({ projectId: scope.projectId })}` : endpoint
}
function rootDescriptor(raw: unknown): WorkspaceRoot {
  const root = raw as WorkspaceRoot
  if (!root || root.kind !== 'workspace-root' || typeof root.name !== 'string' || !root.name || root.name.length > 512
    || typeof root.path !== 'string' || root.path.length > 32768 || /[\x00-\x1f\x7f]/.test(root.path)
    || !(/^(?:[A-Za-z]:[\\/]|\/|\\\\)/.test(root.path))
    || typeof root.workspaceBinding !== 'string' || !/^[a-f0-9]{64}$/.test(root.workspaceBinding)) {
    throw new Error('Invalid workspace root')
  }
  return { kind: 'workspace-root', name: root.name, path: root.path, workspaceBinding: root.workspaceBinding }
}
function appCatalogue(raw: unknown): WorkspaceAppCatalogue {
  const value = raw as Record<string, unknown>
  if (!value || value.host !== 'gateway' || typeof value.available !== 'boolean' || !Array.isArray(value.apps)) throw new Error('Invalid host app catalog')
  const apps = value.apps.map(app => {
    if (!app || typeof app.id !== 'string' || !/^[a-z-]{1,32}$/.test(app.id) || typeof app.label !== 'string' || app.label.length > 120) throw new Error('Invalid host app')
    return { id: app.id, label: app.label }
  })
  return { available: value.available, host: 'gateway', apps, reason: typeof value.reason === 'string' ? value.reason : undefined }
}

export function createV4WorkspaceFiles(http: WorkspaceFileHttp): WorkspaceFiles {
  const transport = http
  http = {
    requestJson: <T>(...args: Parameters<WorkspaceFileHttp['requestJson']>) => translate(transport.requestJson<T>(...args)),
    requestBlob: (...args) => translate(transport.requestBlob(...args)),
  }
  async function resolveFiles(sessionKey: string, paths: string[], signal?: AbortSignal, reference?: WorkspaceFileReferenceV1): Promise<WorkspaceFile[]> {
    if (!sessionKey || !paths.length) return []
    const raw = await http.requestJson<unknown>('/api/v1/workspace-files/resolve', {
      method: 'POST', sessionKey, json: { paths, ...(reference ? { reference } : {}) }, signal,
    })
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('Invalid workspace files')
    const result = raw as Record<string, unknown>
    if (reference && result.referenceScopeValidated !== true) return []
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
        revision: revision(item.revision),
        ...annotationMetadata(item),
        resource: { kind: 'workspace-file', workspaceBinding: result.workspaceBinding as string, relativePath: item.path },
        kind: item.kind === 'image' && !IMAGE_MIMES.has(item.mime) ? 'download' : item.kind as WorkspaceFile['kind'],
      }
    })
  }
  return {
    async apps(sessionKey, signal) {
      const raw = await http.requestJson<unknown>('/api/v1/workspace-files/apps', { method: 'GET', sessionKey, signal })
      return appCatalogue(raw)
    },
    async root(scope, signal) {
      const target = rootScope(scope)
      return rootDescriptor(await http.requestJson<unknown>(rootEndpoint('/api/v1/workspace-files/root', target), {
        method: 'GET', sessionKey: target.sessionKey, signal,
      }))
    },
    async rootApps(scope, signal) {
      const target = rootScope(scope)
      return appCatalogue(await http.requestJson<unknown>(rootEndpoint('/api/v1/workspace-files/apps', target), {
        method: 'GET', sessionKey: target.sessionKey, signal,
      }))
    },
    async openRoot(scope, root, appId, signal) {
      const target = rootScope(scope)
      const descriptor = rootDescriptor(root)
      if (!/^[a-z-]{1,32}$/.test(appId)) throw new Error('Invalid host app request')
      const result = await http.requestJson<{ ok: boolean; host: string; appId: string }>('/api/v1/workspace-files/open-root', {
        method: 'POST', sessionKey: target.sessionKey,
        json: { workspaceBinding: descriptor.workspaceBinding, appId, ...(target.projectId ? { projectId: target.projectId } : {}) }, signal,
      })
      if (!result || result.ok !== true || result.host !== 'gateway' || result.appId !== appId) throw new Error('Could not open the workspace')
    },
    async openWith(sessionKey, file, appId, signal) {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding || !file.revision || !/^[a-z-]{1,32}$/.test(appId)) throw new Error('Invalid host app request')
      const result = await http.requestJson<{ ok: boolean; host: string; appId: string }>('/api/v1/workspace-files/open', {
        method: 'POST', sessionKey, json: { path: file.path, workspaceBinding: file.workspaceBinding, expectedRevision: file.revision, appId }, signal,
      })
      if (!result || result.ok !== true || result.host !== 'gateway' || result.appId !== appId) throw new Error('Could not open the file')
    },
    resolve: resolveFiles,
    async resolveReference(sessionKey, reference, signal) {
      if (!relativePath(reference.locator.relativePath)) throw new Error('Invalid workspace reference')
      return resolveFiles(sessionKey, [reference.locator.relativePath], signal, reference)
    },
    async read(sessionKey, file, signal) {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding) throw new Error('Invalid workspace file')
      // Construct the endpoint from validated identity; never navigate to model or server supplied URLs.
      const query = pinned(new URLSearchParams({ path: file.path, workspaceBinding: file.workspaceBinding }), file)
      return http.requestBlob(`/api/v1/workspace-files/content?${query}`, { sessionKey, signal })
    },
    async readPage(sessionKey, file, startLine, endLine, signal): Promise<WorkspaceFilePage> {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding
        || !Number.isSafeInteger(startLine) || !Number.isSafeInteger(endLine)
        || startLine < 1 || endLine < startLine || endLine - startLine >= 200) {
        throw new Error('Invalid workspace file page')
      }
      const query = pinned(new URLSearchParams({
        path: file.path, workspaceBinding: file.workspaceBinding,
        startLine: String(startLine), endLine: String(endLine),
      }), file)
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
        startLine, endLine: Number(value.endLine), revision: revision(value.revision),
        resource: { kind: 'workspace-file', workspaceBinding: file.workspaceBinding, relativePath: file.path } }
    },
    async search(sessionKey, file, query, signal) {
      if (!sessionKey || !relativePath(file.path) || !file.workspaceBinding
        || !query.trim() || query.length > 512) throw new Error('Invalid workspace file search')
      const params = pinned(new URLSearchParams({ path: file.path, workspaceBinding: file.workspaceBinding, query }), file)
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
      return { relativePath: file.path, totalLines: Number(value.totalLines), matchLine: value.matchLine as number | null, revision: revision(value.revision) }
    },
    async listDir(sessionKey, path = '', cursor = '0', limit = 200, workspaceBinding = '', signal) {
      if (!sessionKey || typeof path !== 'string' || path.length > 4096
        || (path !== '' && !relativePath(path)) || !CURSOR.test(cursor)
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
          && (typeof value.nextCursor !== 'string' || !CURSOR.test(value.nextCursor)))
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
          resource: { kind: 'workspace-file', workspaceBinding: value.workspaceBinding as string, relativePath: item.path },
        }
      })
      return {
        workspaceBinding: value.workspaceBinding,
        relativePath: value.relativePath,
        entries,
        nextCursor: value.nextCursor as string | null,
        hasMore: value.hasMore,
        truncated: value.truncated,
        directoryRevision: revision(value.directoryRevision),
      }
    },
  }
}
