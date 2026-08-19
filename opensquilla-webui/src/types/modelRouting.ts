export const MODEL_ROUTING_MODES = ['off', 'squilla_router', 'llm_ensemble'] as const

export type ModelRoutingMode = (typeof MODEL_ROUTING_MODES)[number]
export type ImageInputAdmission = 'allowed' | 'blocked' | 'unknown'

export function isModelRoutingMode(value: unknown): value is ModelRoutingMode {
  return typeof value === 'string' && MODEL_ROUTING_MODES.includes(value as ModelRoutingMode)
}

export function normalizeModelRoutingMode(value: unknown): ModelRoutingMode {
  return isModelRoutingMode(value) ? value : 'off'
}
