export type HttpDebtKind =
  | 'httpApiEndpoint'
  | 'httpAuthToken'
  | 'httpAuthorizationHeader'
  | 'httpSessionKeyHeader'

export const TRACKED_HTTP_KINDS: readonly HttpDebtKind[]

export interface HttpBoundarySource {
  rel: string
  source: any
}

export function collectHttpBoundaryOperations(input: {
  ts: any
  sources: readonly HttpBoundarySource[]
}): Array<{ rel: string; kind: HttpDebtKind }>
