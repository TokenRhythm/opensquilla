import type { ArtifactPayload } from '@/types/artifacts'
import type { WorkbenchResourceProvider } from '@/modules/artifactWorkbench'
import type { WorkbenchResource } from '@/types/workbenchResources'
import { artifactWorkbenchPreviewKind } from './artifactPreview'

export const MARKDOWN_IMAGE_CATALOG_LIMIT = 200

/** Update only the owning placeholder; all surrounding Markdown DOM survives. */
export function applyMarkdownImageResources(root: HTMLElement, resources: ReadonlyMap<string, string>): void {
  for (const placeholder of root.querySelectorAll<HTMLElement>('[data-markdown-image]')) {
    const url = resources.get(placeholder.getAttribute('data-markdown-image') || '')
    if (!url) continue
    const existing = placeholder.querySelector<HTMLImageElement>(':scope > img')
    if (existing?.getAttribute('src') === url) continue
    const image = document.createElement('img')
    image.src = url
    image.alt = placeholder.getAttribute('data-markdown-image-alt') || ''
    image.decoding = 'async'
    image.loading = 'lazy'
    image.referrerPolicy = 'no-referrer'
    placeholder.replaceChildren(image)
  }
}

function normalizedRelativePath(source: string): string | null {
  if (!source || source.length > 4096 || /[\\\u0000-\u001f?#]/.test(source)) return null
  if (source.startsWith('/') || /^[a-z][a-z\d+.-]*:/i.test(source)) return null
  let decoded: string
  try { decoded = decodeURIComponent(source) } catch { return null }
  if (/[\\\u0000-\u001f?#]/.test(decoded) || decoded.startsWith('/') || decoded.includes(':')) return null
  const segments: string[] = []
  for (const part of decoded.split('/')) {
    if (part === '..') {
      if (!segments.length) return null
      segments.pop()
    } else if (part && part !== '.') segments.push(part)
  }
  return segments.length ? segments.join('/') : null
}

/** A single bounded catalog page is loaded only when a relative image needs it. */
export function createMarkdownImageResolver(
  provider: Pick<WorkbenchResourceProvider, 'list'>,
  artifact: () => ArtifactPayload,
  sessionKey: () => string,
) {
  let catalog: Promise<WorkbenchResource[]> | undefined
  let activeSignal: AbortSignal | undefined
  return async (source: string, signal: AbortSignal): Promise<ArtifactPayload | null> => {
    const current = artifact()
    const session = sessionKey()
    if (!session || signal.aborted || source.startsWith('/') || /^[a-z][a-z\d+.-]*:/i.test(source)) return null
    const name = normalizedRelativePath(String(current.name || ''))
    const directory = name?.includes('/') ? name.slice(0, name.lastIndexOf('/') + 1) : ''
    const target = normalizedRelativePath(directory + source)
    if (!target) return null
    if (!catalog || activeSignal !== signal) {
      activeSignal = signal
      catalog = provider.list(session, {
        types: ['deliverable'], limit: MARKDOWN_IMAGE_CATALOG_LIMIT, signal, singlePage: true,
      }).then(result => {
        // An unseen page could contain another revision with the same name.
        // Retain alt text rather than display a guessed historical image.
        if (result.hasMore || result.totalCount > result.resources.length) return []
        return result.resources.slice(0, MARKDOWN_IMAGE_CATALOG_LIMIT)
      })
    }
    const resources = await catalog
    if (signal.aborted) return null
    const matches = resources.filter(resource => resource.resource.type === 'deliverable'
      && resource.capabilities.download
      && normalizedRelativePath(resource.name) === target)
    // Do not guess which revision/name the document meant.
    if (matches.length !== 1) return null
    const resource = matches[0]!
    const payload: ArtifactPayload = {
      id: resource.resource.type === 'deliverable'
        ? resource.resource.artifactId || resource.resource.id : undefined,
      name: resource.name, mime: resource.mime, size: resource.size,
      download_url: resource.downloadUrl,
    }
    return artifactWorkbenchPreviewKind(payload) === 'image' ? payload : null
  }
}
