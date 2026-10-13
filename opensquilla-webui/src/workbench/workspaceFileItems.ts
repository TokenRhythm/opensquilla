import { normalizeWorkspaceFileReferenceV1, type WorkspaceFileReferenceV1 } from '@/types/references'
import type { WorkspaceFile } from '@/modules/workspaceFiles'
import type { WorkbenchItem } from './types'

export function createWorkspaceFileItem(sessionKey: string, reference: WorkspaceFileReferenceV1): WorkbenchItem {
  return {
    id: `file:${sessionKey}:${reference.scope.workspaceId ?? ''}:${reference.locator.relativePath}`,
    kind: 'file',
    title: reference.locator.relativePath,
    scope: { type: 'session', id: sessionKey },
    hostKind: 'dom',
    retention: 'dispose-on-suspend',
    payload: { reference },
  }
}

/**
 * Create a Workbench item from the owner-only file metadata resolved from an
 * inline workspace path. This deliberately keeps the validated relative path
 * and binding opaque to the Workbench payload; no host filesystem path is
 * exposed to the renderer.
 */
export function createResolvedWorkspaceFileItem(sessionKey: string, file: WorkspaceFile): WorkbenchItem {
  return {
    id: `file:${sessionKey}:${file.workspaceBinding}:${file.path}`,
    kind: 'file',
    title: file.path,
    scope: { type: 'session', id: sessionKey },
    hostKind: 'dom',
    retention: 'dispose-on-suspend',
    payload: { file },
  }
}

export function workspaceFileReference(item: WorkbenchItem): WorkspaceFileReferenceV1 | null {
  return item.kind === 'file' ? normalizeWorkspaceFileReferenceV1(item.payload.reference) : null
}

export function workspaceFilePayload(item: WorkbenchItem): WorkspaceFile | null {
  if (item.kind !== 'file' || !item.payload.file || typeof item.payload.file !== 'object') return null
  return item.payload.file as WorkspaceFile
}
