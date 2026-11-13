import type { JevDecision, JevSnapshot, JevEnsemble } from '@/modules/jevRouting'

export function jevDecision(overrides: Partial<JevDecision> = {}): JevDecision {
  return {
    id: 'decision-synthetic',
    sessionKey: 'agent:main:webchat:synthetic',
    action: 'switch',
    status: 'pending',
    current: { provider: 'example', model: 'large-model' },
    target: { provider: 'example', model: 'small-model' },
    reason: 'A compact task can use a smaller model.',
    evidence: ['Short input', 'No tools required'],
    risk: 'low',
    createdAt: 123,
    ...overrides,
  }
}

export function jevSnapshot(overrides: Partial<JevSnapshot> = {}): JevSnapshot {
  return {
    sessionKey: 'agent:main:webchat:synthetic',
    enabled: true,
    mode: 'suggest',
    budgetPerHour: 3,
    model: 'synthetic-jev',
    provider: 'example',
    available: true,
    status: 'ready',
    recommendation: null,
    decisions: [],
    ...overrides,
  }
}

export function jevEnsemble(overrides: Partial<JevEnsemble> = {}): JevEnsemble {
  return {
    tier: 'c4', label: 'Synthetic fusion', strategy: 'propose_then_aggregate',
    proposers: [{ provider: 'example', model: 'analyst-a' }, { provider: 'example', model: 'analyst-b' }],
    aggregator: { provider: 'example', model: 'synthesis' },
    configurationFingerprint: 'a'.repeat(64), ...overrides,
  }
}

export function jevHandoffDecision(overrides: Partial<JevDecision> = {}): JevDecision {
  return jevDecision({
    action: 'new_session', target: null, reason: 'The conversation history is approaching its context limit.',
    handoff: { sourceSessionKey: 'agent:main:webchat:synthetic', contextTokens: 64000, contextWindowTokens: 80000 },
    ...overrides,
  })
}
