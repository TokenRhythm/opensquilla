import type { WorkspaceFile, WorkspaceFiles } from '@/modules/workspaceFiles'
import type { WorkbenchItem } from './types'
import { workspaceFilePayload, workspaceFileReference, workspaceFileSessionKey } from './workspaceFileItems'

/** Revalidate the original authority before following a file's current Document. */
export async function resolveWorkspaceDocumentFile(
  files: WorkspaceFiles,
  item: WorkbenchItem,
  isCurrent: () => boolean,
): Promise<WorkspaceFile | null> {
  if (!isCurrent()) return null
  const previous = workspaceFilePayload(item)
  const reference = workspaceFileReference(item)
  const path = previous?.path || reference?.locator.relativePath
  if (!path) return null
  // A reference can outlive a workspace rebinding. Its path alone must never
  // authorize opening or preparing a different file in the replacement root.
  let matches: WorkspaceFile[]
  try {
    matches = reference
      ? await files.resolveReference?.(workspaceFileSessionKey(item), reference) || []
      : await files.resolve(workspaceFileSessionKey(item), [path])
  } catch (error) {
    if (isCurrent()) throw error
    return null
  }
  if (!isCurrent()) return null
  return matches.find(file => file.path === path
    && (!previous || file.workspaceBinding === previous.workspaceBinding)) || null
}
