import { describe, expect, it } from 'vitest'
import type { ChatSteerCapability } from '@/types/chat'
import { steerUnavailableReason } from './steerAvailability'

const availableCapability: ChatSteerCapability = {
  mode: 'same_turn',
  expected_turn_id: 'task-1',
  input_kinds: ['text'],
}

function reason(overrides: Partial<Parameters<typeof steerUnavailableReason>[0]> = {}) {
  return steerUnavailableReason({
    isStreaming: true,
    methodAvailable: true,
    modelRoutingMode: 'manual',
    capability: availableCapability,
    activeTaskId: 'task-1',
    ...overrides,
  })
}

describe('steerUnavailableReason', () => {
  it('returns no display reason when every authoritative gate is satisfied', () => {
    expect(reason()).toBeNull()
  })

  it.each([
    [{ isStreaming: false }, 'noActiveTurn'],
    [{ methodAvailable: false }, 'gatewayUnsupported'],
    [{ capability: null }, 'capabilityPending'],
    [{ activeTaskId: 'task-2' }, 'taskMismatch'],
    [{ capability: { ...availableCapability, expected_turn_id: '' } }, 'taskMismatch'],
    [{
      capability: { ...availableCapability, input_kinds: ['image'] as string[] },
    }, 'textUnsupported'],
    [{ capability: { mode: 'queue_only' } }, 'queueOnly'],
  ] as const)('maps local gate state %o to %s', (overrides, expected) => {
    expect(reason(overrides)).toBe(expected)
  })

  it('uses an authoritative same-turn capability after the next-turn mode changes', () => {
    expect(reason({ modelRoutingMode: 'llm_ensemble' })).toBeNull()
  })

  it('uses the session mode only as a fallback while capability is pending', () => {
    expect(reason({ modelRoutingMode: 'llm_ensemble', capability: null })).toBe('ensemble')
  })

  it('keeps an accepted ensemble turn unavailable after the next-turn mode changes', () => {
    expect(reason({
      modelRoutingMode: 'manual',
      capability: {
        mode: 'queue_only',
        reason: 'ensemble_requires_followup_turn',
      },
    })).toBe('ensemble')
  })

  it.each([
    ['gateway_upgrade_required', 'gatewayUnsupported'],
    ['ensemble_requires_followup_turn', 'ensemble'],
    ['restart_recovery_unavailable', 'taskType'],
    ['task_kind_not_steerable', 'taskType'],
    ['no_active_turn', 'noActiveTurn'],
    ['turn_closing', 'turnClosing'],
    ['future_reason', 'generic'],
  ] as const)('maps server reason %s to %s', (serverReason, expected) => {
    expect(reason({
      capability: { ...availableCapability, reason: serverReason },
    })).toBe(expected)
  })
})
