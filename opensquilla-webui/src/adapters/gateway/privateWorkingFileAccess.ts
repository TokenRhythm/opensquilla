import type { ArtifactContentAccess, WorkingFileRequest, WorkingFileMetadata } from '@/modules/artifactWorkbench'
import { isPreviewPagePath } from '@/utils/workbench/previewPagePath'
import { HttpTransportError } from './privateHttpTransport'

interface WorkingFileTransport {
  requestBinary(endpoint: string, options: {
    sessionKey: string; signal?: AbortSignal; timeoutMs?: number
  }): Promise<{ blob(): Promise<Blob> }>
}

function endpoint(request: WorkingFileRequest, format: 'content' | 'metadata'): string {
  if (!request.sessionKey || !/^doc_[\w-]+$/.test(request.documentId)
    || (request.pagePath !== undefined && !isPreviewPagePath(request.pagePath))) {
    throw new Error('Invalid working file identity')
  }
  const query = new URLSearchParams({ format })
  if (request.pagePath !== undefined) query.set('pagePath', request.pagePath)
  return `/api/v1/artifact-documents/${encodeURIComponent(request.documentId)}/working-file?${query}`
}

export function createWorkingFileAccess(
  http: WorkingFileTransport,
): Pick<ArtifactContentAccess, 'workingFileMetadata' | 'fetchWorkingFile'> {
  return {
    async workingFileMetadata(request) {
      try {
        const response = await http.requestBinary(endpoint(request, 'metadata'), {
          sessionKey: request.sessionKey, signal: request.signal,
        })
        const blob = await response.blob()
        if (blob.size > 64 * 1024) throw new Error('Invalid working file metadata')
        const data: WorkingFileMetadata = JSON.parse(await blob.text())
        if (data.documentId !== request.documentId || !isPreviewPagePath(data.pagePath)
          || (request.pagePath !== undefined && data.pagePath !== request.pagePath)
          || typeof data.sourcePath !== 'string' || typeof data.workspace !== 'string'
          || typeof data.name !== 'string' || typeof data.mime !== 'string'
          || !Number.isSafeInteger(data.size) || data.size < 0) {
          throw new Error('Invalid working file metadata')
        }
        return data
      } catch (error) {
        if (error instanceof HttpTransportError && [404, 405, 501].includes(error.status || 0)) return null
        throw error
      }
    },
    async fetchWorkingFile(request) {
      const response = await http.requestBinary(endpoint(request, 'content'), {
        sessionKey: request.sessionKey, signal: request.signal, timeoutMs: 0,
      })
      return response.blob()
    },
  }
}
