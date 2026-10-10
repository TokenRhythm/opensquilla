const PDF_VIEWER_ORIGIN = 'chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai'
const PDF_VIEWER_INDEX = `${PDF_VIEWER_ORIGIN}/index.html`
const PDF_EMBEDDER_CSS = `${PDF_VIEWER_ORIGIN}/pdf_embedder.css`

interface PdfFrame {
  readonly frameTreeNodeId: number
  readonly url: string
  readonly parent: PdfFrame | null
}

interface PdfRequest {
  readonly url: string
  readonly method: string
  readonly resourceType: string
  readonly webContentsId: number
  readonly frame?: PdfFrame | null
}

interface PdfResponse {
  readonly url: string
  readonly statusCode: number
  readonly resourceType: string
  readonly responseHeaders?: Record<string, string[]>
  readonly webContentsId: number
  readonly frame?: PdfFrame | null
}

interface PdfDocument {
  readonly url: string
  readonly webContentsId: number
  readonly frameTreeNodeId: number
}

interface PdfFrameTree {
  readonly frame?: { readonly url?: string; readonly urlFragment?: string; readonly mimeType?: string }
  readonly childFrames?: readonly PdfFrameTree[]
}

const PDF_VIEWER_RESOURCE_TYPES = new Set([
  'script', 'stylesheet', 'image', 'font', 'xhr', 'other',
])

function httpUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return (url.protocol === 'http:' || url.protocol === 'https:')
      && !url.username && !url.password
  } catch {
    return false
  }
}

function dynamicUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'data:' || url.protocol === 'blob:' && httpUrl(url.pathname)
  } catch { return false }
}

function documentForViewerRequest(details: PdfRequest): PdfFrame | undefined {
  const frame = details.frame
  if (!frame || details.method !== 'GET' || !resourceOriginAllowed(details.url)) return
  if (details.url === PDF_EMBEDDER_CSS && details.resourceType === 'stylesheet') return frame
  if (details.url === PDF_VIEWER_INDEX && details.resourceType === 'subFrame'
    && frame.url === 'about:blank') return frame.parent ?? undefined
  if (frame.url === PDF_VIEWER_INDEX && PDF_VIEWER_RESOURCE_TYPES.has(details.resourceType)) {
    return frame.parent ?? undefined
  }
}

function headerValues(headers: PdfResponse['responseHeaders'], name: string): string[] {
  const entry = Object.entries(headers ?? {}).find(([key]) => key.toLowerCase() === name)
  return entry?.[1] ?? []
}

function pdfResponse(details: PdfResponse): boolean {
  if (!httpUrl(details.url) || details.statusCode < 200 || details.statusCode >= 300
    || (details.resourceType !== 'mainFrame' && details.resourceType !== 'subFrame')) return false
  if (!headerValues(details.responseHeaders, 'content-type').some(value =>
    /^\s*application\/pdf\s*(?:;|$)/i.test(value))) return false
  return !headerValues(details.responseHeaders, 'content-disposition').some(value =>
    /^\s*attachment\s*(?:;|$)/i.test(value))
}

function resourceOriginAllowed(url: string): boolean {
  try {
    const parsed = new URL(url)
    return (
      (parsed.protocol === 'chrome-extension:'
        && parsed.hostname === 'mhjfbmdgcfjbbpaeojofohoefgiehjai')
      || (parsed.protocol === 'chrome:' && parsed.hostname === 'resources')
    ) && !parsed.port && !parsed.username && !parsed.password
      && !parsed.search && !parsed.hash
  } catch {
    return false
  }
}

/**
 * Grants only the Chromium built-in PDF viewer resources belonging to a PDF
 * response in this WebContents. Ordinary web pages cannot create this grant.
 */
export class NativeWorkbenchPdfResourcePolicy {
  private readonly documents = new Map<number, Map<number, PdfDocument>>()

  observeResponse(details: PdfResponse): void {
    if (!pdfResponse(details) || !details.frame
      || !Number.isSafeInteger(details.webContentsId)
      || !Number.isSafeInteger(details.frame.frameTreeNodeId)) return
    this.rememberDocument(details.webContentsId, details.frame, details.url)
  }

  private rememberDocument(webContentsId: number, frame: PdfFrame, url = frame.url): void {
    let frames = this.documents.get(webContentsId)
    if (!frames) {
      frames = new Map()
      this.documents.set(webContentsId, frames)
    }
    // A page can continually create and remove PDF iframes. Keep the trust
    // record bounded even when Chromium has already destroyed older frames.
    frames.delete(frame.frameTreeNodeId)
    if (frames.size >= 64) frames.delete(frames.keys().next().value!)
    frames.set(frame.frameTreeNodeId, {
      url,
      webContentsId,
      frameTreeNodeId: frame.frameTreeNodeId,
    })
  }

  needsFrameTree(details: PdfRequest): boolean {
    const document = documentForViewerRequest(details)
    return Boolean(document && dynamicUrl(document.url)
      && !this.isViewerResourceRequest(details))
  }

  /** Accept only canonical Chromium MIME evidence from Page.getFrameTree. */
  observeFrameTree(details: PdfRequest, result: { frameTree?: PdfFrameTree }): void {
    const document = documentForViewerRequest(details)
    if (!document || !dynamicUrl(document.url) || !Number.isSafeInteger(details.webContentsId)
      || !Number.isSafeInteger(document.frameTreeNodeId)) return
    const lineage: string[] = []
    for (let frame: PdfFrame | null = document; frame; frame = frame.parent) {
      if (lineage.length >= 64) return
      lineage.unshift(frame.url)
    }
    let remaining = 1024
    const matches: string[] = []
    const visit = (node: PdfFrameTree, ancestors: string[]): void => {
      if (--remaining < 0 || ancestors.length >= 64 || !node.frame?.url) return
      const urls = [...ancestors, node.frame.url + (node.frame.urlFragment ?? '')]
      if (urls.length === lineage.length && urls.every((url, index) => url === lineage[index])) {
        matches.push(node.frame.mimeType ?? '')
      }
      for (const child of node.childFrames ?? []) {
        if (remaining < 0) break
        visit(child, urls)
      }
    }
    if (result.frameTree) visit(result.frameTree, [])
    // Blob addresses identify immutable content; identical data addresses do
    // too. Multiple copies are admissible only if every matching document is
    // canonical PDF. Never select the first matching URL from an ambiguous tree.
    if (remaining >= 0 && matches.length && matches.every(mime => mime === 'application/pdf')) {
      this.rememberDocument(details.webContentsId, document)
    }
  }

  /** Invoke on every onBeforeRequest before applying the normal network policy. */
  requestAllowed(details: PdfRequest): boolean {
    const frame = details.frame
    if (!frame || !Number.isSafeInteger(details.webContentsId)) return false
    if (details.resourceType === 'mainFrame' && (httpUrl(details.url) || dynamicUrl(details.url))) {
      this.forgetWebContents(details.webContentsId)
    } else if (details.resourceType === 'subFrame' && (httpUrl(details.url) || dynamicUrl(details.url))) {
      this.documents.get(details.webContentsId)?.delete(frame.frameTreeNodeId)
    }
    return this.isViewerResourceRequest(details)
  }

  /** Pure check for a viewer request; also suitable for onErrorOccurred. */
  isViewerResourceRequest(details: PdfRequest): boolean {
    const frame = details.frame
    if (!frame || !Number.isSafeInteger(details.webContentsId)) return false
    if (details.method !== 'GET' || !resourceOriginAllowed(details.url)) return false
    const documents = this.documents.get(details.webContentsId)
    if (!documents) return false

    const ownDocument = documents.get(frame.frameTreeNodeId)
    if (details.url === PDF_EMBEDDER_CSS && details.resourceType === 'stylesheet'
      && ownDocument?.url === frame.url) return true

    const parent = frame.parent
    const parentDocument = parent && documents.get(parent.frameTreeNodeId)
    if (!parentDocument || parentDocument.url !== parent?.url) return false
    if (details.url === PDF_VIEWER_INDEX && details.resourceType === 'subFrame'
      && frame.url === 'about:blank') return true
    return frame.url === PDF_VIEWER_INDEX
      && PDF_VIEWER_RESOURCE_TYPES.has(details.resourceType)
  }

  forgetWebContents(webContentsId: number): void {
    this.documents.delete(webContentsId)
  }

  clear(): void {
    this.documents.clear()
  }
}
