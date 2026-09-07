import { loadContractValidators } from '../../../scripts/contracts/gateway_contract_verification.mjs'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { SessionRow, SessionTask } from './generated/v4/sessionsList'

// Compile-time Contract fixture: generated nullable wire types must admit the
// null values already present in v4 golden responses.
const nullableTask: SessionTask = null
const rowWithNullableTasks: SessionRow = {
  key: 'agent:main:webchat:nullable-task',
  active_task: nullableTask,
  last_task: null,
}
void rowWithNullableTasks

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

interface FixtureDocument {
  cases: Array<{ id: string, wire?: unknown }>
}

const validators = await loadContractValidators('sessions.list') as {
  validateSessionsListRequestFrame: ContractValidator
  validateSessionsListResponseFrame: ContractValidator
}

function fixture(name: string): FixtureDocument {
  const url = new URL(
    `../../../contracts/gateway/v4/sessions/fixtures/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('generated sessions.list v4 validators', () => {
  it('accepts every request golden oracle', () => {
    for (const testCase of fixture('requests.json').cases) {
      if (testCase.wire === undefined) continue
      expect(
        validators.validateSessionsListRequestFrame(testCase.wire),
        testCase.id,
      ).toBe(true)
    }
  })

  it('accepts every success and error response golden oracle', () => {
    for (const name of ['responses.json', 'errors.json']) {
      for (const testCase of fixture(name).cases) {
        if (testCase.wire === undefined) continue
        expect(
          validators.validateSessionsListResponseFrame(testCase.wire),
          testCase.id,
        ).toBe(true)
      }
    }
  })

  it.each([
    { hasMore: true },
    { has_more: false },
    { nextCursor: 'opaque' },
    { next_cursor: null },
    { totalCount: 7 },
    { total_count: 7 },
  ])('accepts independently optional v4 aliases: %j', (partialAlias) => {
    expect(validators.validateSessionsListResponseFrame({
      type: 'res',
      id: 'partial-alias',
      ok: true,
      payload: { sessions: [], count: 0, ts: 1, ...partialAlias },
      error: null,
    })).toBe(true)
  })

  it('rejects a different method and an incomplete success payload', () => {
    expect(validators.validateSessionsListRequestFrame({
      type: 'req',
      id: 'wrong-method',
      method: 'sessions.search',
      params: {},
    })).toBe(false)
    expect(validators.validateSessionsListResponseFrame({
      type: 'res',
      id: 'missing-fields',
      ok: true,
      payload: { sessions: [] },
      error: null,
    })).toBe(false)
  })
})
