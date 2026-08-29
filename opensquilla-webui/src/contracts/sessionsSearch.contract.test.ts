import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type {
  SessionsSearchParams,
  SessionsSearchResult,
} from './generated/v4/sessionsSearch'

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

interface FixtureDocument {
  cases: Array<{ id: string, wire?: unknown }>
}

const validators = await import('./generated/v4/sessionsSearchValidators.mjs') as {
  validateSessionsSearchRequestFrame: ContractValidator
  validateSessionsSearchResponseFrame: ContractValidator
  validateSessionsSearchResult: ContractValidator
}

function fixture(name: string): FixtureDocument {
  const url = new URL(
    `../../../contracts/gateway/v4/sessions/fixtures/sessions-search/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('generated sessions.search v4 validators', () => {
  it('accepts every request golden oracle', () => {
    for (const testCase of fixture('requests.json').cases) {
      if (testCase.wire === undefined) continue
      expect(
        validators.validateSessionsSearchRequestFrame(testCase.wire),
        testCase.id,
      ).toBe(true)
    }
  })

  it('accepts every success and error response golden oracle', () => {
    for (const name of ['responses.json', 'errors.json']) {
      for (const testCase of fixture(name).cases) {
        if (testCase.wire === undefined) continue
        expect(
          validators.validateSessionsSearchResponseFrame(testCase.wire),
          testCase.id,
        ).toBe(true)
      }
    }
  })

  it('keeps generated domain types usable with nullable and extension fields', () => {
    const params: SessionsSearchParams = {
      query: 'milk',
      limit: 5,
      futureOption: { enabled: true },
    }
    const result: SessionsSearchResult = {
      sessions: [{
        key: 'agent:main:s1',
        title: 'Grocery list',
        effectiveAgentId: null,
        surface: null,
        updatedAt: null,
        futureField: ['preserved'],
      }],
      messages: [],
      query: params.query ?? '',
      ts: 1700000000000,
    }
    expect(result.sessions[0]?.futureField).toEqual(['preserved'])
  })

  it('rejects a different method and an incomplete result', () => {
    expect(validators.validateSessionsSearchRequestFrame({
      type: 'req',
      id: 'wrong-method',
      method: 'sessions.resolve',
      params: {},
    })).toBe(false)
    expect(validators.validateSessionsSearchResponseFrame({
      type: 'res',
      id: 'missing-fields',
      ok: true,
      payload: { sessions: [], messages: [], query: 'x' },
      error: null,
    })).toBe(false)
  })

  it('uses JSON Schema integer semantics for integral JSON numbers', () => {
    expect(validators.validateSessionsSearchResult({
      sessions: [],
      messages: [],
      query: 'x',
      ts: 1000.0,
    })).toBe(true)
    expect(validators.validateSessionsSearchResult({
      sessions: [],
      messages: [],
      query: 'x',
      ts: 1000.5,
    })).toBe(false)
  })
})
