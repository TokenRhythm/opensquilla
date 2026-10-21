import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { walkFiles } from './lib/fs-walk.mjs'

const require = createRequire(import.meta.url)
const ts = require('@typescript/typescript6')

const root = fileURLToPath(new URL('..', import.meta.url))
const srcRoot = join(root, 'src')
const allowedDesktopGlobal = new Set([
  'src/platform/capabilities.ts',
  'src/platform/desktop.ts',
  'src/vite-env.d.ts',
])
function unwrapExpression(node) {
  let current = node
  while (
    ts.isParenthesizedExpression(current)
    || ts.isAsExpression(current)
    || ts.isTypeAssertionExpression(current)
    || ts.isNonNullExpression(current)
  ) {
    current = current.expression
  }
  return current
}

function globalObjectName(node) {
  const current = unwrapExpression(node)
  return ts.isIdentifier(current) && new Set(['window', 'globalThis', 'self']).has(current.text)
}

function isDesktopGlobalAccess(node) {
  if (ts.isPropertyAccessExpression(node)) {
    return node.name.text === 'opensquillaDesktop' && globalObjectName(node.expression)
  }
  if (ts.isElementAccessExpression(node)) {
    const argument = node.argumentExpression
    return globalObjectName(node.expression)
      && ts.isStringLiteralLike(argument)
      && argument.text === 'opensquillaDesktop'
  }
  return false
}

function desktopGlobalAccesses(body, rel) {
  if (allowedDesktopGlobal.has(rel)) return 0
  const source = ts.createSourceFile(rel, body, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  let count = 0
  function visit(node) {
    if (isDesktopGlobalAccess(node)) count += 1
    ts.forEachChild(node, visit)
  }
  visit(source)
  return count
}
const stalePlatformPatterns = [
  'activeProfile',
  'cloudUrl',
  'getDesktopRpcConnection',
  'desktop:rpc-connection',
]

// Live-turn fold fence: the append-only turn log and its reducer are internal
// chat details. Keep their imports inside chat composables plus ChatView.
function isUnderChatComposables(rel) {
  const normalized = rel.split('\\').join('/')
  return normalized.startsWith('src/composables/chat/')
}
function isChatView(rel) {
  return rel.split('\\').join('/') === 'src/views/ChatView.vue'
}
const turnLogModulePatterns = [
  '@/composables/chat/useChatTurnLog',
  '@/utils/chat/foldTurn',
]

// Test files exercise the fenced modules directly (that is their job) and are
// not a runtime layer, so they are exempt from the import fence below.
function isTestFile(entry) {
  return /\.(test|spec)\.(ts|tsx)$/.test(entry)
}

function hasRuntimeVueDependency(body, rel) {
  const source = ts.createSourceFile(rel, body, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const isVue = node => node && ts.isStringLiteralLike(node)
    && (node.text === 'vue' || node.text.startsWith('vue/') || node.text.startsWith('@vue/'))
  function visit(node) {
    if (ts.isImportDeclaration(node) && isVue(node.moduleSpecifier)) {
      const clause = node.importClause
      if (!clause) return true
      if (!clause.isTypeOnly && (clause.name || !clause.namedBindings
        || ts.isNamespaceImport(clause.namedBindings)
        || clause.namedBindings.elements.length === 0
        || clause.namedBindings.elements.some(element => !element.isTypeOnly))) return true
    }
    if (ts.isExportDeclaration(node) && isVue(node.moduleSpecifier) && !node.isTypeOnly) {
      if (!node.exportClause || !ts.isNamedExports(node.exportClause)
        || node.exportClause.elements.length === 0
        || node.exportClause.elements.some(element => !element.isTypeOnly)) return true
    }
    if (ts.isCallExpression(node) && isVue(node.arguments[0])
      && (node.expression.kind === ts.SyntaxKind.ImportKeyword
        || (ts.isIdentifier(node.expression) && node.expression.text === 'require'))) return true
    return ts.forEachChild(node, visit)
  }
  return Boolean(visit(source))
}

const failures = []
for (const file of walkFiles(srcRoot, /\.(ts|vue)$/, { skipFile: isTestFile })) {
  const rel = relative(root, file).replace(/\\/g, '/')
  const body = readFileSync(file, 'utf8')
  if (rel === 'src/utils/chat/parkedPendingQueueCache.ts' && hasRuntimeVueDependency(body, rel)) {
    failures.push(`${rel}: Vue runtime dependencies belong in the composable; inject object normalization into the cache.`)
  }
  const desktopAccessCount = desktopGlobalAccesses(body, rel)
  if (desktopAccessCount > 0) {
    failures.push(
      `${rel}: Electron preload access must stay behind src/platform/. Found ${desktopAccessCount} AST property access(es).`,
    )
  }
  for (const pattern of stalePlatformPatterns) {
    if (body.includes(pattern)) {
      failures.push(`${rel}: stale desktop/cloud platform pattern found: "${pattern}".`)
    }
  }
  if (!isUnderChatComposables(rel) && !isChatView(rel)) {
    for (const moduleId of turnLogModulePatterns) {
      if (body.includes(moduleId)) {
        failures.push(`${rel}: live-turn log "${moduleId}" must stay within src/composables/chat/ or views/ChatView.vue.`)
      }
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join('\n'))
  process.exit(1)
}

console.log('Architecture guard passed.')
