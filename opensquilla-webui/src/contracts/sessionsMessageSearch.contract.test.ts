import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { validateParams, validateResult } from './generated/v4/sessionsMessagesSearchValidators.mjs'
import { validateParams as validateHistoryParams } from './generated/v4/sessionsHistoryPageV2Validators.mjs'

interface FixtureDocument {
  cases: Array<{ id: string; expectation: string; wire: { params?: unknown; payload?: unknown } }>
}

function fixture(name: string): FixtureDocument {
  const url = new URL(`../../../contracts/gateway/v4/sessions/fixtures/sessions-messages-search/${name}`, import.meta.url)
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('current-session message search contracts', () => {
  for (const testCase of fixture('requests.json').cases) {
    it(testCase.id, () => {
      expect(validateParams(testCase.wire.params)).toBe(testCase.expectation !== 'reject')
    })
  }

  for (const testCase of fixture('responses.json').cases) {
    it(testCase.id, () => {
      expect(validateResult(testCase.wire.payload)).toBe(testCase.expectation !== 'reject')
    })
  }

  it('accepts a stable message anchor without a directional page cursor', () => {
    expect(validateHistoryParams({ key: 'key', anchor_message_id: 'mid', direction: 'before', target_items: 20 })).toBe(true)
  })

  it.each(['opaque-page-cursor', null])('rejects simultaneous message anchor and page cursor (%s)', cursor => {
    expect(validateHistoryParams({ key: 'key', anchor_message_id: 'mid', direction: 'before', cursor })).toBe(false)
  })

  it('keeps normal history pagination valid', () => {
    expect(validateHistoryParams({ key: 'key', cursor: 'opaque-page-cursor', direction: 'before' })).toBe(true)
  })
})
