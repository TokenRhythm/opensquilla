import { createRpcAnalysisProgram } from './rpc-typescript-program.mjs'

export const TRACKED_RPC_MEMBERS = [
  'call',
  'on',
  'supportsMethod',
  'supportsEvent',
  'markMethodUnavailable',
  'waitForConnection',
]

const SEED_EXPORTS = new Map([
  ['src/stores/rpc::useRpcStore', 'factory'],
  ['src/lib/rpc::RpcClient', 'client-type'],
  ['src/lib/rpc::RpcCallOptions', 'rpc-support-type'],
  ['src/lib/rpc::RpcConnectionWaitOptions', 'rpc-support-type'],
  ['src/lib/rpc::RpcEventHandler', 'rpc-support-type'],
  ['src/adapters/gateway/privateTransports::createPrivateGatewayTransports', 'factory'],
  ['src/adapters/gateway/privateTransports::RpcTransport', 'client-type'],
  ['src/adapters/gateway/privateTransports::EventTransport', 'client-type'],
  ['src/adapters/gateway/privateTransports::GatewayTransports', 'client-type'],
])

const normalized = path => path.replace(/\\/g, '/')
const moduleKey = path => normalized(path).replace(/\.(?:vue|[cm]?[jt]sx?)$/, '')

function unwrap(ts, expression) {
  let current = expression
  while (
    ts.isParenthesizedExpression(current)
    || ts.isAsExpression(current)
    || ts.isTypeAssertionExpression(current)
    || ts.isNonNullExpression(current)
    || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(current))
    || ts.isAwaitExpression(current)
  ) current = current.expression
  return current
}

function propertyName(ts, node) {
  if (!node) return null
  if (
    ts.isIdentifier(node)
    || ts.isPrivateIdentifier(node)
    || ts.isStringLiteralLike(node)
    || ts.isNumericLiteral(node)
  ) {
    return node.text
  }
  return null
}

function memberAccess(ts, expression) {
  const current = unwrap(ts, expression)
  if (ts.isPropertyAccessExpression(current)) {
    return { receiver: current.expression, member: current.name.text }
  }
  if (
    ts.isElementAccessExpression(current)
    && current.argumentExpression
    && (
      ts.isStringLiteralLike(current.argumentExpression)
      || ts.isNumericLiteral(current.argumentExpression)
    )
  ) {
    return { receiver: current.expression, member: current.argumentExpression.text }
  }
  return null
}

function isDirectInvocation(ts, node) {
  let current = node
  while (
    current.parent
    && (
      ts.isParenthesizedExpression(current.parent)
      || ts.isAsExpression(current.parent)
      || ts.isTypeAssertionExpression(current.parent)
      || ts.isNonNullExpression(current.parent)
      || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(current.parent))
    )
  ) current = current.parent
  return Boolean(
    current.parent
    && ts.isCallExpression(current.parent)
    && unwrap(ts, current.parent.expression) === node,
  )
}

function requireCall(ts, expression) {
  const current = unwrap(ts, expression)
  if (
    ts.isCallExpression(current)
    && ts.isIdentifier(current.expression)
    && current.expression.text === 'require'
    && current.arguments.length === 1
    && ts.isStringLiteralLike(current.arguments[0])
  ) return current.arguments[0].text
  return null
}

function shapeDepth(path) {
  return path ? path.split('.').length : 0
}

function selectShape(shape, property) {
  const selected = new Set()
  for (const suffix of shape) {
    if (suffix === property) selected.add('')
    else if (suffix.startsWith(`${property}.`)) selected.add(suffix.slice(property.length + 1))
  }
  return selected
}

const memberSeparator = '\0'
const encodeMember = (path, member) => `${path}${memberSeparator}${member}`
function decodeMember(encoded) {
  const index = encoded.lastIndexOf(memberSeparator)
  return { path: encoded.slice(0, index), member: encoded.slice(index + 1) }
}

function selectMemberShape(shape, property) {
  const selected = new Set()
  for (const encoded of shape) {
    const { path, member } = decodeMember(encoded)
    if (path === property) selected.add(encodeMember('', member))
    else if (path.startsWith(`${property}.`)) {
      selected.add(encodeMember(path.slice(property.length + 1), member))
    }
  }
  return selected
}

/**
 * Collect raw RPC operations from values proven to descend from a concrete
 * store/client seed.  Symbols, rather than identifier text, are the identity
 * of the data-flow graph; property paths form a finite lattice bounded by the
 * deepest path any source AST can actually consume.
 */
export function collectRpcTransportOperations({ ts, root, sources, analysis: suppliedAnalysis }) {
  const analysis = suppliedAnalysis ?? createRpcAnalysisProgram({ ts, root, sources })
  const { checker } = analysis
  const activeRels = new Set(sources.map(source => source.rel))
  const sourceEntries = analysis.sources.filter(source => activeRels.has(source.rel))
  const stateBySource = new Map()
  const stateByRel = new Map()
  const symbolIds = new WeakMap()
  let nextSymbolId = 1
  const functionIds = new WeakMap()
  let nextFunctionId = 1
  const classIds = new WeakMap()
  let nextClassId = 1

  function symbolId(symbol) {
    if (!symbol) return null
    let id = symbolIds.get(symbol)
    if (!id) {
      id = nextSymbolId
      nextSymbolId += 1
      symbolIds.set(symbol, id)
    }
    return id
  }

  function functionId(node) {
    let id = functionIds.get(node)
    if (!id) {
      id = nextFunctionId
      nextFunctionId += 1
      functionIds.set(node, id)
    }
    return id
  }

  function classId(node) {
    let id = classIds.get(node)
    if (!id) {
      id = nextClassId
      nextClassId += 1
      classIds.set(node, id)
    }
    return id
  }

  function enclosingClass(node) {
    let current = node?.parent ?? null
    while (current) {
      if (ts.isClassDeclaration(current) || ts.isClassExpression(current)) return current
      current = current.parent
    }
    return null
  }

  function instancePath(node) {
    const owner = enclosingClass(node)
    return owner ? `c${classId(owner)}` : null
  }

  function rawSymbolAt(node) {
    if (
      node
      && ts.isIdentifier(node)
      && ts.isShorthandPropertyAssignment(node.parent)
      && node.parent.name === node
    ) {
      return checker.getShorthandAssignmentValueSymbol(node.parent)
        ?? analysis.symbolAt(node)
    }
    return analysis.symbolAt(node)
  }

  function canonicalSymbolAt(node) {
    return analysis.canonicalSymbol(rawSymbolAt(node))
  }

  function stateForNode(node) {
    return stateBySource.get(node.getSourceFile()) ?? null
  }

  let maxPathDepth = 1
  const demandedShapePaths = new Set([''])

  function addDemandedPath(parts) {
    for (let start = 0; start < parts.length; start += 1) {
      const suffix = parts.slice(start).join('.')
      if (suffix) demandedShapePaths.add(suffix)
    }
  }

  function accessParts(expression) {
    const access = memberAccess(ts, expression)
    if (!access) return []
    return [...accessParts(access.receiver), access.member]
  }

  function collectBindingPaths(node, prefix = []) {
    if (!ts.isObjectBindingPattern(node) && !ts.isArrayBindingPattern(node)) return
    node.elements.forEach((element, index) => {
      if (!element || ts.isOmittedExpression(element)) return
      const member = ts.isObjectBindingPattern(node)
        ? propertyName(ts, element.propertyName ?? element.name)
        : String(index)
      if (!member) return
      const path = [...prefix, member]
      addDemandedPath(path)
      collectBindingPaths(element.name, path)
    })
  }

  function bindingDepth(node, depth = 0) {
    if (!ts.isObjectBindingPattern(node) && !ts.isArrayBindingPattern(node)) return depth
    let maximum = depth
    for (const element of node.elements) {
      if (!element || ts.isOmittedExpression(element)) continue
      maximum = Math.max(maximum, bindingDepth(element.name, depth + 1))
    }
    return maximum
  }
  function accessDepth(expression) {
    const current = unwrap(ts, expression)
    const access = memberAccess(ts, current)
    return access ? 1 + accessDepth(access.receiver) : 0
  }
  for (const { source } of sourceEntries) {
    function visit(node) {
      if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
        maxPathDepth = Math.max(maxPathDepth, accessDepth(node))
        addDemandedPath(accessParts(node))
      }
      if (ts.isVariableDeclaration(node) || ts.isParameter(node)) {
        maxPathDepth = Math.max(maxPathDepth, bindingDepth(node.name))
        collectBindingPaths(node.name)
      }
      ts.forEachChild(node, visit)
    }
    visit(source)
  }

  function bounded(path) {
    return !path || (
      shapeDepth(path) <= maxPathDepth
      && demandedShapePaths.has(path)
    )
  }

  function prependShape(prefix, shape) {
    const result = new Set()
    for (const suffix of shape) {
      const path = suffix ? `${prefix}.${suffix}` : prefix
      if (bounded(path)) result.add(path)
    }
    return result
  }

  function prependMemberShape(prefix, shape) {
    const result = new Set()
    for (const encoded of shape) {
      const { path, member } = decodeMember(encoded)
      const next = path ? `${prefix}.${path}` : prefix
      if (bounded(next)) result.add(encodeMember(next, member))
    }
    return result
  }

  const originBySymbol = new Map()
  for (const [seed, origin] of SEED_EXPORTS) {
    const separator = seed.lastIndexOf('::')
    const relKey = seed.slice(0, separator)
    const exported = seed.slice(separator + 2)
    const entry = sourceEntries.find(({ rel }) => moduleKey(rel) === relKey)
    if (!entry) continue
    const symbol = analysis.exportedSymbol(entry.rel, exported)
    if (!symbol) continue
    originBySymbol.set(symbol, origin)
    const canonical = analysis.canonicalSymbol(symbol)
    if (canonical) originBySymbol.set(canonical, origin)
  }

  const cjsExportsByRel = new Map()
  function cjsExportTarget(node, source) {
    if (!ts.isBinaryExpression(node) || node.operatorToken.kind !== ts.SyntaxKind.EqualsToken) {
      return null
    }
    const compact = node.left.getText(source).replace(/\s/g, '')
    if (compact === 'module.exports') return 'default'
    const dot = /^(?:module\.exports|exports)\.([A-Za-z_$][\w$]*)$/.exec(compact)
    if (dot) return dot[1]
    const bracket = /^(?:module\.exports|exports)\[['"]([^'"]+)['"]\]$/.exec(compact)
    return bracket?.[1] ?? null
  }

  for (const { rel, source } of sourceEntries) {
    const cjsExports = new Map()
    function collectCjs(node) {
      const exported = cjsExportTarget(node, source)
      if (exported) {
        if (
          exported === 'default'
          && ts.isObjectLiteralExpression(unwrap(ts, node.right))
        ) {
          for (const property of unwrap(ts, node.right).properties) {
            if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue
            const name = propertyName(ts, property.name)
            if (!name) continue
            cjsExports.set(
              name,
              ts.isShorthandPropertyAssignment(property) ? property.name : property.initializer,
            )
          }
        }
        cjsExports.set(exported, node.right)
      }
      ts.forEachChild(node, collectCjs)
    }
    collectCjs(source)
    cjsExportsByRel.set(rel, cjsExports)
  }

  function requireMember(expression, importerRel) {
    const current = unwrap(ts, expression)
    const direct = requireCall(ts, current)
    if (direct) {
      const target = analysis.resolveRecord(importerRel, direct)
      return target ? { rel: target.rel, exported: 'default' } : null
    }
    const access = memberAccess(ts, current)
    if (!access) return null
    const specifier = requireCall(ts, access.receiver)
    const target = specifier ? analysis.resolveRecord(importerRel, specifier) : null
    return target ? { rel: target.rel, exported: access.member } : null
  }

  function exportedExpression(rel, exported) {
    return cjsExportsByRel.get(rel)?.get(exported) ?? null
  }

  function symbolOrigin(symbol, seen = new Set()) {
    if (!symbol || seen.has(symbol)) return null
    const nextSeen = new Set(seen).add(symbol)
    const direct = originBySymbol.get(symbol)
    if (direct) return direct
    const canonical = analysis.canonicalSymbol(symbol)
    if (canonical && canonical !== symbol) {
      const origin = symbolOrigin(canonical, nextSeen)
      if (origin) return origin
    }
    for (const declaration of symbol.declarations ?? []) {
      if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
        const origin = expressionOrigin(declaration.initializer, nextSeen)
        if (origin) return origin
      }
      if (ts.isExportAssignment(declaration)) {
        const origin = expressionOrigin(declaration.expression, nextSeen)
        if (origin) return origin
      }
      if (ts.isBindingElement(declaration) && declaration.initializer) {
        const origin = expressionOrigin(declaration.initializer, nextSeen)
        if (origin) return origin
      }
    }
    return null
  }

  function expressionOrigin(expression, seen = new Set()) {
    const current = unwrap(ts, expression)
    const direct = symbolOrigin(rawSymbolAt(
      ts.isPropertyAccessExpression(current) ? current.name : current,
    ), seen)
    if (direct) return direct
    const state = stateForNode(current)
    const required = state ? requireMember(current, state.rel) : null
    if (required) {
      const exported = analysis.exportedSymbol(required.rel, required.exported)
      const origin = symbolOrigin(exported, seen)
      if (origin) return origin
      const cjsExpression = exportedExpression(required.rel, required.exported)
      if (cjsExpression) return expressionOrigin(cjsExpression, seen)
    }
    return null
  }

  const functions = new Set()
  for (const { source } of sourceEntries) {
    function collectFunctions(node) {
      if (
        ts.isFunctionDeclaration(node)
        || ts.isFunctionExpression(node)
        || ts.isArrowFunction(node)
        || ts.isMethodDeclaration(node)
      ) functions.add(node)
      ts.forEachChild(node, collectFunctions)
    }
    collectFunctions(source)
  }

  function callableFromSymbol(symbol, seen = new Set()) {
    if (!symbol || seen.has(symbol)) return null
    const nextSeen = new Set(seen).add(symbol)
    const canonical = analysis.canonicalSymbol(symbol)
    if (canonical && canonical !== symbol) {
      const found = callableFromSymbol(canonical, nextSeen)
      if (found) return found
    }
    for (const declaration of symbol.declarations ?? []) {
      if (ts.isFunctionDeclaration(declaration) || ts.isMethodDeclaration(declaration)) {
        return declaration
      }
      if (
        (
          ts.isVariableDeclaration(declaration)
          || ts.isPropertyDeclaration(declaration)
          || ts.isPropertyAssignment(declaration)
        )
        && declaration.initializer
      ) {
        const found = callableForExpression(declaration.initializer, nextSeen)
        if (found) return found
      }
      if (ts.isShorthandPropertyAssignment(declaration)) {
        const found = callableForExpression(declaration.name, nextSeen)
        if (found) return found
      }
      if (ts.isExportAssignment(declaration)) {
        const found = callableForExpression(declaration.expression, nextSeen)
        if (found) return found
      }
    }
    return null
  }

  function callableForExpression(expression, seen = new Set()) {
    const current = unwrap(ts, expression)
    if (
      ts.isFunctionExpression(current)
      || ts.isArrowFunction(current)
      || ts.isFunctionDeclaration(current)
    ) return current
    const symbolNode = ts.isPropertyAccessExpression(current) ? current.name : current
    const found = callableFromSymbol(rawSymbolAt(symbolNode), seen)
    if (found) return found
    const state = stateForNode(current)
    const required = state ? requireMember(current, state.rel) : null
    if (!required) return null
    const exported = analysis.exportedSymbol(required.rel, required.exported)
    const exportedCallable = callableFromSymbol(exported, seen)
    if (exportedCallable) return exportedCallable
    const cjsExpression = exportedExpression(required.rel, required.exported)
    return cjsExpression ? callableForExpression(cjsExpression, seen) : null
  }

  function pathForExpression(expression) {
    const current = unwrap(ts, expression)
    if (current.kind === ts.SyntaxKind.ThisKeyword) return instancePath(current)
    if (ts.isIdentifier(current)) {
      const id = symbolId(rawSymbolAt(current))
      return id ? `s${id}` : null
    }
    const access = memberAccess(ts, current)
    if (!access) return null
    const parent = pathForExpression(access.receiver)
    return parent ? `${parent}.${access.member}` : null
  }

  function hasModifier(node, kind) {
    return Boolean(ts.getModifiers(node)?.some(modifier => modifier.kind === kind))
  }

  function classStaticPath(owner) {
    if (owner.name) return pathForExpression(owner.name)
    if (
      ts.isClassExpression(owner)
      && ts.isVariableDeclaration(owner.parent)
      && ts.isIdentifier(owner.parent.name)
    ) return pathForExpression(owner.parent.name)
    return `c${classId(owner)}.static`
  }

  function classMemberPath(node) {
    const owner = enclosingClass(node)
    const name = propertyName(ts, node.name)
    if (!owner || !name) return null
    const base = hasModifier(node, ts.SyntaxKind.StaticKeyword)
      ? classStaticPath(owner)
      : `c${classId(owner)}`
    return base ? `${base}.${name}` : null
  }

  function isConstructorParameterProperty(node) {
    if (!ts.isParameter(node) || !ts.isConstructorDeclaration(node.parent)) return false
    return Boolean(ts.getModifiers(node)?.some(modifier => [
      ts.SyntaxKind.PublicKeyword,
      ts.SyntaxKind.ProtectedKeyword,
      ts.SyntaxKind.PrivateKeyword,
      ts.SyntaxKind.ReadonlyKeyword,
    ].includes(modifier.kind)))
  }

  function constructorForExpression(expression, seen = new Set()) {
    const current = unwrap(ts, expression)
    const symbolNode = ts.isPropertyAccessExpression(current) ? current.name : current
    const raw = rawSymbolAt(symbolNode)
    const symbol = analysis.canonicalSymbol(raw) ?? raw
    if (!symbol || seen.has(symbol)) return null
    const nextSeen = new Set(seen).add(symbol)
    for (const declaration of symbol.declarations ?? []) {
      let owner = null
      if (ts.isClassDeclaration(declaration) || ts.isClassExpression(declaration)) {
        owner = declaration
      } else if (
        ts.isVariableDeclaration(declaration)
        && declaration.initializer
        && ts.isClassExpression(unwrap(ts, declaration.initializer))
      ) {
        owner = unwrap(ts, declaration.initializer)
      } else if (ts.isExportAssignment(declaration)) {
        const found = constructorForExpression(declaration.expression, nextSeen)
        if (found) return found
      }
      if (owner) {
        return owner.members.find(member => ts.isConstructorDeclaration(member)) ?? null
      }
    }
    return null
  }

  function typeNameSymbol(node) {
    if (ts.isIdentifier(node)) return rawSymbolAt(node)
    if (ts.isQualifiedName(node)) return rawSymbolAt(node.right)
    if (ts.isPropertyAccessExpression(node)) return rawSymbolAt(node.name)
    return null
  }

  function typeIsDirectOrigin(node, desired, seen = new Set()) {
    if (!node || seen.has(node)) return false
    const nextSeen = new Set(seen).add(node)
    if (ts.isParenthesizedTypeNode(node) || ts.isTypeOperatorNode(node)) {
      return typeIsDirectOrigin(node.type, desired, nextSeen)
    }
    if (ts.isUnionTypeNode(node) || ts.isIntersectionTypeNode(node)) {
      return node.types.some(type => typeIsDirectOrigin(type, desired, nextSeen))
    }
    const name = ts.isTypeReferenceNode(node)
      ? node.typeName
      : ts.isExpressionWithTypeArguments(node)
        ? node.expression
        : null
    if (!name) return false
    const symbol = typeNameSymbol(name)
    if (symbolOrigin(symbol) === desired) return true
    const canonical = analysis.canonicalSymbol(symbol)
    for (const declaration of canonical?.declarations ?? []) {
      if (ts.isTypeAliasDeclaration(declaration)) {
        if (typeIsDirectOrigin(declaration.type, desired, nextSeen)) return true
      } else if (ts.isInterfaceDeclaration(declaration)) {
        for (const heritage of declaration.heritageClauses ?? []) {
          for (const type of heritage.types) {
            if (typeIsDirectOrigin(type, desired, nextSeen)) return true
          }
        }
      }
    }
    return false
  }

  function clientPropertyPaths(typeNode, depth = 0, seen = new Set()) {
    if (!typeNode || depth >= maxPathDepth || seen.has(typeNode)) return new Set()
    const result = new Set()
    const type = checker.getTypeAtLocation(typeNode)
    const nextSeen = new Set(seen).add(typeNode)
    for (const property of checker.getPropertiesOfType(type)) {
      const declaration = property.valueDeclaration ?? property.declarations?.[0]
      const propertyType = declaration?.type
      if (!declaration || !propertyType) continue
      const name = property.getName()
      if (typeIsDirectOrigin(propertyType, 'client-type')) {
        result.add(name)
        continue
      }
      for (const suffix of clientPropertyPaths(propertyType, depth + 1, nextSeen)) {
        const path = `${name}.${suffix}`
        if (bounded(path)) result.add(path)
      }
    }
    return result
  }

  for (const { rel, source } of sourceEntries) {
    const state = {
      rel,
      source,
      rpcObjects: new Set(),
      rpcPropertyPaths: new Set(),
      rpcCapabilities: new Map(),
      rpcMemberPaths: new Map(),
      returnShapes: new Map(),
      returnMemberShapes: new Map(),
    }
    stateBySource.set(source, state)
    stateByRel.set(rel, state)
  }

  function pathShape(state, path) {
    const shape = new Set()
    for (const candidate of [...state.rpcObjects, ...state.rpcPropertyPaths]) {
      if (candidate === path) shape.add('')
      else if (candidate.startsWith(`${path}.`)) shape.add(candidate.slice(path.length + 1))
    }
    return shape
  }

  function rpcShapeForExpression(expression) {
    const current = unwrap(ts, expression)
    const state = stateForNode(current)
    if (!state) return new Set()
    const path = pathForExpression(current)
    if (path) {
      const shape = pathShape(state, path)
      if (shape.size) return shape
    }
    const access = memberAccess(ts, current)
    if (access) return selectShape(rpcShapeForExpression(access.receiver), access.member)
    if (ts.isCallExpression(current)) {
      if (expressionOrigin(current.expression) === 'factory') return new Set([''])
      const target = callableForExpression(current.expression)
      return target
        ? new Set(stateForNode(target)?.returnShapes.get(functionId(target)) ?? [])
        : new Set()
    }
    if (ts.isNewExpression(current) && current.expression) {
      return expressionOrigin(current.expression) === 'client-type'
        ? new Set([''])
        : new Set()
    }
    if (ts.isObjectLiteralExpression(current)) {
      const shape = new Set()
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) {
          for (const suffix of rpcShapeForExpression(property.expression)) shape.add(suffix)
          continue
        }
        if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue
        const name = propertyName(ts, property.name)
        if (!name) continue
        const value = ts.isShorthandPropertyAssignment(property) ? property.name : property.initializer
        for (const suffix of prependShape(name, rpcShapeForExpression(value))) shape.add(suffix)
      }
      return shape
    }
    if (ts.isArrayLiteralExpression(current)) {
      const shape = new Set()
      current.elements.forEach((element, index) => {
        if (ts.isSpreadElement(element)) return
        for (const suffix of prependShape(String(index), rpcShapeForExpression(element))) {
          shape.add(suffix)
        }
      })
      return shape
    }
    if (ts.isConditionalExpression(current)) {
      return new Set([
        ...rpcShapeForExpression(current.whenTrue),
        ...rpcShapeForExpression(current.whenFalse),
      ])
    }
    return new Set()
  }

  function isRpcExpression(expression) {
    return rpcShapeForExpression(expression).has('')
  }

  function isRpcMemberReceiver(expression, member) {
    if (isRpcExpression(expression)) return true
    const state = stateForNode(expression)
    const path = pathForExpression(expression)
    if (state && path && state.rpcMemberPaths.get(path)?.has(member)) return true
    return rpcMemberShapeForExpression(expression).has(encodeMember('', member))
  }

  function rpcFunctionMembers(expression) {
    const current = unwrap(ts, expression)
    const symbol = rawSymbolAt(current)
    const state = stateForNode(current)
    const id = symbolId(symbol)
    if (state && id) {
      const known = state.rpcCapabilities.get(id)
      if (known) return new Set(known)
    }
    const access = memberAccess(ts, current)
    if (
      access
      && TRACKED_RPC_MEMBERS.includes(access.member)
      && isRpcMemberReceiver(access.receiver, access.member)
    ) return new Set([access.member])
    if (
      ts.isArrowFunction(current)
      || ts.isFunctionExpression(current)
      || ts.isMethodDeclaration(current)
    ) {
      const members = new Set()
      function visit(node) {
        if (ts.isFunctionLike(node) && node !== current) return
        const nested = memberAccess(ts, node)
        if (
          nested
          && TRACKED_RPC_MEMBERS.includes(nested.member)
          && isRpcMemberReceiver(nested.receiver, nested.member)
        ) members.add(nested.member)
        ts.forEachChild(node, visit)
      }
      visit(current)
      return members
    }
    return new Set()
  }

  function rpcMemberShapeForExpression(expression) {
    const current = unwrap(ts, expression)
    const state = stateForNode(current)
    if (!state) return new Set()
    const path = pathForExpression(current)
    if (path) {
      const shape = new Set()
      for (const [candidate, members] of state.rpcMemberPaths) {
        if (candidate !== path && !candidate.startsWith(`${path}.`)) continue
        const suffix = candidate === path ? '' : candidate.slice(path.length + 1)
        for (const member of members) shape.add(encodeMember(suffix, member))
      }
      if (shape.size) return shape
    }
    const access = memberAccess(ts, current)
    if (access) return selectMemberShape(
      rpcMemberShapeForExpression(access.receiver),
      access.member,
    )
    if (ts.isCallExpression(current)) {
      const target = callableForExpression(current.expression)
      return target
        ? new Set(stateForNode(target)?.returnMemberShapes.get(functionId(target)) ?? [])
        : new Set()
    }
    if (ts.isObjectLiteralExpression(current)) {
      const shape = new Set()
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) {
          for (const encoded of rpcMemberShapeForExpression(property.expression)) shape.add(encoded)
          continue
        }
        if (
          !ts.isPropertyAssignment(property)
          && !ts.isShorthandPropertyAssignment(property)
          && !ts.isMethodDeclaration(property)
        ) continue
        const name = propertyName(ts, property.name)
        if (!name) continue
        const value = ts.isShorthandPropertyAssignment(property)
          ? property.name
          : ts.isMethodDeclaration(property)
            ? property
            : property.initializer
        for (const encoded of prependMemberShape(name, rpcMemberShapeForExpression(value))) {
          shape.add(encoded)
        }
        if (TRACKED_RPC_MEMBERS.includes(name)) {
          const forwarded = rpcFunctionMembers(value)
          if (forwarded.has(name)) shape.add(encodeMember('', name))
        }
      }
      return shape
    }
    if (ts.isArrayLiteralExpression(current)) {
      const shape = new Set()
      current.elements.forEach((element, index) => {
        if (ts.isSpreadElement(element)) return
        for (const encoded of prependMemberShape(
          String(index),
          rpcMemberShapeForExpression(element),
        )) shape.add(encoded)
      })
      return shape
    }
    if (ts.isConditionalExpression(current)) {
      return new Set([
        ...rpcMemberShapeForExpression(current.whenTrue),
        ...rpcMemberShapeForExpression(current.whenFalse),
      ])
    }
    return new Set()
  }

  function addSetValue(set, value) {
    if (value === null || value === undefined || set.has(value)) return false
    set.add(value)
    return true
  }

  function addMapSetValue(map, key, value) {
    let values = map.get(key)
    if (!values) {
      values = new Set()
      map.set(key, values)
    }
    return addSetValue(values, value)
  }

  function bindShapeAtPath(state, path, shape) {
    if (!path) return false
    let changed = false
    for (const suffix of shape) {
      if (!bounded(suffix)) continue
      const target = suffix ? `${path}.${suffix}` : path
      changed = addSetValue(
        suffix ? state.rpcPropertyPaths : state.rpcObjects,
        target,
      ) || changed
    }
    return changed
  }

  function bindMemberShapeAtPath(state, path, shape) {
    if (!path) return false
    let changed = false
    const grouped = new Map()
    for (const encoded of shape) {
      const decoded = decodeMember(encoded)
      if (!bounded(decoded.path)) continue
      const target = decoded.path ? `${path}.${decoded.path}` : path
      let members = grouped.get(target)
      if (!members) {
        members = new Set()
        grouped.set(target, members)
      }
      members.add(decoded.member)
    }
    for (const [target, incoming] of grouped) {
      let members = state.rpcMemberPaths.get(target)
      if (
        !incoming.has('call')
        && !members?.has('call')
        && !state.rpcObjects.has(target)
      ) continue
      if (!members) {
        members = new Set()
        state.rpcMemberPaths.set(target, members)
      }
      for (const member of incoming) changed = addSetValue(members, member) || changed
    }
    return changed
  }

  function bindPattern(state, pattern, shape, memberShape = new Set()) {
    if (ts.isIdentifier(pattern)) {
      const id = symbolId(rawSymbolAt(pattern))
      const path = id ? `s${id}` : null
      let changed = bindShapeAtPath(state, path, shape)
      changed = bindMemberShapeAtPath(state, path, memberShape) || changed
      return changed
    }
    if (ts.isObjectBindingPattern(pattern)) {
      let changed = false
      for (const element of pattern.elements) {
        const member = propertyName(ts, element.propertyName ?? element.name)
        if (!member) continue
        if (shape.has('') && TRACKED_RPC_MEMBERS.includes(member) && ts.isIdentifier(element.name)) {
          const id = symbolId(rawSymbolAt(element.name))
          if (id) {
            let capabilities = state.rpcCapabilities.get(id)
            if (!capabilities) {
              capabilities = new Set()
              state.rpcCapabilities.set(id, capabilities)
            }
            changed = addSetValue(capabilities, member) || changed
          }
        }
        changed = bindPattern(
          state,
          element.name,
          selectShape(shape, member),
          selectMemberShape(memberShape, member),
        ) || changed
      }
      return changed
    }
    if (ts.isArrayBindingPattern(pattern)) {
      let changed = false
      pattern.elements.forEach((element, index) => {
        if (!element || ts.isOmittedExpression(element)) return
        changed = bindPattern(
          state,
          element.name,
          selectShape(shape, String(index)),
          selectMemberShape(memberShape, String(index)),
        ) || changed
      })
      return changed
    }
    return false
  }

  // Explicit client types are seeds. Store factory types are deliberately not:
  // Function.prototype.call on `typeof useRpcStore` is not an RPC operation.
  for (const state of stateByRel.values()) {
    function collectTypedBindings(node) {
      if (
        (
          ts.isParameter(node)
          || ts.isVariableDeclaration(node)
          || ts.isPropertyDeclaration(node)
        )
        && node.type
      ) {
        if (typeIsDirectOrigin(node.type, 'client-type')) {
          if (ts.isPropertyDeclaration(node)) {
            bindShapeAtPath(state, classMemberPath(node), new Set(['']))
          } else {
            bindPattern(state, node.name, new Set(['']))
          }
        } else if (ts.isPropertyDeclaration(node)) {
          const path = classMemberPath(node)
          for (const suffix of clientPropertyPaths(node.type)) {
            if (path) state.rpcPropertyPaths.add(`${path}.${suffix}`)
          }
        } else if (ts.isIdentifier(node.name)) {
          const path = pathForExpression(node.name)
          for (const suffix of clientPropertyPaths(node.type)) {
            if (path) state.rpcPropertyPaths.add(`${path}.${suffix}`)
          }
        }
      }
      ts.forEachChild(node, collectTypedBindings)
    }
    collectTypedBindings(state.source)
  }

  function bindConstructorParameterProperty(state, node) {
    if (!isConstructorParameterProperty(node) || !ts.isIdentifier(node.name)) return false
    const path = classMemberPath(node)
    let changed = bindShapeAtPath(state, path, rpcShapeForExpression(node.name))
    changed = bindMemberShapeAtPath(
      state,
      path,
      rpcMemberShapeForExpression(node.name),
    ) || changed
    return changed
  }

  function bindPropertyDeclaration(state, node) {
    if (!ts.isPropertyDeclaration(node) || !node.initializer) return false
    const path = classMemberPath(node)
    let changed = bindShapeAtPath(state, path, rpcShapeForExpression(node.initializer))
    changed = bindMemberShapeAtPath(
      state,
      path,
      rpcMemberShapeForExpression(node.initializer),
    ) || changed
    return changed
  }

  function returnExpressions(functionNode) {
    if (ts.isArrowFunction(functionNode) && !ts.isBlock(functionNode.body)) {
      return [functionNode.body]
    }
    if (!functionNode.body || !ts.isBlock(functionNode.body)) return []
    const expressions = []
    function visit(node) {
      if (ts.isFunctionLike(node) && node !== functionNode) return
      if (ts.isReturnStatement(node) && node.expression) {
        expressions.push(node.expression)
        return
      }
      ts.forEachChild(node, visit)
    }
    visit(functionNode.body)
    return expressions
  }

  function propagateLocal(state) {
    let changed = false
    function visit(node) {
      changed = bindPropertyDeclaration(state, node) || changed
      changed = bindConstructorParameterProperty(state, node) || changed
      if (ts.isVariableDeclaration(node) && node.initializer) {
        changed = bindPattern(
          state,
          node.name,
          rpcShapeForExpression(node.initializer),
          rpcMemberShapeForExpression(node.initializer),
        ) || changed
      }
      if (
        ts.isBinaryExpression(node)
        && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      ) {
        const path = pathForExpression(node.left)
        changed = bindShapeAtPath(state, path, rpcShapeForExpression(node.right)) || changed
        changed = bindMemberShapeAtPath(
          state,
          path,
          rpcMemberShapeForExpression(node.right),
        ) || changed
      }
      ts.forEachChild(node, visit)
    }
    visit(state.source)
    return changed
  }

  function propagateReturns() {
    let changed = false
    for (const functionNode of functions) {
      const state = stateForNode(functionNode)
      if (!state) continue
      const id = functionId(functionNode)
      for (const expression of returnExpressions(functionNode)) {
        for (const suffix of rpcShapeForExpression(expression)) {
          if (bounded(suffix)) changed = addMapSetValue(state.returnShapes, id, suffix) || changed
        }
        for (const encoded of rpcMemberShapeForExpression(expression)) {
          if (bounded(decodeMember(encoded).path)) {
            changed = addMapSetValue(state.returnMemberShapes, id, encoded) || changed
          }
        }
      }
    }
    return changed
  }

  function propagateCalls(state) {
    let changed = false
    function propagateArguments(target, args) {
      const targetState = target ? stateForNode(target) : null
      if (!target || !targetState) return false
      let propagated = false
      target.parameters.forEach((parameter, index) => {
        const argument = args?.[index]
        if (!argument || ts.isSpreadElement(argument)) return
        propagated = bindPattern(
          targetState,
          parameter.name,
          rpcShapeForExpression(argument),
          rpcMemberShapeForExpression(argument),
        ) || propagated
      })
      return propagated
    }
    function visit(node) {
      if (ts.isCallExpression(node)) {
        const target = callableForExpression(node.expression)
        changed = propagateArguments(target, node.arguments) || changed
      } else if (ts.isNewExpression(node)) {
        const target = constructorForExpression(node.expression)
        changed = propagateArguments(target, node.arguments) || changed
      }
      ts.forEachChild(node, visit)
    }
    visit(state.source)
    return changed
  }

  let changed = true
  while (changed) {
    changed = false
    for (const state of stateByRel.values()) changed = propagateLocal(state) || changed
    changed = propagateReturns() || changed
    for (const state of stateByRel.values()) changed = propagateCalls(state) || changed
  }

  const operations = []
  for (const state of stateByRel.values()) {
    function collect(node) {
      const access = memberAccess(ts, node)
      if (
        access
        && TRACKED_RPC_MEMBERS.includes(access.member)
        && isRpcMemberReceiver(access.receiver, access.member)
        && !ts.isTypeOfExpression(node.parent)
      ) {
        operations.push({
          rel: state.rel,
          kind: isDirectInvocation(ts, node)
            ? access.member
            : `${access.member}Reference`,
        })
      }
      if (
        ts.isBindingElement(node)
        && ts.isIdentifier(node.name)
      ) {
        const id = symbolId(rawSymbolAt(node.name))
        for (const member of id ? state.rpcCapabilities.get(id) ?? [] : []) {
          operations.push({ rel: state.rel, kind: `${member}Reference` })
        }
      }
      ts.forEachChild(node, collect)
    }
    collect(state.source)
  }
  return operations
}
