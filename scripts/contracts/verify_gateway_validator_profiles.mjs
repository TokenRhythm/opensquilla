#!/usr/bin/env node
// Complete role inventory plus finite differential corpus; not an equivalence proof.
import assert from 'node:assert/strict'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { parseArgs } from 'node:util'
import {
  readContractInventory, readProductionTargets, repositoryRoot, targetIdentity, walkFiles,
} from './gateway_contract_inventory.mjs'
import { loadContractValidators } from './gateway_contract_verification.mjs'
import { jsonProbes, mutationSamples, schemaSamples } from './verification_samples.mjs'

function fixtureValues(root) {
  const values = []
  for (const path of walkFiles(join(root, 'contracts/gateway/v4'))) {
    if (!path.replace(/\\/g, '/').includes('/fixtures/') || !path.endsWith('.json')) continue
    const document = JSON.parse(readFileSync(path, 'utf8'))
    for (const testCase of document.cases ?? []) {
      if (!Object.hasOwn(testCase, 'wire')) continue
      const wire = testCase.wire
      values.push(wire)
      if (wire && typeof wire === 'object') {
        for (const field of ['params', 'payload', 'result']) {
          if (Object.hasOwn(wire, field)) values.push(wire[field])
        }
      }
    }
  }
  return values
}

async function storedValidators(root, contract) {
  for (const extension of ['mjs', 'cjs']) {
    const path = join(root, 'opensquilla-webui/src/contracts/generated/v4', `${contract.stem}Validators.${extension}`)
    if (!existsSync(path)) continue
    const module = await import(pathToFileURL(path).href)
    return extension === 'cjs' ? module.default : module
  }
  return {}
}

function evaluate(validator, value, label) {
  const input = structuredClone(value)
  const accepted = validator(input)
  assert.equal(typeof accepted, 'boolean', `${label}: synchronous boolean validator`)
  assert.deepEqual(input, value, `${label}: validation must not mutate inputs`)
  return { accepted, errors: structuredClone(validator.errors ?? null) }
}

export async function verifyProfiles({ baselineRoot, verificationRoot } = {}) {
  const inventory = readContractInventory()
  const policy = JSON.parse(readFileSync(join(repositoryRoot, 'contracts/gateway/v4/production-targets.json'), 'utf8'))
  const selected = new Set(readProductionTargets(inventory, policy).map(target => (
    targetIdentity(target.kind, target.wireName, target.role)
  )))
  const fixtures = fixtureValues(repositoryRoot)
  const result = {
    contracts: inventory.length, roles: 0, comparedRoles: 0, comparedInputs: 0,
    supplementalRoles: [], rolesWithoutPositiveSeed: [], acceptedInputs: 0, rejectedInputs: 0,
  }
  for (const contract of inventory) {
    const validators = await loadContractValidators(contract.wireName, { kind: contract.kind })
    assert.deepEqual(Object.keys(validators).sort(), contract.targets.map(target => target.exportName).sort())
    const stored = await storedValidators(baselineRoot ?? repositoryRoot, contract)
    const expectedExports = contract.targets.filter(target => baselineRoot
      ? !(contract.wireName === 'sessions.list' && ['params', 'result'].includes(target.role))
      : selected.has(targetIdentity(contract.kind, contract.wireName, target.role)))
    assert.deepEqual(Object.keys(stored).sort(), expectedExports.map(target => target.exportName).sort(),
      `${contract.wireName}: unexpected persistent exports`)
    const complete = verificationRoot ? await storedValidators(verificationRoot, contract) : null
    if (complete) assert.deepEqual(Object.keys(complete).sort(), Object.keys(validators).sort())
    for (const target of contract.targets) {
      const identity = targetIdentity(contract.kind, contract.wireName, target.role)
      const validator = validators[target.exportName]
      assert.equal(typeof validator, 'function', identity)
      const seeds = schemaSamples(contract.schema, { $ref: target.reference })
      const positives = [...seeds, ...fixtures].filter(value => validator(structuredClone(value)))
      if (!positives.length) result.rolesWithoutPositiveSeed.push(identity)
      const corpus = new Map([...jsonProbes, ...seeds, ...fixtures,
        ...positives.slice(0, 4).flatMap(value => mutationSamples(value)),
      ].map(value => [JSON.stringify(value), value]))
      const previous = stored[target.exportName]
      if (previous) result.comparedRoles++
      else if (baselineRoot) result.supplementalRoles.push(identity)
      for (const [probe, value] of corpus) {
        const label = `${identity}: ${probe.slice(0, 200)}`
        const actual = evaluate(validator, value, label)
        result[actual.accepted ? 'acceptedInputs' : 'rejectedInputs']++
        if (previous) {
          assert.deepEqual(actual, evaluate(previous, value, label), `${label}: baseline mismatch`)
          result.comparedInputs++
        }
        if (complete) assert.deepEqual(actual, evaluate(complete[target.exportName], value, label),
          `${label}: verification profile mismatch`)
      }
      result.roles++
    }
  }
  assert.equal(result.roles, 869)
  assert.equal(result.comparedRoles, baselineRoot ? 867 : selected.size)
  assert.deepEqual(result.rolesWithoutPositiveSeed, [], 'each role requires a positive seed')
  if (baselineRoot) assert.deepEqual(result.supplementalRoles, [
    'method:sessions.list:params', 'method:sessions.list:result',
  ])
  return result
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { values } = parseArgs({ options: {
    'baseline-root': { type: 'string' }, 'verification-root': { type: 'string' }, report: { type: 'string' },
  } })
  const report = await verifyProfiles({
    baselineRoot: values['baseline-root'], verificationRoot: values['verification-root'],
  })
  const serialized = `${JSON.stringify(report, null, 2)}\n`
  if (values.report) writeFileSync(values.report, serialized, 'utf8')
  console.log(serialized)
}
