import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { exactTransportDebtFailures } from './exact-transport-debt.mjs'
import { walkFiles } from './fs-walk.mjs'
import {
  collectHttpBoundaryOperations,
  TRACKED_HTTP_KINDS,
} from './http-architecture-provenance.mjs'
import {
  boundaryReexportViolation,
  collectBoundaryArchitectureViolations,
  gatewayAdapterRpcStoreImportViolation,
  generatedContractImportViolation,
  moduleReferenceSpecifier,
  privateGatewayTransportImportViolation,
} from './rpc-architecture-imports.mjs'
import {
  collectRpcTransportOperations,
  TRACKED_RPC_MEMBERS,
} from './rpc-symbol-provenance.mjs'
import { createRpcAnalysisProgram } from './rpc-typescript-program.mjs'
import { transportDebtLanes } from '../rpc-debt/index.mjs'

const defaultRoot = fileURLToPath(new URL('../..', import.meta.url))
const require = createRequire(import.meta.url)
const ts = require('typescript')
const trackedRpcKinds = new Set(
  TRACKED_RPC_MEMBERS.flatMap(member => [member, `${member}Reference`]),
)
const trackedHttpKinds = new Set(TRACKED_HTTP_KINDS)
const trackedKinds = new Set([...trackedRpcKinds, ...trackedHttpKinds])

const normalized = path => path.replace(/\\/g, '/')
const isTestFile = rel => /\.(test|spec)\.(?:[cm]?[jt]sx?)$/.test(rel)
const isGatewayAdapter = rel => rel.startsWith('src/adapters/gateway/')
const isGeneratedContract = rel => rel.startsWith('src/contracts/generated/')
const isRpcTransportImplementation = rel => (
  rel === 'src/stores/rpc.ts'
  || rel === 'src/lib/rpc.ts'
  || rel === 'src/adapters/gateway/privateTransports.ts'
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

/** Evaluate RPC and HTTP migration debt from one source scan and one ledger. */
export function evaluateRpcArchitectureGate({
  root = defaultRoot,
  debtLanes = transportDebtLanes,
} = {}) {
  const failures = []
  const laneByFile = new Map()
  const expectedByKind = new Map([...trackedKinds].map(kind => [kind, new Map()]))
  for (const { lane, debt } of debtLanes) {
    if (!lane || typeof lane !== 'string') {
      failures.push('Transport debt lane has no stable name.')
      continue
    }
    for (const [rel, record] of Object.entries(debt)) {
      const previous = laneByFile.get(rel)
      if (previous) {
        failures.push(`${rel}: transport debt is owned by both ${previous} and ${lane}.`)
        continue
      }
      laneByFile.set(rel, lane)
      for (const [kind, count] of Object.entries(record)) {
        if (!trackedKinds.has(kind)) {
          failures.push(`${rel}: ${lane} contains unknown transport debt kind ${kind}.`)
        } else if (!Number.isInteger(count) || count <= 0) {
          failures.push(`${rel}: ${lane} ${kind} debt must be a positive integer.`)
        } else {
          expectedByKind.get(kind).set(rel, count)
        }
      }
    }
  }

  const sources = []
  for (const file of walkFiles(join(root, 'src'), /\.(?:vue|[cm]?[jt]sx?)$/)) {
    const rel = normalized(relative(root, file))
    sources.push({ rel, source: sourceFile(rel, readFileSync(file, 'utf8')) })
  }

  for (const { rel, source } of sources) {
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
        const storeFailure = gatewayAdapterRpcStoreImportViolation({
          root, importer: rel, specifier,
        })
        if (storeFailure) failures.push(storeFailure)
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
  const sourceAnalysis = createRpcAnalysisProgram({ ts, root, sources })
  failures.push(...collectBoundaryArchitectureViolations({
    ts,
    root,
    sources,
    analysis: sourceAnalysis,
  }))

  const productionSources = sources.filter(({ rel }) => (
    !isTestFile(rel) && !isGeneratedContract(rel)
  ))
  const actualByKind = new Map([...trackedKinds].map(kind => [kind, new Map()]))
  for (const operation of collectRpcTransportOperations({
    ts,
    root,
    sources: productionSources,
    analysis: sourceAnalysis,
  })) {
    if (isRpcTransportImplementation(operation.rel)) continue
    increment(actualByKind.get(operation.kind), operation.rel)
  }
  for (const operation of collectHttpBoundaryOperations({
    ts,
    sources: productionSources,
  })) {
    if (isGatewayAdapter(operation.rel) || isRpcTransportImplementation(operation.rel)) continue
    increment(actualByKind.get(operation.kind), operation.rel)
  }

  for (const kind of trackedKinds) {
    failures.push(...exactTransportDebtFailures(
      kind,
      expectedByKind.get(kind),
      actualByKind.get(kind),
    ))
  }

  const debtByLane = new Map(debtLanes.map(({ lane }) => [lane, 0]))
  for (const ledger of actualByKind.values()) {
    for (const [rel, count] of ledger) {
      const lane = laneByFile.get(rel)
      if (lane) increment(debtByLane, lane, count)
    }
  }

  const rpcTotal = [...trackedRpcKinds]
    .flatMap(kind => [...actualByKind.get(kind).values()])
    .reduce((sum, count) => sum + count, 0)
  const httpTotal = [...trackedHttpKinds]
    .flatMap(kind => [...actualByKind.get(kind).values()])
    .reduce((sum, count) => sum + count, 0)
  return {
    failures,
    total: rpcTotal + httpTotal,
    rpcTotal,
    httpTotal,
    debtByLane,
  }
}

function runCli() {
  const { failures, total, rpcTotal, httpTotal, debtByLane } = evaluateRpcArchitectureGate()
  if (failures.length > 0) {
    console.error(failures.join('\n'))
    process.exitCode = 1
    return
  }
  const laneSummary = [...debtByLane]
    .map(([lane, count]) => `${lane}=${count}`)
    .join(', ')
  console.log(
    `Transport architecture guard passed (${total} exact debt operations; `
    + `RPC=${rpcTotal}, HTTP=${httpTotal}; ${laneSummary}).`,
  )
}

const invokedAs = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : null
if (invokedAs === import.meta.url) runCli()
