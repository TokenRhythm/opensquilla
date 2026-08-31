import type { WorkbenchItem } from './types'

export interface WorkspaceFileRef {
  workspaceId: string
  workspaceName: string
  workspacePath: string
  path: string
  /** Monotonic per-open counter. Every createWorkspaceFileWorkbenchItem call
   *  produces a distinct payload under the same stable id, so re-opening a
   *  file that is already open flows a changed prop into the panel and
   *  retriggers its reload watch instead of being a silent no-op. */
  openNonce?: number
}

let openNonceCounter = 0

export function workspaceFileWorkbenchItemId(
  workspaceId: string,
  path: string,
): string {
  return `ws-file:${workspaceId}:${path}`
}

export function createWorkspaceFileWorkbenchItem(
  ref: WorkspaceFileRef,
): WorkbenchItem {
  return {
    id: workspaceFileWorkbenchItemId(ref.workspaceId, ref.path),
    kind: 'file',
    title: ref.path.split('/').pop() || ref.path,
    scope: { type: 'workspace', id: ref.workspaceId },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: {
      workspaceId: ref.workspaceId,
      workspaceName: ref.workspaceName,
      workspacePath: ref.workspacePath,
      path: ref.path,
      openNonce: ++openNonceCounter,
    },
  }
}

export function workspaceFileFromWorkbenchItem(
  item: WorkbenchItem,
): WorkspaceFileRef | null {
  if (item.kind !== 'file') return null
  const payload = item.payload as Record<string, unknown>
  const workspaceId = typeof payload.workspaceId === 'string' ? payload.workspaceId : ''
  const workspaceName = typeof payload.workspaceName === 'string' ? payload.workspaceName : ''
  const workspacePath = typeof payload.workspacePath === 'string' ? payload.workspacePath : ''
  const path = typeof payload.path === 'string' ? payload.path : ''
  const openNonce = typeof payload.openNonce === 'number' ? payload.openNonce : undefined
  if (!workspaceId || !path) return null
  return { workspaceId, workspaceName, workspacePath, path, openNonce }
}
