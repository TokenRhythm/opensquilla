import { loadContractValidators } from '../../../scripts/contracts/gateway_contract_verification.mjs'
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
    request: () => loadContractValidators('sessions.create')
      .then(module => (module as { validateSessionsCreateRequestFrame: ContractValidator }).validateSessionsCreateRequestFrame),
    response: () => loadContractValidators('sessions.create')
      .then(module => (module as { validateSessionsCreateResponseFrame: ContractValidator }).validateSessionsCreateResponseFrame),
  },
  {
    name: 'rename',
    directory: 'sessions-rename',
    request: () => loadContractValidators('sessions.rename')
      .then(module => (module as { validateSessionsRenameRequestFrame: ContractValidator }).validateSessionsRenameRequestFrame),
    response: () => loadContractValidators('sessions.rename')
      .then(module => (module as { validateSessionsRenameResponseFrame: ContractValidator }).validateSessionsRenameResponseFrame),
  },
  {
    name: 'delete',
    directory: 'sessions-delete',
    request: () => loadContractValidators('sessions.delete')
      .then(module => (module as { validateSessionsDeleteRequestFrame: ContractValidator }).validateSessionsDeleteRequestFrame),
    response: () => loadContractValidators('sessions.delete')
      .then(module => (module as { validateSessionsDeleteResponseFrame: ContractValidator }).validateSessionsDeleteResponseFrame),
  },
  {
    name: 'fork',
    directory: 'sessions-fork',
    request: () => loadContractValidators('sessions.fork')
      .then(module => (module as { validateSessionsForkRequestFrame: ContractValidator }).validateSessionsForkRequestFrame),
    response: () => loadContractValidators('sessions.fork')
      .then(module => (module as { validateSessionsForkResponseFrame: ContractValidator }).validateSessionsForkResponseFrame),
  },
  {
    name: 'forkThroughTurn',
    directory: 'sessions-fork-through-turn',
    request: () => loadContractValidators('sessions.forkThroughTurn')
      .then(module => (module as { validateSessionsForkThroughTurnRequestFrame: ContractValidator }).validateSessionsForkThroughTurnRequestFrame),
    response: () => loadContractValidators('sessions.forkThroughTurn')
      .then(module => (module as { validateSessionsForkThroughTurnResponseFrame: ContractValidator }).validateSessionsForkThroughTurnResponseFrame),
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

  it('rejects incomplete or ambiguous fork acknowledgements', async () => {
    const fork = await contracts[3].response()
    const forkThroughTurn = await contracts[4].response()

    expect(fork({
      type: 'res',
      id: 'missing-parent',
      ok: true,
      payload: { key: 'agent:main:webchat:child' },
      error: null,
    })).toBe(false)
    expect(fork({
      type: 'res',
      id: 'incomplete-through-ack',
      ok: true,
      payload: {
        key: 'agent:main:webchat:child',
        parentKey: 'agent:main:webchat:parent',
        forkMode: 'through_turn',
      },
      error: null,
    })).toBe(false)
    expect(forkThroughTurn({
      type: 'res',
      id: 'silent-full-fallback',
      ok: true,
      payload: {
        key: 'agent:main:webchat:child',
        parentKey: 'agent:main:webchat:parent',
      },
      error: null,
    })).toBe(false)
  })
})
