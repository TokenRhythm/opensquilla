import { describe, expect, it } from 'vitest'

import { normalizeTurnOutcome } from './turnOutcome'

describe('normalizeTurnOutcome document mutation receipts', () => {
  it('normalizes an authoritative snake-case receipt without consulting prose', () => {
    expect(normalizeTurnOutcome({
      turn_id: 'turn-1',
      status: 'succeeded',
      outcome: {
        kind: 'completed',
        document_mutation_outcome: {
          version: 1,
          status: 'applied',
          phase: 'commit',
          retry_policy: 'never',
          change_set_id: 'change-1',
          result_revision_id: 'revision-2',
          proposal_attempts: 2,
          corrected: true,
        },
      },
    })).toMatchObject({
      turnId: 'turn-1',
      status: 'succeeded',
      documentMutationOutcome: {
        version: 1,
        status: 'applied',
        phase: 'commit',
        retryPolicy: 'never',
        changeSetId: 'change-1',
        resultRevisionId: 'revision-2',
        proposalAttempts: 2,
        corrected: true,
      },
    })
  })

  it('does not invent a document result from generic task completion', () => {
    expect(normalizeTurnOutcome({
      turn_id: 'turn-2',
      status: 'succeeded',
      outcome: { kind: 'completed', reason: 'done' },
    })?.documentMutationOutcome).toBeUndefined()
  })

  it('drops unknown mutation states instead of widening the product contract', () => {
    expect(normalizeTurnOutcome({
      turn_id: 'turn-3',
      status: 'succeeded',
      document_mutation_outcome: { status: 'probably_applied' },
    })?.documentMutationOutcome).toBeUndefined()
  })
})
