import type { ArtifactPayload } from '@/types/artifacts'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import type { WorkbenchResource } from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import { isPreviewPagePath } from '@/utils/workbench/previewPagePath'

export interface WorkspacePreviewLink {
  callId: string
  documentId: string
  name: string
  entrypoint: string
  relativePath?: string
  bundleRoot?: string
  previewPagePath?: string
}

export function workspacePreviewFromToolCall(
  call: Pick<ChatToolCall, 'toolId' | 'name' | 'isRunning' | 'status' | 'isError' | 'result'>,
): WorkspacePreviewLink | null {
  if (call.name !== 'open_workspace_preview' || call.isRunning || call.isError
    || call.status !== 'success' || !call.toolId) return null
  let result: unknown
  try { result = JSON.parse(call.result) } catch { return null }
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null
  const record = result as Record<string, unknown>
  if (record.error || record.is_error || record.isError
    || !['ready', 'registered'].includes(String(record.previewStatus))) return null
  const documentId = record.documentId
  if (typeof documentId !== 'string' || !/^doc_[\w-]+$/.test(documentId)
    || record.resourceId !== `document:${documentId}`) return null
  if (record.open !== undefined) {
    if (!record.open || typeof record.open !== 'object' || Array.isArray(record.open)
      || (record.open as Record<string, unknown>).resourceId !== record.resourceId) return null
  }
  const entrypoint = typeof record.entrypoint === 'string' ? record.entrypoint : ''
  if (!entrypoint || /[\x00-\x1f\x7f]/.test(entrypoint)) return null
  const name = entrypoint.split(/[/\\]/).pop() || ''
  if (!/\.(html?|xhtml)$/i.test(name)) return null
  const sourcePath = entrypoint.replace(/\\/g, '/')
  const workspace = typeof record.workspace === 'string'
    ? record.workspace.replace(/\\/g, '/').replace(/\/+$/, '') : ''
  const relativePath = workspace && sourcePath.startsWith(`${workspace}/`)
    ? sourcePath.slice(workspace.length + 1) : undefined
  const bundleRoot = typeof record.bundleRoot === 'string' ? record.bundleRoot : ''
  const directory = record.bundleMode === 'directory' && relativePath
    && bundleRoot && !/[\\\x00-\x1f\x7f:%?#]/.test(bundleRoot)
    && bundleRoot.split('/').every(part => part && part !== '.' && part !== '..')
    && relativePath.startsWith(`${bundleRoot}/`)
  return { callId: call.toolId, documentId, name, entrypoint, relativePath,
    ...(directory ? { bundleRoot } : {}) }
}

export function workspacePreviewIdentity(preview: WorkspacePreviewLink): string {
  return JSON.stringify([preview.documentId, preview.previewPagePath || ''])
}

/** Expand only a server-enumerated bundle; a mention in prose never adds a page. */
export function workspacePreviewPages(
  preview: WorkspacePreviewLink,
  resource: WorkbenchResource | null | undefined,
): WorkspacePreviewLink[] {
  if (!preview.bundleRoot || !preview.relativePath || !resource?.capabilities.preview
    || resource.resource.type !== 'document'
    || workbenchResourceRefId(resource.resource) !== preview.documentId) return [preview]
  const sourcePath = preview.entrypoint.replace(/\\/g, '/')
  const workspacePrefix = sourcePath.slice(0, -preview.relativePath.length)
  const pages = new Set(resource.previewPages?.filter(isPreviewPagePath))
  const children = [...pages].flatMap(previewPagePath => {
    const relativePath = `${preview.bundleRoot}/${previewPagePath}`
    if (relativePath === preview.relativePath) return []
    return [{ ...preview, previewPagePath, relativePath,
      name: previewPagePath.split('/').pop()!, entrypoint: `${workspacePrefix}${relativePath}` }]
  })
  return [preview, ...children]
}

/** Paths are display aliases only. Opening still uses the registered Document ID. */
export function workspacePreviewForPath(
  text: string,
  previews: readonly WorkspacePreviewLink[],
): WorkspacePreviewLink | undefined {
  const path = text.replace(/\\/g, '/')
  const relative = path.replace(/^\.\//, '')
  const matches = previews.filter(preview => !path.includes('/')
    ? path === preview.name
    : path === preview.entrypoint.replace(/\\/g, '/') || relative === preview.relativePath)
  return matches.length === 1 ? matches[0] : undefined
}

export function workspacePreviewLabel(
  preview: WorkspacePreviewLink,
  previews: readonly WorkspacePreviewLink[],
): string {
  return previews.filter(item => item.name === preview.name).length > 1
    ? preview.relativePath || preview.entrypoint : preview.name
}

// Track only nodes created by this decorator. A model-authored class or data
// attribute is never evidence that a node is an authorized resource action.
const decoratedRoots = new WeakMap<HTMLElement, HTMLButtonElement[]>()

/** Upgrade complete inline-code paths in sanitized answer HTML, never tool output. */
export function decorateWorkspacePreviewLinks(
  root: HTMLElement,
  previews: readonly WorkspacePreviewLink[],
  onOpen: (preview: WorkspacePreviewLink) => void,
  labelFor: (preview: WorkspacePreviewLink) => string,
  onMenu?: (event: MouseEvent | KeyboardEvent, preview: WorkspacePreviewLink) => void,
): string[] {
  for (const button of decoratedRoots.get(root) ?? []) {
    if (root.contains(button)) button.replaceWith(...button.childNodes)
  }
  const buttons: HTMLButtonElement[] = []
  const matched = new Set<string>()
  for (const code of root.querySelectorAll('code')) {
    // The Markdown renderer marks every fenced/indented block as hljs even
    // when highlighting is disabled; never reinterpret that code as a link.
    if (code.closest('pre, a, button') || code.classList.contains('hljs')) continue
    const preview = workspacePreviewForPath(code.textContent ?? '', previews)
    if (!preview) continue
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'workspace-file-link'
    button.setAttribute('role', 'link')
    button.title = labelFor(preview)
    button.setAttribute('aria-label', button.title)
    button.addEventListener('click', event => {
      event.preventDefault()
      event.stopPropagation()
      onOpen(preview)
    })
    if (onMenu) {
      button.addEventListener('contextmenu', event => onMenu(event, preview))
      button.addEventListener('keydown', event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) onMenu(event, preview)
      })
    }
    code.replaceWith(button)
    button.appendChild(code)
    buttons.push(button)
    matched.add(workspacePreviewIdentity(preview))
  }
  decoratedRoots.set(root, buttons)
  return [...matched]
}

/** Both live and restored calls retain this link, independently of tool disclosure. */
export function workspacePreviewsFromMessage(message: ChatRenderedMessage): WorkspacePreviewLink[] {
  const calls = [
    ...(message.timelineItems ?? []).flatMap(item => item.type === 'tool-group' ? item.group.calls : []),
    ...(message.toolCalls ?? []),
  ]
  const previews = new Map<string, WorkspacePreviewLink>()
  for (const call of calls) {
    const link = workspacePreviewFromToolCall(call)
    // Keep the latest successful call identity so reopening after adding a
    // page refreshes its server inventory, without duplicating the site link.
    if (link) previews.set(link.documentId, link)
  }
  return [...previews.values()]
}

/** An open-action descriptor, not a delivery or a filesystem navigation target. */
export function workspacePreviewOpenAction(link: WorkspacePreviewLink, sessionKey?: string): ArtifactPayload {
  return {
    source: 'workspace-preview',
    documentId: link.documentId,
    name: link.name,
    mime: 'text/html',
    session_key: sessionKey,
    ...(link.previewPagePath ? { previewPagePath: link.previewPagePath } : {}),
  }
}
