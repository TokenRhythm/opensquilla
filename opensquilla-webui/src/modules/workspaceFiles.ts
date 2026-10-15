import type { InjectionKey } from 'vue'

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
}

export interface WorkspaceFilePage {
  relativePath: string
  content: string
  totalLines: number
  startLine: number
  endLine: number
}

export interface WorkspaceFileSearch {
  relativePath: string
  totalLines: number
  matchLine: number | null
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
  resolve(sessionKey: string, paths: string[], signal?: AbortSignal): Promise<WorkspaceFile[]>
  read(sessionKey: string, file: WorkspaceFile, signal?: AbortSignal): Promise<Blob>
  readPage?(sessionKey: string, file: WorkspaceFile, startLine: number, endLine: number, signal?: AbortSignal): Promise<WorkspaceFilePage>
  search?(sessionKey: string, file: WorkspaceFile, query: string, signal?: AbortSignal): Promise<WorkspaceFileSearch>
  listDir?(sessionKey: string, path?: string, cursor?: string, limit?: number, workspaceBinding?: string, signal?: AbortSignal): Promise<WorkspaceDirectoryPage>
}

export const WORKSPACE_FILES_KEY: InjectionKey<WorkspaceFiles> = Symbol('WorkspaceFiles')
