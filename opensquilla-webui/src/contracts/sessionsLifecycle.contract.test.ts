import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

interface FixtureCase {
  id: string
  wire?: unknown
}

interface FixtureDocument {
  cases: FixtureCase[]
}

interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

const contracts = [
  {
    name: 'create',
    directory: 'sessions-create',
    request: () => import('./generated/v4/sessionsCreateValidators.mjs')
      .then(module => (module as { validateSessionsCreateRequestFrame: ContractValidator }).validateSessionsCreateRequestFrame),
    response: () => import('./generated/v4/sessionsCreateValidators.mjs')
      .then(module => (module as { validateSessionsCreateResponseFrame: ContractValidator }).validateSessionsCreateResponseFrame),
  },
  {
    name: 'rename',
    directory: 'sessions-rename',
    request: () => import('./generated/v4/sessionsRenameValidators.mjs')
      .then(module => (module as { validateSessionsRenameRequestFrame: ContractValidator }).validateSessionsRenameRequestFrame),
    response: () => import('./generated/v4/sessionsRenameValidators.mjs')
      .then(module => (module as { validateSessionsRenameResponseFrame: ContractValidator }).validateSessionsRenameResponseFrame),
  },
  {
    name: 'delete',
    directory: 'sessions-delete',
    request: () => import('./generated/v4/sessionsDeleteValidators.mjs')
      .then(module => (module as { validateSessionsDeleteRequestFrame: ContractValidator }).validateSessionsDeleteRequestFrame),
    response: () => import('./generated/v4/sessionsDeleteValidators.mjs')
      .then(module => (module as { validateSessionsDeleteResponseFrame: ContractValidator }).validateSessionsDeleteResponseFrame),
  },
] as const

function fixture(directory: string, name: string): FixtureDocument {
  const url = new URL(
    `../../../contracts/gateway/v4/sessions/fixtures/${directory}/${name}`,
    import.meta.url,
  )
  return JSON.parse(readFileSync(url, 'utf8')) as FixtureDocument
}

describe('generated session lifecycle v4 validators', () => {
  for (const contract of contracts) {
    it(`${contract.name} accepts every request golden oracle`, async () => {
      const validate = await contract.request()
      for (const testCase of fixture(contract.directory, 'requests.json').cases) {
        if (testCase.wire === undefined) continue
        expect(validate(testCase.wire), testCase.id).toBe(true)
      }
    })

    it(`${contract.name} accepts every success and error golden oracle`, async () => {
      const validate = await contract.response()
      for (const name of ['responses.json', 'errors.json']) {
        for (const testCase of fixture(contract.directory, name).cases) {
          if (testCase.wire === undefined) continue
          expect(validate(testCase.wire), testCase.id).toBe(true)
        }
      }
    })
  }

  it('rejects cross-method and incomplete lifecycle frames', async () => {
    const create = await contracts[0].request()
    const rename = await contracts[1].response()
    expect(create({
      type: 'req',
      id: 'wrong-method',
      method: 'sessions.rename',
      params: {},
    })).toBe(false)
    expect(rename({
      type: 'res',
      id: 'missing-fields',
      ok: true,
      payload: { key: 'only-key' },
    })).toBe(false)
  })
})
