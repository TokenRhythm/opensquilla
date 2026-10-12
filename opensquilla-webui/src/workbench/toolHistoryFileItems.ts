import { getActivePinia } from 'pinia'
import { useAppStore } from '@/stores/app'
import { isOmittedActivityPayload, type ActivityToolCodeView } from '@/utils/chat/activityToolDetails'
import { boundedTextPreview } from '@/utils/chat/toolContentPreview'
import type { ToolFileSnapshot } from '@/types/chat'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import { downloadText } from '@/utils/browser'
import { useWorkbenchStore } from './store'
import type { WorkbenchItem } from './types'

export type ToolHistoryFileView = ActivityToolCodeView & {
  kind: 'read' | 'write' | 'edit'
  path: string
  content: string
}

export interface ToolHistoryFileOptions {
  sessionKey: string
  toolId: string
  recordKey?: string
  title?: string
  fileSnapshot?: ToolFileSnapshot
}

/** Download the recorded source, never a current workspace path or a bounded preview. */
export async function downloadToolHistoryContent(
  view: ActivityToolCodeView,
  options: Pick<ToolHistoryFileOptions, 'sessionKey' | 'fileSnapshot'>,
  access?: ArtifactContentAccess,
): Promise<void> {
  const snapshot = options.fileSnapshot
  const name = view.path?.replace(/\\/g, '/').split('/').pop() || 'tool-output.txt'
  const filename = view.kind === 'edit' ? `${name}.diff` : name
  const sha256 = snapshot?.sha256_ref || snapshot?.legacy_ref?.sha256
  if (sha256) {
    if (!access?.downloadAttachment) throw new Error('Recorded content download is unavailable')
    await access.downloadAttachment({ kind: 'file', displayId: sha256, renderKey: sha256,
      name: filename, mime: 'text/plain', size: snapshot?.bytes, sha256_ref: sha256,
      ...(snapshot?.legacy_ref ? { fileSnapshotRef: { messageId: snapshot.legacy_ref.message_id, toolUseId: snapshot.legacy_ref.tool_use_id } } : {}) },
    { sessionKey: options.sessionKey })
    return
  }
  downloadText(filename, 'text/plain;charset=utf-8', snapshot?.content ?? view.content ?? '')
}

/** A recorded operation is not a reference to the current workspace file. */
export function canOpenToolHistoryFile(view?: ActivityToolCodeView | null): view is ToolHistoryFileView {
  return Boolean(view && ['read', 'write', 'edit'].includes(view.kind)
    && typeof view.path === 'string' && view.path.trim()
    && typeof view.content === 'string'
    && !isOmittedActivityPayload(view.content)
    && !view.unavailable && !view.error
    && (view.kind !== 'edit' || view.content.trim()))
}

export function createToolHistoryFileItem(
  view: ActivityToolCodeView,
  options: ToolHistoryFileOptions,
): WorkbenchItem | null {
  if (!canOpenToolHistoryFile(view) || !options.sessionKey.trim() || !options.toolId.trim()) return null
  const preview = boundedTextPreview(view.content)
  const snapshot: ToolHistoryFileView = {
    kind: view.kind,
    path: view.path,
    content: preview.content,
    ...(view.language ? { language: view.language } : {}),
    ...(view.partial ? { partial: true } : {}),
    ...(view.truncated || preview.truncated ? { truncated: true } : {}),
  }
  return {
    // The tuple preserves distinct calls to the same path and has no ambiguous
    // separator boundary. This DOM-only identity is never a native surface ID.
    id: `tool-file-history:${JSON.stringify([options.sessionKey, options.recordKey || options.toolId, view.kind])}`,
    kind: 'tool-file-history',
    title: view.path.replace(/\\/g, '/').split('/').pop() || options.title || view.path,
    scope: { type: 'session', id: options.sessionKey },
    hostKind: 'dom',
    retention: 'dispose-on-suspend',
    payload: { snapshot, toolId: options.toolId,
      ...(options.fileSnapshot ? { fileSnapshot: { ...options.fileSnapshot } } : {}),
      ...(preview.truncated ? { fullContent: view.content } : {}) },
  }
}

export function toolHistoryFileFromItem(item: WorkbenchItem): ToolHistoryFileView | null {
  if (item.kind !== 'tool-file-history' || item.scope.type !== 'session'
    || !item.scope.id || typeof item.payload.toolId !== 'string' || !item.payload.toolId) return null
  const snapshot = item.payload.snapshot
  if (!snapshot || typeof snapshot !== 'object') return null
  const view = snapshot as ActivityToolCodeView
  return canOpenToolHistoryFile(view) ? view : null
}

export function openToolHistoryFile(view: ActivityToolCodeView, options: ToolHistoryFileOptions): boolean {
  const item = createToolHistoryFileItem(view, options)
  const pinia = getActivePinia()
  if (!item || !pinia || useAppStore(pinia).features.artifactWorkbench !== true) return false
  const store = useWorkbenchStore(pinia)
  if (store.activeSessionId && store.activeSessionId !== options.sessionKey) return false
  return store.openItem(item)
}
