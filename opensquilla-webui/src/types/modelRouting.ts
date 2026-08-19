export const MODEL_ROUTING_MODES = ['off', 'squilla_router', 'llm_ensemble'] as const

export type ModelRoutingMode = (typeof MODEL_ROUTING_MODES)[number]
export type ImageInputAdmission = 'allowed' | 'blocked' | 'unknown'

/** Canonical mode names used by the Gateway and persisted session state. */
export const GATEWAY_MODEL_ROUTING_MODES = ['direct', 'router', 'ensemble'] as const

export type GatewayModelRoutingMode = (typeof GATEWAY_MODEL_ROUTING_MODES)[number]

export function isModelRoutingMode(value: unknown): value is ModelRoutingMode {
  return typeof value === 'string' && MODEL_ROUTING_MODES.includes(value as ModelRoutingMode)
}

export function normalizeModelRoutingMode(value: unknown): ModelRoutingMode {
  return isModelRoutingMode(value) ? value : 'off'
}

export function isGatewayModelRoutingMode(value: unknown): value is GatewayModelRoutingMode {
  return typeof value === 'string'
    && GATEWAY_MODEL_ROUTING_MODES.includes(value as GatewayModelRoutingMode)
}

export function modelRoutingModeToGateway(mode: ModelRoutingMode): GatewayModelRoutingMode {
  if (mode === 'squilla_router') return 'router'
  if (mode === 'llm_ensemble') return 'ensemble'
  return 'direct'
}

export function gatewayModelRoutingModeToUi(mode: unknown): ModelRoutingMode | null {
  if (mode === 'router' || mode === 'squilla_router') return 'squilla_router'
  if (mode === 'ensemble' || mode === 'llm_ensemble') return 'llm_ensemble'
  if (mode === 'direct' || mode === 'off') return 'off'
  return null
}
