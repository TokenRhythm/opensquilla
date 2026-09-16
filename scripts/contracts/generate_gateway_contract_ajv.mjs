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

function browserSafeEsm(source, esm) {
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

/** Resolve logical roles without depending on generated filenames or aliases. */
export function contractTargets(schema, roles) {
  const method = schema['x-opensquilla-method']
  const event = schema['x-opensquilla-event']
  if ((method === undefined) === (event === undefined)) {
    throw new Error('Contract must declare exactly one method or event metadata block')
  }
  const metadata = method ?? event
  const available = method === undefined
    ? ['frame', 'payload'].filter(role => Object.hasOwn(event, role))
    : ['request', 'params', 'response', 'result']
  if (method === undefined && available.length !== 1) {
    throw new Error('event Contract must declare exactly one of frame or payload')
  }
  if (roles !== undefined && (
    !Array.isArray(roles) || roles.length === 0 || new Set(roles).size !== roles.length
    || roles.some(role => !available.includes(role))
  )) {
    throw new Error('validator roles must be unique declared Contract roles')
  }
  return available.filter(role => roles === undefined || roles.includes(role)).map(role => {
    const reference = metadata[role]
    const definition = directDefinitionName(reference)
    if (!Object.hasOwn(schema.$defs ?? {}, definition)) {
      throw new Error(`missing validator definition: ${definition}`)
    }
    return { role, reference, definition, exportName: `validate${definition}` }
  })
}

/** Compile selected entry points; schema assertions and AJV options stay identical. */
export function compileContract(schema, { esm = true, roles } = {}) {
  const targets = contractTargets(schema, roles)
  const ajv = new Ajv2020({
    allErrors: true,
    strict: true,
    strictRequired: false,
    allowUnionTypes: true,
    code: { esm, source: true, optimize: true },
  })
  for (const keyword of [...collectExtensionKeywords(schema)].sort()) {
    ajv.addKeyword({ keyword, valid: true })
  }
  ajv.addSchema(schema)
  const exports = {}
  for (const { reference, definition, exportName } of targets) {
    const id = `urn:opensquilla:contract:v4:${definition}`
    if (Object.hasOwn(exports, exportName)) throw new Error(`duplicate target: ${exportName}`)
    ajv.addSchema({
      $id: id,
      $schema: schema.$schema,
      $ref: `${schema.$id}${reference}`,
    })
    exports[exportName] = id
  }
  return browserSafeEsm(standaloneCode(ajv, exports), esm)
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const schemaPath = process.argv[2]
  if (!schemaPath) throw new Error('usage: generate_gateway_contract_ajv.mjs <schema> [--esm] [--roles result,params]')
  const rolesIndex = process.argv.indexOf('--roles')
  const roles = rolesIndex === -1 ? undefined : (process.argv[rolesIndex + 1] ?? '').split(',')
  const schema = JSON.parse(readFileSync(schemaPath, 'utf8'))
  process.stdout.write(compileContract(schema, { esm: process.argv.includes('--esm'), roles }))
}
