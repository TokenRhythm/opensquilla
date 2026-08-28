export const TRACKED_RPC_MEMBERS: readonly [
  'call',
  'on',
  'supportsMethod',
  'supportsEvent',
  'markMethodUnavailable',
  'waitForConnection',
]

export interface RpcTransportOperation {
  rel: string
  kind: string
}

export function collectRpcTransportOperations(input: {
  ts: typeof import('typescript')
  root: string
  sources: Array<{
    rel: string
    source: import('typescript').SourceFile
  }>
}): RpcTransportOperation[]
