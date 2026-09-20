import { describe, expect, it } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { ChatMessage } from '@/types/chat'
import { normalizeTurnOutcome } from './turnOutcome'
import { localizedChatErrorMessage } from './errors'
import { chatErrorPresentation } from './chatErrorPresentation'
import { dedupeTerminalErrorNotices } from './terminalErrorNotices'
import { reconcileClientTerminalNotices } from './historyMerge'

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
    expect(localizedChatErrorMessage('429', fallback)).toBe('The task did not finish. Please try again later.')
    expect(localizedChatErrorMessage('429', fallback, false, 'future_kind')).toBe('The task did not finish. Please try again later.')
  })

  it('fails closed when references or turn identities conflict', () => {
    for (const nested of [{ error_id: 'abcdef02' }, { turn_id: 'another', error_id: 'abcdef01' }]) {
      expect(normalizeTurnOutcome({ turn_id: 't', error_id: 'abcdef01', outcome: nested })?.errorId).toBeNull()
    }
  })

  it.each(['en', 'zh-Hans', 'de', 'es', 'fr', 'ja'] as const)('localizes allowlisted causes in %s', async locale => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    for (const kind of ['rate_limited', 'provider_overloaded', 'auth_invalid', 'context_overflow',
      'unsupported_feature', 'insufficient_credits', 'model_not_found', 'transport_transient',
      'policy_refusal', 'empty_response', 'malformed_response', 'bad_request'] as const) {
      const message = localizedChatErrorMessage('429', 'safe fallback', false, kind)
      expect(message).not.toBe('safe fallback')
      expect(message).not.toContain('chat.errorMessage.')
      expect(message).toBe(i18n.global.t(chatErrorPresentation({ failureKind: kind }).messageKey))
    }
    i18n.global.locale.value = 'en'
  })

  const user = (turnId: string): ChatMessage => ({
    role: 'user', text: 'Synthetic request', turnId, messageId: `user-${turnId}`, ts: null,
  })
  const notice = (errorId: string | null, turnId = 'turn-a'): ChatMessage => ({
    role: 'error', text: 'Safe error', turnId, ts: null, terminalNotice: true, errorCode: '429',
    turnOutcome: { turnId, status: 'failed', errorId, failureKind: 'rate_limited' },
  })

  it.each(['abcdef01', null])('retains conflict evidence from live reference %s through repeated history sync', errorId => {
    const incoming = [user('turn-a'), { ...notice('abcdef02'), messageId: 'durable-error' }]
    const result = reconcileClientTerminalNotices([user('turn-a'), notice(errorId)], incoming)
    expect(result.filter(message => message.role === 'error')).toHaveLength(1)
    expect(result.find(message => message.role === 'error')?.turnOutcome?.errorId).toBeNull()
    expect(reconcileClientTerminalNotices(result, incoming).find(message => message.role === 'error')?.turnOutcome?.errorId).toBeNull()
  })

  it('never moves an identified error to another turn with identical user text', () => {
    expect(reconcileClientTerminalNotices([user('turn-a'), notice('abcdef01')], [user('turn-b')]))
      .toEqual([user('turn-b')])
  })

  it('keeps a terminal cause distinct from an unrelated same-turn error', () => {
    const other: ChatMessage = { role: 'error', text: 'Synthetic tool failure', turnId: 'turn-a', ts: null }
    const result = reconcileClientTerminalNotices([user('turn-a'), notice('abcdef01')], [user('turn-a'), other])
    expect(result).toContainEqual(other)
    expect(result.filter(message => message.terminalNotice)).toHaveLength(1)
  })

  it('merges a partial history page without requiring its user row', () => {
    const result = reconcileClientTerminalNotices([notice(null)], [{ ...notice('abcdef01'), messageId: 'durable-error' }])
    expect(result).toHaveLength(1)
    expect(result[0]?.turnOutcome?.errorId).toBeNull()
  })

  it.each([false, true])('keeps lifecycle timeout authoritative with reversed=%s', reversed => {
    const terminal: ChatMessage = { ...notice('abcdef01'), text: 'Safe timeout', turnOutcome: {
      ...notice('abcdef01').turnOutcome!, status: 'timeout', statusSource: 'task', reason: 'hard_deadline_exceeded',
    } }
    const rich = notice('abcdef01')
    const [merged] = dedupeTerminalErrorNotices(reversed ? [rich, terminal] : [terminal, rich])
    expect(merged?.text).toBe(i18n.global.t('chat.errorMessage.timeout'))
    expect(merged?.turnOutcome).toMatchObject({ status: 'timeout', reason: 'hard_deadline_exceeded', errorId: 'abcdef01' })
  })

  it('replaces technical terminal guidance with a concise local cause', () => {
    for (const [code, key] of [
      ['timeout', 'timeout'], ['llm_timeout', 'timeout'],
      ['provider_output_truncated', 'responseIncomplete'], ['provider_request_too_large', 'contextLimit'],
    ]) {
      expect(localizedChatErrorMessage(code, 'specific guidance', false, 'rate_limited'))
        .toBe(i18n.global.t(`chat.errorMessage.${key}`))
    }
    const reasoning = 'The model used its output budget for reasoning without returning a visible answer. Increase the budget.'
    expect(localizedChatErrorMessage('empty_response', reasoning, false, 'empty_response'))
      .toBe(i18n.global.t('chat.errorMessage.responseIncomplete'))
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
