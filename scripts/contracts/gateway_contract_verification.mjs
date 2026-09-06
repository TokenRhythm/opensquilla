// Test-only standalone loading. No Python dependency and no production artifacts.
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { compileContract } from './generate_gateway_contract_ajv.mjs'
import { readContractInventory } from './gateway_contract_inventory.mjs'

let inventory
let directory
const modules = new Map()

export async function loadContractValidators(wireName, { kind = 'method' } = {}) {
  inventory ??= readContractInventory()
  const contract = inventory.find(entry => entry.kind === kind && entry.wireName === wireName)
  if (!contract) throw new Error(`unknown verification Contract: ${kind}:${wireName}`)
  const key = `${kind}:${wireName}`
  if (!modules.has(key)) {
    if (!directory) {
      directory = mkdtempSync(join(tmpdir(), 'opensquilla-contract-verification-'))
      const ownedDirectory = directory
      process.once('exit', () => rmSync(ownedDirectory, { recursive: true, force: true }))
    }
    const output = join(directory, `${contract.stem}Validators.mjs`)
    writeFileSync(output, compileContract(contract.schema), 'utf8')
    modules.set(key, import(/* @vite-ignore */ pathToFileURL(output).href))
  }
  return modules.get(key)
}
