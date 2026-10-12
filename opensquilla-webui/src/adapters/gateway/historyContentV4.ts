import type { HistoryContentReaderFactory } from '@/modules/historyContent'
import { ContentRangeCache } from '@/utils/chat/contentRangeCache'

interface ContentHttpTransport {
  requestBinary(endpoint: string, options?: {
    signal?: AbortSignal
    range?: string
    cache?: 'no-store'
  }): Promise<{
    readonly metadata: { readonly status: number; readonly contentLength?: number }
    stream(): ReadableStream<Uint8Array> | null
  }>
}

/** Content reads share Gateway authentication without buffering the HTTP body. */
export function createHistoryContentReader(http: ContentHttpTransport): HistoryContentReaderFactory {
  return () => new ContentRangeCache({
    fetcher: async (input, options) => {
      const response = await http.requestBinary(String(input), {
        signal: options?.signal ?? undefined,
        range: new Headers(options?.headers).get('Range') ?? undefined,
        cache: 'no-store',
      })
      const headers = new Headers()
      if (response.metadata.contentLength !== undefined) {
        headers.set('Content-Length', String(response.metadata.contentLength))
      }
      return new Response(response.stream(), { status: response.metadata.status, headers })
    },
  })
}
