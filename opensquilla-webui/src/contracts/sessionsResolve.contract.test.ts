import { loadContractValidators } from '../../../scripts/contracts/gateway_contract_verification.mjs'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type {
  SessionsResolveParams,
  SessionsResolveResult,
} from './generated/v4/sessionsResolve'

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

interface FixtureDocument {
  cases: Array<{ id: string, wire?: unknown }>
}

const validators = await loadContractValidators('sessions.resolve') as {
  validateSessionsResolveRequestFrame: ContractValidator
  validateSessionsResolveResponseFrame: ContractValidator
  validateSessionsResolveResult: ContractValidator
}

function fixture(name: string): FixtureDocument {
  const url = new URL(
    `../../../contracts/gateway/v4/sessions/fixtures/sessions-resolve/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('generated sessions.resolve v4 validators', () => {
  it('accepts every request golden oracle', () => {
    for (const testCase of fixture('requests.json').cases) {
      if (testCase.wire === undefined) continue
      expect(
        validators.validateSessionsResolveRequestFrame(testCase.wire),
        testCase.id,
      ).toBe(true)
    }
  })

  it('accepts every success and error response golden oracle', () => {
    for (const name of ['responses.json', 'errors.json']) {
      for (const testCase of fixture(name).cases) {
        if (testCase.wire === undefined) continue
        expect(
          validators.validateSessionsResolveResponseFrame(testCase.wire),
          testCase.id,
        ).toBe(true)
      }
    }
  })

  it('keeps generated domain types usable with nullable and extension fields', () => {
    const params: SessionsResolveParams = {
      key: 'agent:main:webchat:default',
      futureField: { enabled: true },
    }
    const result: SessionsResolveResult = {
      session_key: params.key,
      session_id: 'session-1',
      model: null,
      futureField: ['preserved'],
    }
    expect(result.session_key).toBe(params.key)
    expect(result.futureField).toEqual(['preserved'])
  })

  it('rejects a different method and incomplete identity projection', () => {
    expect(validators.validateSessionsResolveRequestFrame({
      type: 'req',
      id: 'wrong-method',
      method: 'sessions.search',
      params: { key: 'session-1' },
    })).toBe(false)
    expect(validators.validateSessionsResolveResponseFrame({
      type: 'res',
      id: 'missing-identity',
      ok: true,
      payload: { session_key: 'session-1' },
      error: null,
    })).toBe(false)
  })

  it('uses JSON Schema integer semantics for integral JSON numbers', () => {
    expect(validators.validateSessionsResolveResult({
      session_key: 'agent:main:canonical',
      session_id: 'canonical',
      created_at: 1000.0,
      updated_at: 2000.0,
    })).toBe(true)
    expect(validators.validateSessionsResolveResult({
      session_key: 'agent:main:canonical',
      session_id: 'canonical',
      created_at: 1000.5,
    })).toBe(false)
    expect(validators.validateSessionsResolveResult({
      session_key: 'agent:main:canonical',
      session_id: 'canonical',
      created_at: true,
    })).toBe(false)
  })
})
