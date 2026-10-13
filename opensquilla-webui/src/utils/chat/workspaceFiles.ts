import type { WorkspaceFile } from '@/modules/workspaceFiles'
import { getIconSvg } from '@/utils/icons'

/** A complete path is a candidate, never proof that the file exists or is accessible. */
export function workspaceFilePath(value: string, encoded = false): string | null {
  let path = value.trim()
  if (encoded) {
    try { path = decodeURIComponent(path) } catch { return null }
  }
  if (!path || path.length > 4096 || /[\x00-\x1f\x7f<>"|?*#]/.test(path)
    || /^(?:\/\/|\\\\)/.test(path)
    || (/^[A-Za-z][\w+.-]*:/.test(path) && !/^[A-Za-z]:[\\/]/.test(path))
    || !/\.[\p{L}\p{N}]{1,16}$/u.test(path)) return null
  return path
}

type Decoration = { container: HTMLElement; original: Element }
const decorated = new WeakMap<HTMLElement, Decoration[]>()

export function clearWorkspaceFileLinks(root: HTMLElement): void {
  for (const { container, original } of decorated.get(root) ?? []) {
    if (root.contains(container)) container.replaceWith(original)
  }
  decorated.delete(root)
}

function candidates(root: HTMLElement): { element: Element; path: string }[] {
  return Array.from(root.querySelectorAll('code, a[data-workspace-path]')).flatMap(element => {
    if (element.closest('pre, button') || element.classList.contains('hljs')
      || (element.tagName === 'CODE' && element.closest('a'))) return []
    const path = workspaceFilePath(element.tagName === 'A'
      ? element.getAttribute('data-workspace-path') ?? '' : element.textContent ?? '')
    return path ? [{ element, path }] : []
  })
}

export function workspaceFileCandidates(root: HTMLElement): string[] {
  return [...new Set(candidates(root).map(item => item.path))]
}

export function decorateWorkspaceFileLinks(
  root: HTMLElement,
  files: readonly WorkspaceFile[],
  onOpen: (file: WorkspaceFile) => void,
  labelFor: (file: WorkspaceFile) => string,
  onMenu?: (event: MouseEvent | KeyboardEvent, file: WorkspaceFile) => void,
  menuLabelFor?: (file: WorkspaceFile) => string,
): void {
  clearWorkspaceFileLinks(root)
  const items: Decoration[] = []
  const byPath = new Map(files.map(file => [file.requestedPath, file]))
  for (const { element, path } of candidates(root)) {
    const file = byPath.get(path)
    if (!file) continue
    const container = document.createElement('span')
    container.className = 'workspace-file-entry'
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'workspace-file-link'
    button.setAttribute('role', 'link')
    button.title = labelFor(file)
    button.setAttribute('aria-label', button.title)
    if (element.tagName === 'CODE') {
      button.appendChild(element.cloneNode(true))
    } else button.textContent = element.textContent
    button.addEventListener('click', event => {
      event.preventDefault()
      event.stopPropagation()
      onOpen(file)
    })
    container.appendChild(button)
    if (onMenu) {
      const actionButton = document.createElement('button')
      actionButton.type = 'button'
      actionButton.className = 'workspace-file-action-trigger'
      actionButton.innerHTML = getIconSvg('moreHorizontal', 14)
      actionButton.title = menuLabelFor?.(file) || labelFor(file)
      actionButton.setAttribute('aria-label', actionButton.title)
      actionButton.setAttribute('aria-haspopup', 'menu')
      actionButton.addEventListener('click', event => onMenu(event, file))
      actionButton.addEventListener('contextmenu', event => onMenu(event, file))
      actionButton.addEventListener('keydown', event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) onMenu(event, file)
      })
      container.appendChild(actionButton)
      button.addEventListener('contextmenu', event => onMenu(event, file))
      button.addEventListener('keydown', event => {
        if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) onMenu(event, file)
      })
    }
    element.replaceWith(container)
    items.push({ container, original: element })
  }
  decorated.set(root, items)
}
