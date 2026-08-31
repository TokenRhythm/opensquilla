import type { InjectionKey } from 'vue'

export type WorkspaceFilesErrorKind = 'not-found' | 'invalid' | 'unavailable'

/** Transport-independent error exposed by the WorkspaceFiles seam. */
export class WorkspaceFilesError extends Error {
  readonly name = 'WorkspaceFilesError'

  constructor(
    readonly kind: WorkspaceFilesErrorKind,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
  }
}

/** One visible child of a listed workspace directory. */
export interface WorkspaceFileEntry {
  name: string
  path: string
  type: 'file' | 'directory'
  size?: number
  mtime?: number
}

/** One bounded, gitignore-aware read of a workspace text file. */
export interface WorkspaceFileContent {
  path: string
  size: number
  binary: boolean
  truncated: boolean
  content: string | null
}

export interface WorkspaceFileListing {
  path: string
  entries: WorkspaceFileEntry[]
}

export interface WorkspaceFilesRequestOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

/**
 * Read-only workspace file access owned by the Gateway.
 *
 * Domain consumers call this seam instead of building raw HTTP requests;
 * endpoints, auth headers, and response decoding stay inside the Gateway
 * Adapter.
 */
export interface WorkspaceFiles {
  listDir(
    workspaceId: string,
    path: string,
    options?: WorkspaceFilesRequestOptions,
  ): Promise<WorkspaceFileListing>
  readFile(
    workspaceId: string,
    path: string,
    options?: WorkspaceFilesRequestOptions,
  ): Promise<WorkspaceFileContent>
}

export const WORKSPACE_FILES_KEY: InjectionKey<WorkspaceFiles> = Symbol('WorkspaceFiles')
