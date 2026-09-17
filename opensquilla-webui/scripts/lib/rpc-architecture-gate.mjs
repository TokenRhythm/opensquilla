import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

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
  resolveSourceImport,
} from './rpc-architecture-imports.mjs'
import {
  collectRpcTransportOperations,
  TRACKED_RPC_MEMBERS,
} from './rpc-symbol-provenance.mjs'
import { createRpcAnalysisProgram } from './rpc-typescript-program.mjs'

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
const isTestingSupport = rel => rel.startsWith('src/testing/')
const isGatewayAdapter = rel => rel.startsWith('src/adapters/gateway/')
const isGeneratedContract = rel => rel.startsWith('src/contracts/generated/')
const isPrivateHttpTransportImplementation = rel => (
  rel === 'src/adapters/gateway/privateHttpTransport.ts'
)
const isStaticAssetTransportImplementation = rel => rel === 'src/platform/staticAssets.ts'
const isRawConversationWireName = value => (
  value.startsWith('session.event.') || /^task\.[a-z0-9_.-]+$/i.test(value)
)
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

function hasEsmNamedValueExport(source, name) {
  for (const statement of source.statements) {
    if (
      ts.isExportDeclaration(statement)
      && !statement.isTypeOnly
      && statement.exportClause
      && ts.isNamedExports(statement.exportClause)
      && statement.exportClause.elements.some(element => (
        !element.isTypeOnly && element.name.text === name
      ))
    ) {
      return true
    }
    const modifiers = ts.canHaveModifiers(statement)
      ? ts.getModifiers(statement) ?? []
      : []
    if (!modifiers.some(modifier => modifier.kind === ts.SyntaxKind.ExportKeyword)) continue
    if (
      (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement))
      && statement.name?.text === name
    ) {
      return true
    }
    if (
      ts.isVariableStatement(statement)
      && statement.declarationList.declarations.some(declaration => (
        ts.isIdentifier(declaration.name) && declaration.name.text === name
      ))
    ) {
      return true
    }
  }
  return false
}

/** Evaluate forbidden RPC and HTTP operations outside their private boundaries. */
export function evaluateRpcArchitectureGate({ root = defaultRoot } = {}) {
  const failures = []

  const sources = []
  for (const file of walkFiles(join(root, 'src'), /\.(?:vue|[cm]?[jt]sx?)$/).sort()) {
    const rel = normalized(relative(root, file))
    sources.push({ rel, source: sourceFile(rel, readFileSync(file, 'utf8')) })
  }

  for (const { rel, source } of sources) {
    function visit(node) {
      const specifier = moduleReferenceSpecifier(ts, node)
      if (specifier) {
        const target = resolveSourceImport(root, rel, specifier)
        const targetRel = target ? normalized(relative(root, target)).replace(/\.(?:vue|[cm]?[jt]sx?)$/, '') : ''
        if (
          targetRel === 'src/lib/rpc'
          && rel !== 'src/stores/rpc.ts'
          && rel !== 'src/adapters/gateway/privateTransports.ts'
          && !isTestFile(rel)
          && !isTestingSupport(rel)
        ) {
          failures.push(`${rel}: lib/rpc may be imported only by the RPC store or private Gateway transport.`)
        }
        if (
          targetRel.startsWith('src/adapters/gateway/')
          && !isGatewayAdapter(rel)
          && rel !== 'src/main.ts'
          && !isTestFile(rel)
          && !isTestingSupport(rel)
        ) {
          failures.push(`${rel}: Gateway Adapters may be imported only by the composition root or tests.`)
        }
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
      if (
        ts.isStringLiteralLike(node)
        && node.text === 'sessions.search'
        && !isGatewayAdapter(rel)
        && !isTestFile(rel)
        && !isGeneratedContract(rel)
      ) {
        failures.push(`${rel}: sessions.search wire literal is allowed only in its Contract Adapter.`)
      }
      if (
        ts.isStringLiteralLike(node)
        && isRawConversationWireName(node.text)
        && !isGatewayAdapter(rel)
        && !isTestFile(rel)
        && !isGeneratedContract(rel)
      ) {
        failures.push(
          `${rel}: ${node.text} wire literal is allowed only in a Gateway Adapter, generated Contract, or test.`,
        )
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
  }
  const sourceAnalysis = createRpcAnalysisProgram({ ts, root, sources })
  const canonicalStore = sources.find(({ rel }) => rel === 'src/stores/rpc.ts')
  if (
    canonicalStore
    && (
      !hasEsmNamedValueExport(canonicalStore.source, 'useRpcStore')
      || !sourceAnalysis.exportedSymbol('src/stores/rpc.ts', 'useRpcStore')
    )
  ) {
    failures.push(
      'src/stores/rpc.ts: RPC provenance seed must remain an ESM named export "useRpcStore".',
    )
  }
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
    root,
    sources: productionSources,
    analysis: sourceAnalysis,
  })) {
    if (
      isPrivateHttpTransportImplementation(operation.rel)
      || isStaticAssetTransportImplementation(operation.rel)
    ) continue
    increment(actualByKind.get(operation.kind), operation.rel)
  }

  for (const kind of [...trackedKinds].sort()) {
    for (const [rel, count] of [...actualByKind.get(kind)].sort()) {
      failures.push(
        `${rel}: unexpected raw transport ${kind} (${count}); add a domain Adapter instead.`,
      )
    }
  }

  const rpcTotal = [...trackedRpcKinds]
    .flatMap(kind => [...actualByKind.get(kind).values()])
    .reduce((sum, count) => sum + count, 0)
  const httpTotal = [...trackedHttpKinds]
    .flatMap(kind => [...actualByKind.get(kind).values()])
    .reduce((sum, count) => sum + count, 0)
  return {
    failures: [...new Set(failures)].sort(),
    total: rpcTotal + httpTotal,
    rpcTotal,
    httpTotal,
  }
}

function runCli() {
  const { failures, rpcTotal, httpTotal } = evaluateRpcArchitectureGate()
  if (failures.length > 0) {
    console.error(failures.join('\n'))
    process.exitCode = 1
    return
  }
  console.log(
    'Transport boundary guard passed '
    + `(outside allowed boundaries: RPC=${rpcTotal}, HTTP=${httpTotal}; `
    + 'raw HTTP is confined to the private transport and static assets).',
  )
}

const invokedAs = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : null
if (invokedAs === import.meta.url) runCli()
