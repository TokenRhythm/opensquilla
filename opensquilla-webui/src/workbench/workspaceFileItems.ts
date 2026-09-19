import { normalizeWorkspaceFileReferenceV1, type WorkspaceFileReferenceV1 } from '@/types/references'
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

export function workspaceFileReference(item: WorkbenchItem): WorkspaceFileReferenceV1 | null {
  return item.kind === 'file' ? normalizeWorkspaceFileReferenceV1(item.payload.reference) : null
}
