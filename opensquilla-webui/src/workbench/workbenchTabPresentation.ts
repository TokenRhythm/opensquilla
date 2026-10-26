import { artifactIsHistoricalPreview, artifactSourcePathFromWorkbenchItem } from './artifactItems'
import { workspaceFilePayload, workspaceFileReference } from './workspaceFileItems'
import type { WorkbenchItem } from './types'

export function workbenchTabSourcePath(item: WorkbenchItem): string {
  if (item.kind === 'artifact-preview') return artifactSourcePathFromWorkbenchItem(item)
  if (item.kind === 'file') {
    return workspaceFilePayload(item)?.path || workspaceFileReference(item)?.locator.relativePath || ''
  }
  return ''
}

export function workbenchTabName(item: WorkbenchItem): string {
  if (item.kind !== 'file') return item.title
  return workbenchTabSourcePath(item).split(/[\\/]/).pop() || item.title
}

/** Same names are a presentation collision, never a resource identity. */
export function workbenchTabSourceLabel(item: WorkbenchItem, items: readonly WorkbenchItem[]): string {
  const path = workbenchTabSourcePath(item)
  if (!path) return ''
  const name = workbenchTabName(item)
  const distinctSources = new Set(items.filter(other => workbenchTabName(other) === name)
    .map(workbenchTabSourcePath).filter(Boolean))
  if (distinctSources.size < 2) return ''
  const separator = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return separator > 0 ? path.slice(0, separator) : './'
}

export { artifactIsHistoricalPreview as workbenchTabIsHistorical }
