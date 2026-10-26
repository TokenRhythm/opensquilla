import type { InjectionKey } from 'vue'
import type { WorkspaceFileReferenceV1 } from '@/types/references'

/** Stable location within one authorized workspace; revision identifies content. */
export interface WorkspaceFileResource {
  kind: 'workspace-file'
  workspaceBinding: string
  relativePath: string
}
export class WorkspaceFilesChangedError extends Error {
  constructor() { super('The file or directory changed. Reload to see the current version.'); this.name = 'WorkspaceFilesChangedError' }
}
export class WorkspaceTextLimitError extends Error {
  constructor(readonly limit: 'line' | 'page') {
    super('Text preview limit exceeded')
    this.name = 'WorkspaceTextLimitError'
  }
}

export interface WorkspaceFile {
  requestedPath: string
  path: string
  name: string
  mime: string
  size: number
  kind: 'text' | 'image' | 'download'
  workspaceBinding: string
  /** Explicit Gateway capabilities; absent on older Gateways. */
  textPaging?: boolean
  nativeActions?: boolean
  revision?: string
  resource?: WorkspaceFileResource
  documentTarget?: { documentId: string; pagePath?: string }
  annotationCapability?: { supported: boolean; reasonCode?: string }
}

export interface WorkspaceFilePage {
  relativePath: string
  content: string
  totalLines: number
  startLine: number
  endLine: number
  revision?: string
  resource?: WorkspaceFileResource
}

export interface WorkspaceFileSearch {
  relativePath: string
  totalLines: number
  matchLine: number | null
  revision?: string
}

export interface WorkspaceDirectoryEntry {
  name: string
  path: string
  type: 'directory' | 'file'
  kind: 'directory' | 'text' | 'image' | 'download'
  mime: string
  size: number | null
  mtimeNs: string
  revision?: string
  resource?: WorkspaceFileResource
}

export interface WorkspaceDirectoryPage {
  workspaceBinding: string
  relativePath: string
  entries: WorkspaceDirectoryEntry[]
  nextCursor: string | null
  hasMore: boolean
  truncated: boolean
  directoryRevision?: string
}

/** A root is resolved from trusted project/session authority, never a client path. */
export type WorkspaceRootScope = { sessionKey: string; projectId?: never } | { projectId: string; sessionKey?: never }
export interface WorkspaceRoot {
  kind: 'workspace-root'
  name: string
  /** Display/copy only. Launch requests carry the binding, not this host path. */
  path: string
  workspaceBinding: string
}
export interface WorkspaceAppCatalogue {
  available: boolean
  host: 'gateway'
  apps: Array<{ id: string; label: string }>
  reason?: string
}

export interface WorkspaceDirectoryEntry {
  name: string
  path: string
  type: 'directory' | 'file'
  kind: 'directory' | 'text' | 'image' | 'download'
  mime: string
  size: number | null
  mtimeNs: string
}

export interface WorkspaceDirectoryPage {
  workspaceBinding: string
  relativePath: string
  entries: WorkspaceDirectoryEntry[]
  nextCursor: string | null
  hasMore: boolean
  truncated: boolean
}

export interface WorkspaceFiles {
  apps?(sessionKey: string, signal?: AbortSignal): Promise<WorkspaceAppCatalogue>
  openWith?(sessionKey: string, file: WorkspaceFile, appId: string, signal?: AbortSignal): Promise<void>
  root?(scope: WorkspaceRootScope, signal?: AbortSignal): Promise<WorkspaceRoot>
  rootApps?(scope: WorkspaceRootScope, signal?: AbortSignal): Promise<WorkspaceAppCatalogue>
  openRoot?(scope: WorkspaceRootScope, root: WorkspaceRoot, appId: string, signal?: AbortSignal): Promise<void>
  resolve(sessionKey: string, paths: string[], signal?: AbortSignal): Promise<WorkspaceFile[]>
  /** Resolve current file metadata after validating the reference workspace scope. */
  resolveReference?(sessionKey: string, reference: WorkspaceFileReferenceV1, signal?: AbortSignal): Promise<WorkspaceFile[]>
  read(sessionKey: string, file: WorkspaceFile, signal?: AbortSignal): Promise<Blob>
  readPage?(sessionKey: string, file: WorkspaceFile, startLine: number, endLine: number, signal?: AbortSignal): Promise<WorkspaceFilePage>
  search?(sessionKey: string, file: WorkspaceFile, query: string, signal?: AbortSignal): Promise<WorkspaceFileSearch>
  listDir?(sessionKey: string, path?: string, cursor?: string, limit?: number, workspaceBinding?: string, signal?: AbortSignal): Promise<WorkspaceDirectoryPage>
}

export const WORKSPACE_FILES_KEY: InjectionKey<WorkspaceFiles> = Symbol('WorkspaceFiles')
