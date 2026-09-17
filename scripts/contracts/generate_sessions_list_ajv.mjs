#!/usr/bin/env node
// Generate deterministic standalone validators for the sessions.list Contract.

import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '../..')
const requireFromWebui = createRequire(resolve(repoRoot, 'opensquilla-webui/package.json'))
const Ajv2020 = requireFromWebui('ajv/dist/2020').default
const standaloneCode = requireFromWebui('ajv/dist/standalone').default

const schemaPath = process.argv[2]
if (!schemaPath) throw new Error('usage: generate_ajv.mjs <schema>')

const schema = JSON.parse(readFileSync(schemaPath, 'utf8'))
const ajv = new Ajv2020({
  allErrors: true,
  strict: true,
  strictRequired: false,
  allowUnionTypes: true,
  code: { esm: false, source: true, optimize: true },
})
for (const keyword of [
  'x-opensquilla-candidate',
  'x-opensquilla-codegen',
  'x-opensquilla-method',
  'x-opensquilla-known-values',
  'x-opensquilla-wire',
]) {
  ajv.addKeyword({ keyword, valid: true })
}
ajv.addSchema(schema)

const definitions = [
  'SessionsListRequestFrame',
  'SessionsListResponseFrame',
]
const exports = {}
for (const name of definitions) {
  const id = `urn:opensquilla:contract:v4:${name}`
  ajv.addSchema({
    $id: id,
    $schema: schema.$schema,
    $ref: `${schema.$id}#/$defs/${name}`,
  })
  exports[`validate${name}`] = id
}

process.stdout.write(standaloneCode(ajv, exports))
