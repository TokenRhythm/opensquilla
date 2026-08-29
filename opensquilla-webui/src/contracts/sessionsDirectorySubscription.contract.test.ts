import { describe, expect, it } from 'vitest'

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

const subscribeValidators = await import(
  './generated/v4/sessionsSubscribeValidators.mjs',
) as {
  validateSessionsSubscribeRequestFrame: ContractValidator
  validateSessionsSubscribeResponseFrame: ContractValidator
  validateSessionsSubscribeResult: ContractValidator
}
const unsubscribeValidators = await import(
  './generated/v4/sessionsUnsubscribeValidators.mjs',
) as {
  validateSessionsUnsubscribeRequestFrame: ContractValidator
  validateSessionsUnsubscribeResponseFrame: ContractValidator
  validateSessionsUnsubscribeResult: ContractValidator
}

describe('generated session-directory lease validators', () => {
  const contracts = [
    {
      name: 'subscribe',
      method: 'sessions.subscribe',
      request: subscribeValidators.validateSessionsSubscribeRequestFrame,
      response: subscribeValidators.validateSessionsSubscribeResponseFrame,
      result: subscribeValidators.validateSessionsSubscribeResult,
    },
    {
      name: 'unsubscribe',
      method: 'sessions.unsubscribe',
      request: unsubscribeValidators.validateSessionsUnsubscribeRequestFrame,
      response: unsubscribeValidators.validateSessionsUnsubscribeResponseFrame,
      result: unsubscribeValidators.validateSessionsUnsubscribeResult,
    },
  ] as const

  for (const contract of contracts) {
    const { name, method } = contract
    it(`${name} accepts current and legacy request parameter shapes`, () => {
      for (const params of [undefined, {}, null, [], 'legacy', 7, true]) {
        const request: Record<string, unknown> = {
          type: 'req',
          id: 'lease-1',
          method,
        }
        if (params !== undefined) request.params = params
        expect(contract.request(request)).toBe(true)
      }
    })

    it(`${name} preserves null result and error frame shapes`, () => {
      expect(contract.response({
        type: 'res',
        id: 'lease-1',
        ok: true,
        payload: null,
      })).toBe(true)
      expect(contract.response({
        type: 'res',
        id: 'lease-1',
        ok: false,
        error: { code: 'UNAUTHORIZED', message: 'guest denied' },
      })).toBe(true)
      expect(contract.result(null)).toBe(true)
      expect(contract.result({ subscribed: true })).toBe(false)
    })
  }

  it('rejects cross-method and malformed lease frames', () => {
    expect(subscribeValidators.validateSessionsSubscribeRequestFrame({
      type: 'req',
      id: 'wrong-method',
      method: 'sessions.unsubscribe',
    })).toBe(false)
    expect(unsubscribeValidators.validateSessionsUnsubscribeResponseFrame({
      type: 'res',
      id: 'missing-error',
      ok: false,
    })).toBe(false)
  })
})
