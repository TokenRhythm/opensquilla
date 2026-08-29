import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type {
  SessionsChangedCanonicalPayload,
  SessionsChangedEventPayload,
  SessionsChangedLegacyPayload,
} from './generated/v4/sessionsChanged'

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

interface FixtureDocument {
  cases: Array<{ id: string, wire?: unknown }>
}

const validators = await import('./generated/v4/sessionsChangedValidators.mjs') as {
  validateSessionsChangedEventPayload: ContractValidator
}

function fixture(name: string): FixtureDocument {
  const url = new URL(
    `../../../contracts/gateway/v4/sessions/fixtures/sessions-changed/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('generated sessions.changed v4 validator', () => {
  it('accepts every canonical and legacy event golden oracle', () => {
    for (const testCase of fixture('events.json').cases) {
      if (testCase.wire === undefined) continue
      expect(
        validators.validateSessionsChangedEventPayload(testCase.wire),
        testCase.id,
      ).toBe(true)
    }
  })

  it('rejects every invalid event golden oracle', () => {
    for (const testCase of fixture('errors.json').cases) {
      if (testCase.wire === undefined) continue
      expect(
        validators.validateSessionsChangedEventPayload(testCase.wire),
        testCase.id,
      ).toBe(false)
    }
  })

  it('keeps canonical, legacy, and additive fields expressible', () => {
    const canonical: SessionsChangedCanonicalPayload = {
      schema_version: 1,
      key: 'agent:main:canonical',
      reason: 'created',
      futureField: { enabled: true },
    }
    const legacy: SessionsChangedLegacyPayload = {
      key: 'agent:main:legacy',
      reason: 'cron_system_event',
      futureField: ['preserved'],
    }
    const event: SessionsChangedEventPayload = canonical
    expect(event.futureField).toEqual({ enabled: true })
    expect(legacy.futureField).toEqual(['preserved'])
  })

  it('preserves JSON integer compatibility for the version discriminator', () => {
    expect(validators.validateSessionsChangedEventPayload({
      schema_version: 1.0,
      key: 'agent:main:float-version',
      reason: 'created',
    })).toBe(true)
    expect(validators.validateSessionsChangedEventPayload({
      schema_version: true,
      key: 'agent:main:bad',
      reason: 'created',
    })).toBe(false)
  })
})
