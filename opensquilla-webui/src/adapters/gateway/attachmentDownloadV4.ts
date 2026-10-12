import type { ArtifactAccessRequest } from '@/modules/artifactWorkbench'
import { getPlatform } from '@/platform'
import type { DisplayAttachment } from '@/types/chat'
import { saveByteStream } from '@/utils/streamDownload'
import { attachmentReferenceHttpPath } from './privateArtifactHttpTransport'

interface AttachmentDownloadTransport {
  requestBinary(path: string, options: { sessionKey: string; signal?: AbortSignal; timeoutMs: number }):
    Promise<{ stream(): ReadableStream<Uint8Array> | null }>
}

/** Retained material only: no renderer-selected URLs or credentials in links. */
export async function downloadRetainedAttachment(
  http: AttachmentDownloadTransport,
  attachment: DisplayAttachment,
  request: ArtifactAccessRequest = {},
): Promise<'saved' | 'cancelled'> {
  const path = attachmentReferenceHttpPath(attachment.sha256_ref, attachment.fileSnapshotRef)
  const sessionKey = request.sessionKey?.trim()
  if (!path || !sessionKey || attachment.download_url) throw new Error('Invalid retained attachment')
  if (request.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
  const name = attachment.name?.split(/[/\\]/).pop()?.replace(/[\u0000-\u001f\u007f<>:"|?*]/g, '_') || 'content.txt'
  const platform = getPlatform()
  if (platform.id === 'desktop' && platform.files.saveAttachment && platform.gateway.getConnection) {
    const connection = await platform.gateway.getConnection()
    if (connection.status !== 'ready' || !connection.instanceId) throw new Error('Gateway unavailable')
    if (request.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    return (await platform.files.saveAttachment({ gatewayInstanceId: connection.instanceId,
      sessionKey, sha256: attachment.sha256_ref!.toLowerCase(), name })).status
  }
  return saveByteStream(name, async () => {
    const response = await http.requestBinary(path, { sessionKey, signal: request.signal, timeoutMs: 0 })
    const stream = response.stream()
    if (!stream) throw new Error('Streaming download unavailable')
    return stream
  }, { signal: request.signal, expectedBytes: attachment.size, mime: 'text/plain;charset=utf-8' })
}
