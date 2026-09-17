export type HttpDebtKind =
  | 'httpRequest'
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
  root?: string
  sources: readonly HttpBoundarySource[]
  analysis?: any
}): Array<{ rel: string; kind: HttpDebtKind }>
