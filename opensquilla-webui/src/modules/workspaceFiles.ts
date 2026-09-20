import type { InjectionKey } from 'vue'

export interface WorkspaceFile {
  requestedPath: string
  path: string
  name: string
  mime: string
  size: number
  kind: 'text' | 'image' | 'download'
  workspaceBinding: string
}

export interface WorkspaceFiles {
  resolve(sessionKey: string, paths: string[], signal?: AbortSignal): Promise<WorkspaceFile[]>
  read(sessionKey: string, file: WorkspaceFile, signal?: AbortSignal): Promise<Blob>
}

export const WORKSPACE_FILES_KEY: InjectionKey<WorkspaceFiles> = Symbol('WorkspaceFiles')
