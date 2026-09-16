export interface RpcArchitectureGateResult {
  failures: string[]
  /** Forbidden operations found outside their allowed transport boundary. */
  total: number
  rpcTotal: number
  httpTotal: number
}

export function evaluateRpcArchitectureGate(options?: {
  root?: string
}): RpcArchitectureGateResult
