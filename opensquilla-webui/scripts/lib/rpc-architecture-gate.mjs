import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

import { walkFiles } from './fs-walk.mjs'
import {
  boundaryReexportViolation,
  generatedContractImportViolation,
  localBoundaryReexportViolations,
  moduleReferenceSpecifier,
  privateGatewayTransportImportViolation,
} from './rpc-architecture-imports.mjs'
import {
  collectRpcTransportOperations,
  TRACKED_RPC_MEMBERS,
} from './rpc-symbol-provenance.mjs'
import { transportDebtLanes } from '../rpc-debt/index.mjs'

const root = fileURLToPath(new URL('../..', import.meta.url))
const srcRoot = join(root, 'src')
const require = createRequire(import.meta.url)
const ts = require('typescript')
const failures = []
const trackedKinds = new Set(
  TRACKED_RPC_MEMBERS.flatMap(member => [member, `${member}Reference`]),
)

const normalized = path => path.replace(/\\/g, '/')
const isTestFile = rel => /\.(test|spec)\.(?:[cm]?[jt]sx?)$/.test(rel)
const isGatewayAdapter = rel => rel.startsWith('src/adapters/gateway/')
const isGeneratedContract = rel => rel.startsWith('src/contracts/generated/')
const isTransportImplementation = rel => (
  rel === 'src/stores/rpc.ts' || rel === 'src/lib/rpc.ts'
)

function scriptBody(rel, body) {
  if (!rel.endsWith('.vue')) return body
  return [...body.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1])
    .join('\n')
}

function sourceKind(rel) {
  if (/\.tsx$/.test(rel)) return ts.ScriptKind.TSX
  if (/\.jsx$/.test(rel)) return ts.ScriptKind.JSX
  if (/\.(?:[cm]?js)$/.test(rel)) return ts.ScriptKind.JS
  return ts.ScriptKind.TS
}

function sourceFile(rel, body) {
  return ts.createSourceFile(
    rel,
    scriptBody(rel, body),
    ts.ScriptTarget.Latest,
    true,
    sourceKind(rel),
  )
}

function increment(map, key, amount = 1) {
  map.set(key, (map.get(key) ?? 0) + amount)
}

const laneByFile = new Map()
const expectedByKind = new Map([...trackedKinds].map(kind => [kind, new Map()]))
for (const { lane, debt } of transportDebtLanes) {
  if (!lane || typeof lane !== 'string') {
    failures.push('RPC debt lane has no stable name.')
    continue
  }
  for (const [rel, record] of Object.entries(debt)) {
    const previous = laneByFile.get(rel)
    if (previous) {
      failures.push(`${rel}: RPC debt is owned by both ${previous} and ${lane}.`)
      continue
    }
    laneByFile.set(rel, lane)
    for (const [kind, count] of Object.entries(record)) {
      if (!trackedKinds.has(kind)) {
        failures.push(`${rel}: ${lane} contains unknown RPC debt kind ${kind}.`)
      } else if (!Number.isInteger(count) || count <= 0) {
        failures.push(`${rel}: ${lane} ${kind} debt must be a positive integer.`)
      } else {
        expectedByKind.get(kind).set(rel, count)
      }
    }
  }
}

const sources = []
for (const file of walkFiles(srcRoot, /\.(?:vue|[cm]?[jt]sx?)$/)) {
  const rel = normalized(relative(root, file))
  sources.push({ rel, source: sourceFile(rel, readFileSync(file, 'utf8')) })
}

for (const { rel, source } of sources) {
  failures.push(...localBoundaryReexportViolations(ts, source, {
    root,
    importer: rel,
  }))
  function visit(node) {
    const specifier = moduleReferenceSpecifier(ts, node)
    if (specifier) {
      const generatedFailure = generatedContractImportViolation({
        root, importer: rel, specifier,
      })
      if (generatedFailure) failures.push(generatedFailure)
      const privateFailure = privateGatewayTransportImportViolation({
        root, importer: rel, specifier,
      })
      if (privateFailure) failures.push(privateFailure)
      if (ts.isExportDeclaration(node)) {
        const reexportFailure = boundaryReexportViolation({
          root, importer: rel, specifier,
        })
        if (reexportFailure) failures.push(reexportFailure)
      }
    }
    if (
      ts.isStringLiteralLike(node)
      && node.text === 'sessions.list'
      && !isGatewayAdapter(rel)
      && !isTestFile(rel)
      && !isGeneratedContract(rel)
    ) {
      failures.push(`${rel}: sessions.list wire literal is allowed only in its Contract Adapter.`)
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
}

const productionSources = sources.filter(({ rel }) => (
  !isTestFile(rel) && !isGeneratedContract(rel)
))
const actualByKind = new Map([...trackedKinds].map(kind => [kind, new Map()]))
for (const operation of collectRpcTransportOperations({
  ts,
  root,
  sources: productionSources,
})) {
  if (isGatewayAdapter(operation.rel) || isTransportImplementation(operation.rel)) continue
  increment(actualByKind.get(operation.kind), operation.rel)
}

function compareExactLedger(kind, expected, actual) {
  for (const [rel, count] of actual) {
    const approved = expected.get(rel)
    if (approved === undefined) {
      failures.push(`${rel}: unexpected raw RPC ${kind} (${count}); add a domain Adapter instead.`)
    } else if (approved !== count) {
      failures.push(`${rel}: raw RPC ${kind} count is ${count}; lane debt requires ${approved}.`)
    }
  }
  for (const [rel, count] of expected) {
    if (!actual.has(rel)) {
      failures.push(`${rel}: stale raw RPC ${kind} debt (${count}); remove it from its lane file.`)
    }
  }
}

for (const kind of trackedKinds) {
  compareExactLedger(kind, expectedByKind.get(kind), actualByKind.get(kind))
}

const debtByLane = new Map(transportDebtLanes.map(({ lane }) => [lane, 0]))
for (const ledger of actualByKind.values()) {
  for (const [rel, count] of ledger) {
    const lane = laneByFile.get(rel)
    if (lane) increment(debtByLane, lane, count)
  }
}

if (failures.length > 0) {
  console.error(failures.join('\n'))
  process.exit(1)
}

const total = [...actualByKind.values()]
  .flatMap(ledger => [...ledger.values()])
  .reduce((sum, count) => sum + count, 0)
const laneSummary = [...debtByLane]
  .map(([lane, count]) => `${lane}=${count}`)
  .join(', ')
console.log(`RPC architecture guard passed (${total} exact debt operations; ${laneSummary}).`)
