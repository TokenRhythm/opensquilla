#!/usr/bin/env node
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  repositoryRoot, readContractInventory, readProductionTargets, targetIdentity, walkFiles,
} from './gateway_contract_inventory.mjs'

const require = createRequire(resolve(repositoryRoot, 'opensquilla-webui/package.json'))
const ts = require('typescript')
const { parse: parseVue } = require('@vue/compiler-sfc')
const normalized = path => path.replace(/\\/g, '/')
const isTest = path => /\.(test|spec)\.[cm]?[jt]sx?$/.test(path)
  || /(^|\/)(testing|__tests__|__mocks__)(\/|$)/.test(path)

function resolveImport(root, importer, specifier) {
  const clean = specifier.split(/[?#]/, 1)[0]
  if (clean.startsWith('@/')) return resolve(root, 'src', clean.slice(2))
  if (clean.startsWith('/@fs/')) {
    const absolute = clean.slice(5)
    return resolve(/^(?:\/|[A-Za-z]:\/)/.test(absolute) ? absolute : `/${absolute}`)
  }
  if (clean.startsWith('/')) return resolve(root, clean.slice(1))
  if (clean.startsWith('.')) return resolve(root, dirname(importer), clean)
  return null
}

/** Check reviewed role identities against syntax, never infer deletion permission. */
export function evaluateProductionTargets({ root = repositoryRoot, sources, manifest } = {}) {
  const inventory = readContractInventory(root)
  const policy = manifest ?? JSON.parse(readFileSync(join(root, 'contracts/gateway/v4/production-targets.json'), 'utf8'))
  const selected = readProductionTargets(inventory, policy)
  const expected = new Set(selected.map(target => targetIdentity(target.kind, target.wireName, target.role)))
  const webui = join(root, 'opensquilla-webui')
  const generated = join(webui, 'src/contracts/generated')
  const modules = new Map(inventory.map(contract => [contract.stem, contract]))
  const observed = new Set()
  const failures = []
  const inputs = sources ?? walkFiles(join(webui, 'src'))
    .filter(path => /\.(vue|[cm]?[jt]sx?)$/.test(path))
    .filter(path => !normalized(path).includes('/contracts/generated/'))
    .map(path => ({ path: normalized(relative(webui, path)), text: readFileSync(path, 'utf8') }))

  for (const input of inputs) {
    const path = normalized(input.path)
    if (isTest(path) || /\.d\.[cm]?ts$/.test(path) || path.startsWith('src/contracts/generated/')) continue
    let text = input.text
    if (path.endsWith('.vue')) {
      const parsed = parseVue(text, { filename: path })
      if (parsed.errors.length) {
        failures.push(`${path}: cannot parse Vue scripts for production target analysis`)
        continue
      }
      text = [parsed.descriptor.script?.content, parsed.descriptor.scriptSetup?.content].filter(Boolean).join('\n')
    }
    const kind = /\.[jt]sx$/.test(path) ? ts.ScriptKind.TSX : ts.ScriptKind.TS
    const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, kind)
    const failure = message => failures.push(`${path}: ${message}`)
    if (source.parseDiagnostics.length) failure('cannot parse production source reliably')
    function validatorModule(specifier) {
      const target = resolveImport(webui, path, specifier)
      if (target && normalized(target).startsWith(`${normalized(join(root, 'scripts/contracts'))}/`)) {
        failure('production cannot load test-only Contract tooling')
      }
      if (!target || !normalized(target).startsWith(`${normalized(generated)}/`)) return null
      const match = target.match(/[/\\]([^/\\]+)Validators(?:\.[cm]js)?$/)
      if (!match) return null
      const contract = modules.get(match[1])
      if (!contract) failure(`unknown generated validator module: ${specifier}`)
      return contract ?? { targets: [] }
    }
    function visit(node) {
      if (ts.isImportDeclaration(node) && ts.isStringLiteralLike(node.moduleSpecifier)) {
        const contract = validatorModule(node.moduleSpecifier.text)
        if (contract && !node.importClause?.isTypeOnly) {
          const bindings = node.importClause?.namedBindings
          if (!bindings || !ts.isNamedImports(bindings) || node.importClause.name) {
            failure('production validators require named imports')
          } else for (const element of bindings.elements) {
            if (element.isTypeOnly) continue
            const name = (element.propertyName ?? element.name).text
            const target = contract.targets.find(target => target.exportName === name)
            if (!target) failure(`unknown validator export: ${name}`)
            else observed.add(targetIdentity(contract.kind, contract.wireName, target.role))
          }
        }
      }
      if (ts.isExportDeclaration(node) && node.moduleSpecifier && !node.isTypeOnly
        && ts.isStringLiteralLike(node.moduleSpecifier) && validatorModule(node.moduleSpecifier.text)) {
        failure('production validators require direct named imports, not re-exports')
      }
      if (ts.isIdentifier(node) && node.text === 'require') failure('production require is not auditable; use named imports')
      if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
        const argument = node.arguments[0]
        if (!argument || !ts.isStringLiteralLike(argument)) failure('computed dynamic import is not statically auditable')
        else if (validatorModule(argument.text)) failure('dynamic validator imports are forbidden')
      }
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
        && /^glob(?:Eager)?$/.test(node.expression.name.text)
        && node.expression.expression.getText(source) === 'import.meta') {
        const argument = node.arguments[0]
        const patterns = argument && ts.isArrayLiteralExpression(argument) ? argument.elements : [argument]
        for (const pattern of patterns) {
          if (!pattern || !ts.isStringLiteralLike(pattern)) {
            failure('computed glob imports are not statically auditable')
            continue
          }
          const prefix = pattern.text.replace(/^!/, '').split(/[*?{[]/, 1)[0]
          const base = resolveImport(webui, path, prefix)
          if (!base || normalized(generated).startsWith(normalized(base))
            || normalized(base).startsWith(normalized(generated))) failure('dynamic generated Contract glob is forbidden')
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
  }
  for (const target of observed) if (!expected.has(target)) failures.push(`unapproved production target: ${target}`)
  for (const target of expected) if (!observed.has(target)) failures.push(`unused production target: ${target}`)
  return { failures: [...new Set(failures)].sort(), targets: [...observed].sort() }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = evaluateProductionTargets()
    if (result.failures.length) {
      for (const failure of result.failures) console.error(failure)
      process.exitCode = 1
    } else console.log(`Production validator policy passed (${result.targets.length} approved roles).`)
  } catch (error) {
    console.error(error.message)
    process.exitCode = 1
  }
}
