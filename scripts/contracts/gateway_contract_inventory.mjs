import { readFileSync, readdirSync } from 'node:fs'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { contractTargets } from './generate_gateway_contract_ajv.mjs'

export const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

export function walkFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? walkFiles(path) : entry.isFile() ? [path] : []
  }).sort()
}

export const targetIdentity = (kind, wireName, role) => `${kind}:${wireName}:${role}`

/** One inventory is shared by the usage gate and temporary verification loader. */
export function readContractInventory(root = repositoryRoot) {
  const identities = new Set()
  const stems = new Set()
  return walkFiles(join(root, 'contracts/gateway/v4'))
    .filter(path => path.endsWith('.schema.json'))
    .map(path => {
      const schema = JSON.parse(readFileSync(path, 'utf8'))
      const method = schema['x-opensquilla-method']
      const metadata = method ?? schema['x-opensquilla-event']
      const kind = method === undefined ? 'event' : 'method'
      const wireName = metadata?.name
      const stem = basename(path, '.schema.json').replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())
      const identity = `${kind}:${wireName}`
      if (typeof wireName !== 'string' || identities.has(identity) || stems.has(stem)) {
        throw new Error(`ambiguous Contract inventory: ${path}`)
      }
      identities.add(identity)
      stems.add(stem)
      return { kind, wireName, stem, schema, path, targets: contractTargets(schema) }
    })
}

export function readProductionTargets(inventory, manifest) {
  if (!manifest || Object.keys(manifest).sort().join(',') !== 'format,targets'
    || manifest.format !== 1 || !Array.isArray(manifest.targets)) {
    throw new Error('invalid production target manifest format')
  }
  const contracts = new Map(inventory.map(contract => [`${contract.kind}:${contract.wireName}`, contract]))
  const seen = new Set()
  const selected = []
  for (const entry of manifest.targets) {
    if (!entry || Object.keys(entry).sort().join(',') !== 'kind,roles,wireName'
      || typeof entry.kind !== 'string' || typeof entry.wireName !== 'string') {
      throw new Error('invalid production target entry')
    }
    const identity = `${entry.kind}:${entry.wireName}`
    const contract = contracts.get(identity)
    if (!contract || seen.has(identity)) throw new Error(`unknown or duplicate production Contract: ${identity}`)
    seen.add(identity)
    if (!Array.isArray(entry.roles) || entry.roles.length === 0
      || new Set(entry.roles).size !== entry.roles.length
      || entry.roles.some(role => !contract.targets.some(target => target.role === role))) {
      throw new Error(`invalid production validator roles: ${identity}`)
    }
    for (const target of contract.targets.filter(target => entry.roles.includes(target.role))) {
      selected.push({ ...target, kind: contract.kind, wireName: contract.wireName, stem: contract.stem })
    }
  }
  return selected
}
