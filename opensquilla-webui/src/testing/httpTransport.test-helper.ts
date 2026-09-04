import type { createV4ArtifactWorkbench } from '@/adapters/gateway/artifactWorkbenchV4'

export type TestHttpTransport = Parameters<typeof createV4ArtifactWorkbench>[2]
export type TestHttpBinaryResponse = Awaited<
  ReturnType<TestHttpTransport['requestBinary']>
>

interface TestHttpTransportOverrides {
  clearPreviewOrigin?: TestHttpTransport['clearPreviewOrigin']
  fetchExternalArtifact?: TestHttpTransport['fetchExternalArtifact']
  requestBinary?: TestHttpTransport['requestBinary']
  requestBlob?: TestHttpTransport['requestBlob']
  requestJson?: (endpoint: string, options?: unknown) => Promise<unknown>
}

function unexpected(kind: string): never {
  throw new Error(`Unexpected ${kind} HTTP request in test`)
}

/** Build a structured binary reply without exposing a native Response to consumers. */
export function httpBinaryResponse(
  body: BlobPart | Blob,
  options: {
    contentLength?: number
    contentType?: string
    filename?: string
    status?: number
  } = {},
): TestHttpBinaryResponse {
  const blob = body instanceof Blob
    ? body
    : new Blob([body], options.contentType ? { type: options.contentType } : undefined)
  return {
    metadata: {
      status: options.status ?? 200,
      contentLength: options.contentLength ?? blob.size,
      contentType: options.contentType || blob.type || undefined,
      filename: options.filename,
    },
    blob: async () => blob,
    stream: () => null,
  } as TestHttpBinaryResponse
}

/** Build a narrow transport double; each unconfigured lane fails closed. */
export function httpTransportTestDouble(
  overrides: TestHttpTransportOverrides = {},
): TestHttpTransport {
  const requestJson = overrides.requestJson
    ?? (async () => unexpected('JSON'))
  return {
    clearPreviewOrigin: overrides.clearPreviewOrigin ?? (async () => unexpected('preview cleanup')),
    fetchExternalArtifact: overrides.fetchExternalArtifact ?? (async () => unexpected('external artifact')),
    requestBinary: overrides.requestBinary ?? (async () => unexpected('binary')),
    requestBlob: overrides.requestBlob ?? (async () => unexpected('blob')),
    requestJson: requestJson as TestHttpTransport['requestJson'],
  }
}
