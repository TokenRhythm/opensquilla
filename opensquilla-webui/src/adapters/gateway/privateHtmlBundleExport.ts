import { HtmlBundleExportError, type ArtifactContentAccess, type HtmlBundleExportRequest } from '@/modules/artifactWorkbench'
import { validExportIdentity } from '@/utils/workbench/htmlBundleExport'
import { isPreviewPagePath } from '@/utils/workbench/previewPagePath'
import { assertBinaryReadActive, BinaryBodyTooLargeError, readBinaryBlob, type ReadableBinaryBody } from './boundedBinaryBody'
import { HttpTransportError } from './privateHttpTransport'

// Match the server's ZIP output ceiling, including archive headers.
const MAX_HTML_ZIP_BYTES = 102 * 1024 * 1024

interface HtmlBundleTransport {
  requestBinary(endpoint: string, options?: {
    method?: 'GET' | 'POST'; sessionKey?: string; signal?: AbortSignal; timeoutMs?: number
  }): Promise<ReadableBinaryBody & {
    readonly metadata: ReadableBinaryBody['metadata'] & { readonly status: number; readonly filename?: string }
  }>
}

function endpoint(request: HtmlBundleExportRequest): string {
  if (!request.sessionKey?.trim()) throw new HtmlBundleExportError('unavailable')
  const target = request.target
  const query = new URLSearchParams({ format: 'zip' })
  if (target?.type === 'artifact' && validExportIdentity(target.artifactId, 'art-')) {
    return `/api/v1/artifacts/${encodeURIComponent(target.artifactId)}?${query}`
  }
  if ((target?.type === 'document' || target?.type === 'working')
    && validExportIdentity(target.documentId, 'doc_')) {
    const path = `/api/v1/artifact-documents/${encodeURIComponent(target.documentId)}`
    if (target.type === 'document' && validExportIdentity(target.revisionId, 'rev_')) {
      query.set('revisionId', target.revisionId)
      return `${path}?${query}`
    }
    if (target.type === 'working'
      && (target.pagePath === undefined || isPreviewPagePath(target.pagePath))) {
      if (target.pagePath !== undefined) query.set('pagePath', target.pagePath)
      return `${path}/working-file?${query}`
    }
  }
  throw new HtmlBundleExportError('unavailable')
}

function zipFilename(value?: string): string {
  const name = (value || '').split(/[/\\]/).pop()?.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, '_').trim()
  return name && name.length <= 240 && /\.zip$/i.test(name) ? name : 'webpage.zip'
}

export function createHtmlBundleExportAccess(
  http: HtmlBundleTransport,
): Pick<ArtifactContentAccess, 'exportHtmlBundle'> {
  return {
    async exportHtmlBundle(request) {
      try {
        assertBinaryReadActive(request.signal)
        const response = await http.requestBinary(endpoint(request), {
          sessionKey: request.sessionKey, signal: request.signal, timeoutMs: 0,
        })
        if (response.metadata.contentType?.split(';', 1)[0]?.trim().toLowerCase() !== 'application/zip') {
          await response.stream()?.cancel().catch(() => undefined)
          throw new HtmlBundleExportError('unavailable')
        }
        const blob = await readBinaryBlob(response, { maxBytes: MAX_HTML_ZIP_BYTES, signal: request.signal })
        const magic = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
        assertBinaryReadActive(request.signal)
        // A website export always contains an entrypoint, so require its ZIP local-file header.
        if (magic[0] !== 0x50 || magic[1] !== 0x4b || magic[2] !== 0x03 || magic[3] !== 0x04) {
          throw new HtmlBundleExportError('unavailable')
        }
        return { blob, filename: zipFilename(response.metadata.filename) }
      } catch (error) {
        assertBinaryReadActive(request.signal)
        if (error instanceof HtmlBundleExportError) throw error
        if (error instanceof BinaryBodyTooLargeError) throw new HtmlBundleExportError('too-large')
        if (error instanceof HttpTransportError) {
          if (error.status === 403 && request.target.type === 'working') throw new HtmlBundleExportError('source-forbidden')
          const code = error.payload && typeof error.payload === 'object' && 'code' in error.payload ? error.payload.code : undefined
          if (code === 'ZIP_INCOMPLETE') throw new HtmlBundleExportError('incomplete')
          if (code === 'ZIP_SOURCE_CHANGED') throw new HtmlBundleExportError('source-changed')
          if (error.status === 413 || code === 'ZIP_TOO_LARGE') throw new HtmlBundleExportError('too-large')
          if ([404, 405, 415, 501].includes(error.status || 0)) throw new HtmlBundleExportError('unavailable')
        }
        throw new HtmlBundleExportError('failed')
      }
    },
  }
}
