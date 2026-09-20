import type { InjectionKey } from 'vue'
import type { WorkspaceFileReferenceV1 } from '@/types/references'

export interface WorkspaceSourceSnapshot {
  reference: WorkspaceFileReferenceV1
  relativePath: string
  revision: string
  content: string
  totalLines: number
  startLine: number
  endLine: number
}

export class WorkspaceReferenceError extends Error {
  constructor(readonly code: string) {
    super(code)
    this.name = 'WorkspaceReferenceError'
  }
}

export interface WorkspaceReferences {
  read(sessionKey: string, reference: WorkspaceFileReferenceV1, signal?: AbortSignal): Promise<WorkspaceSourceSnapshot>
}

export const WORKSPACE_REFERENCES_KEY: InjectionKey<WorkspaceReferences> = Symbol('WorkspaceReferences')

export function workspaceReferenceErrorKey(error: unknown): string {
  const code = error instanceof WorkspaceReferenceError ? error.code : ''
  switch (code) {
    case 'STALE_REFERENCE': return 'workspaceReference.stale'
    case 'WORKSPACE_MISMATCH': return 'workspaceReference.workspaceMismatch'
    case 'OWNER_REQUIRED':
    case 'FORBIDDEN': return 'workspaceReference.forbidden'
    case 'FILE_NOT_FOUND':
    case 'SESSION_NOT_FOUND': return 'workspaceReference.missing'
    case 'INVALID_REFERENCE': return 'workspaceReference.invalid'
    case 'FILE_UNAVAILABLE': return 'workspaceReference.unsupported'
    default: return 'workspaceReference.unavailable'
  }
}
