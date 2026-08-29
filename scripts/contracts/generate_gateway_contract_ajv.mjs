#!/usr/bin/env node
// Generate deterministic standalone validators for one Gateway v4 Contract.

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
if (!schemaPath) throw new Error('usage: generate_gateway_contract_ajv.mjs <schema>')
const esm = process.argv.includes('--esm')

const schema = JSON.parse(readFileSync(schemaPath, 'utf8'))
const method = schema['x-opensquilla-method']
const event = schema['x-opensquilla-event']
if ((method === undefined) === (event === undefined)) {
  throw new Error('Contract must declare exactly one method or event metadata block')
}

const eventTargets = event === undefined
  ? []
  : ['frame', 'payload'].filter(role => Object.hasOwn(event, role))
if (event !== undefined && eventTargets.length !== 1) {
  throw new Error('event Contract must declare exactly one of frame or payload')
}
const targets = method === undefined
  ? [event[eventTargets[0]]]
  : [method.request, method.params, method.response, method.result]

function directDefinitionName(reference) {
  const prefix = '#/$defs/'
  if (typeof reference !== 'string' || !reference.startsWith(prefix)) {
    throw new Error('generated validator target must be a local $defs reference')
  }
  const name = reference.slice(prefix.length)
  if (!name || name.includes('/')) {
    throw new Error('generated validator target must name one direct $defs member')
  }
  return name
}

const ajv = new Ajv2020({
  allErrors: true,
  strict: true,
  strictRequired: false,
  allowUnionTypes: true,
  code: { esm, source: true, optimize: true },
})

// AJV 8.17 can emit a CommonJS runtime reference even when standalone
// generation is configured for ESM (currently this happens for maxLength /
// minLength via ucs2length).  A generated validator is loaded directly by
// Vite in the browser, so leaving that reference in the artifact would fail
// at runtime with ``require is not defined``.  Keep the browser artifact
// self-contained and fail closed if a future AJV version introduces a runtime
// helper that we have not reviewed yet.
const ESM_UCS2_LENGTH = '__opensquillaAjvUcs2Length'
const ESM_UCS2_LENGTH_SOURCE = `function ${ESM_UCS2_LENGTH}(str) {
  const len = str.length
  let length = 0
  let pos = 0
  let value
  while (pos < len) {
    length++
    value = str.charCodeAt(pos++)
    if (value >= 0xd800 && value <= 0xdbff && pos < len) {
      value = str.charCodeAt(pos)
      if ((value & 0xfc00) === 0xdc00) pos++
    }
  }
  return length
}
`

function browserSafeEsm(source) {
  if (!esm) return source
  let usedUcs2Length = false
  const normalized = source.replace(
    /const ([A-Za-z_$][\w$]*) = require\("([^"]+)"\)\.default;/g,
    (statement, binding, moduleName) => {
      if (moduleName !== 'ajv/dist/runtime/ucs2length') {
        throw new Error(
          `ESM standalone validator contains an unreviewed CommonJS runtime: ${moduleName}`,
        )
      }
      usedUcs2Length = true
      return `const ${binding} = ${ESM_UCS2_LENGTH};`
    },
  )
  if (normalized.includes('require(')) {
    throw new Error('ESM standalone validator must not contain require(...)')
  }
  return usedUcs2Length ? `${ESM_UCS2_LENGTH_SOURCE}${normalized}` : normalized
}

function collectExtensionKeywords(value, keywords = new Set()) {
  if (Array.isArray(value)) {
    for (const item of value) collectExtensionKeywords(item, keywords)
    return keywords
  }
  if (value === null || typeof value !== 'object') return keywords
  for (const [key, nested] of Object.entries(value)) {
    if (key.startsWith('x-opensquilla-')) keywords.add(key)
    collectExtensionKeywords(nested, keywords)
  }
  return keywords
}

for (const keyword of [...collectExtensionKeywords(schema)].sort()) {
  ajv.addKeyword({ keyword, valid: true })
}
ajv.addSchema(schema)

const exports = {}
for (const reference of targets) {
  const name = directDefinitionName(reference)
  const id = `urn:opensquilla:contract:v4:${name}`
  ajv.addSchema({
    $id: id,
    $schema: schema.$schema,
    $ref: `${schema.$id}${reference}`,
  })
  exports[`validate${name}`] = id
}

process.stdout.write(browserSafeEsm(standaloneCode(ajv, exports)))
