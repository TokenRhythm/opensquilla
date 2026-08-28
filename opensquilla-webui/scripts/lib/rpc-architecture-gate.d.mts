export interface RpcDebtLane {
  lane: string
  debt: Record<string, Record<string, number>>
}

export interface RpcArchitectureGateResult {
  failures: string[]
  total: number
  rpcTotal: number
  httpTotal: number
  debtByLane: Map<string, number>
}

export function evaluateRpcArchitectureGate(options?: {
  root?: string
  debtLanes?: RpcDebtLane[]
}): RpcArchitectureGateResult
