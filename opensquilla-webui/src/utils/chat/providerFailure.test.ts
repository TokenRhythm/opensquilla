import { describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import type { ChatMessage } from '@/types/chat'
import { normalizeTurnOutcome } from './turnOutcome'
import { localizedChatErrorMessage } from './errors'
import { dedupeTerminalErrorNotices } from './terminalErrorNotices'

describe('provider terminal metadata', () => {
  it.each(['turn_outcome', 'outcome', 'turnOutcome'])('normalizes %s and live metadata identically', key => {
    const fields = { failure_kind: 'rate_limited', error_id: 'abcdef01' }
    const base = { turn_id: 'turn-1', status: 'failed' }
    expect(normalizeTurnOutcome({ ...base, [key]: fields })).toEqual(normalizeTurnOutcome({ ...base, ...fields }))
  })

  it.each(['ABCDEF01', 'abcdef0', 'abcdef012', 'abcdef01 ', 123, null, ''])('rejects invalid id %s', errorId => {
    const outcome = normalizeTurnOutcome({ turn_id: 't', error_id: errorId })
    expect(outcome?.errorId).toBeNull()
    expect(normalizeTurnOutcome(outcome as unknown as Record<string, unknown>)?.errorId).toBeNull()
  })

  it('does not parse references or classify prose or HTTP codes', () => {
    const fallback = 'Rate limited (ref: abcdef01)'
    const outcome = normalizeTurnOutcome({ turn_id: 't', code: '429', message: fallback })
    expect(outcome?.errorId).toBeUndefined()
    expect(outcome?.failureKind).toBeUndefined()
    expect(localizedChatErrorMessage('429', fallback)).toBe(fallback)
    expect(localizedChatErrorMessage('429', fallback, false, 'future_kind')).toBe(fallback)
  })

  it('fails closed when references or turn identities conflict', () => {
    for (const nested of [{ error_id: 'abcdef02' }, { turn_id: 'another', error_id: 'abcdef01' }]) {
      expect(normalizeTurnOutcome({ turn_id: 't', error_id: 'abcdef01', outcome: nested })?.errorId).toBeNull()
    }
  })

  it.each(['en', 'zh-Hans', 'de', 'es', 'fr', 'ja'] as const)('localizes allowlisted causes in %s', locale => {
    i18n.global.locale.value = locale
    for (const kind of ['rate_limited', 'provider_overloaded', 'auth_invalid', 'context_overflow',
      'unsupported_feature', 'insufficient_credits', 'model_not_found', 'transport_transient',
      'policy_refusal', 'empty_response', 'malformed_response', 'bad_request']) {
      const message = localizedChatErrorMessage('429', 'safe fallback', false, kind)
      expect(message).not.toBe('safe fallback')
      expect(message).not.toContain('chat.providerFailure.')
    }
    i18n.global.locale.value = 'en'
  })

  it('keeps more specific terminal guidance', () => {
    for (const code of ['timeout', 'llm_timeout', 'provider_output_truncated', 'provider_request_too_large']) {
      expect(localizedChatErrorMessage(code, 'specific guidance', false, 'rate_limited')).toBe('specific guidance')
    }
    const reasoning = 'The model used its output budget for reasoning without returning a visible answer. Increase the budget.'
    expect(localizedChatErrorMessage('empty_response', reasoning, false, 'empty_response')).toBe(reasoning)
  })

  it('merges notices by explicit turn, keeps partial content, and retains reference conflicts', () => {
    const notice = (id: string): ChatMessage => ({
      role: 'error', text: 'safe message', turnId: 't', errorCode: '429', terminalNotice: true, ts: null,
      turnOutcome: { turnId: 't', status: 'failed', failureKind: 'rate_limited', errorId: id },
    })
    const partial: ChatMessage = { role: 'assistant', text: 'Partial answer', turnId: 't', ts: null }
    const merged = dedupeTerminalErrorNotices([partial, notice('abcdef01'), notice('abcdef02')])
    expect(merged).toHaveLength(2)
    expect(merged[0]).toEqual(partial)
    expect(merged[1]?.turnOutcome?.errorId).toBeNull()
    expect(dedupeTerminalErrorNotices([...merged, notice('abcdef01')])[1]?.turnOutcome?.errorId).toBeNull()
    expect(dedupeTerminalErrorNotices([{ ...notice('abcdef01'), turnId: undefined }, notice('abcdef01')])).toHaveLength(2)
  })
})
