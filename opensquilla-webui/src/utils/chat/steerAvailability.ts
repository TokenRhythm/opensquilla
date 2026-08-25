import type { ChatSteerCapability } from '@/types/chat'

export type SteerUnavailableReason =
  | 'gatewayUnsupported'
  | 'ensemble'
  | 'taskType'
  | 'queueOnly'
  | 'noActiveTurn'
  | 'turnClosing'
  | 'capabilityPending'
  | 'taskMismatch'
  | 'textUnsupported'
  | 'generic'

interface SteerAvailabilityState {
  isStreaming: boolean
  methodAvailable: boolean
  modelRoutingMode: string
  capability: ChatSteerCapability | null
  activeTaskId: string
}

const SERVER_REASON_MAP: Record<string, SteerUnavailableReason> = {
  gateway_upgrade_required: 'gatewayUnsupported',
  ensemble_requires_followup_turn: 'ensemble',
  restart_recovery_unavailable: 'taskType',
  task_kind_not_steerable: 'taskType',
  no_active_turn: 'noActiveTurn',
  turn_closing: 'turnClosing',
}

/** Explains an unavailable UI affordance without changing the authoritative steer gate. */
export function steerUnavailableReason(
  state: SteerAvailabilityState,
): SteerUnavailableReason | null {
  if (!state.isStreaming) return 'noActiveTurn'
  if (!state.methodAvailable) return 'gatewayUnsupported'

  const capability = state.capability
  if (!capability) {
    return state.modelRoutingMode === 'llm_ensemble' ? 'ensemble' : 'capabilityPending'
  }
  const serverReason = String(capability.reason || '').trim()
  if (serverReason) return SERVER_REASON_MAP[serverReason] || 'generic'
  if (capability.mode === 'queue_only') return 'queueOnly'
  if (capability.mode !== 'same_turn') return 'generic'

  const expectedTurnId = String(capability.expected_turn_id || '').trim()
  const activeTaskId = String(state.activeTaskId || '').trim()
  if (!expectedTurnId || activeTaskId !== expectedTurnId) return 'taskMismatch'
  if (capability.input_kinds?.length && !capability.input_kinds.includes('text')) {
    return 'textUnsupported'
  }
  return null
}
