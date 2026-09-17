import { createRpcAnalysisProgram } from './rpc-typescript-program.mjs'

export const TRACKED_HTTP_KINDS = [
  'httpRequest',
  'httpApiEndpoint',
  'httpAuthToken',
  'httpAuthorizationHeader',
  'httpSessionKeyHeader',
]

const TOKEN_STORAGE_KEY = 'opensquilla.wsToken'
const AUTHORIZATION_HEADER = 'authorization'
const SESSION_KEY_HEADER = 'x-opensquilla-session-key'
const memberSeparator = '\0'

const TAG = Object.freeze({
  authHeader: 'auth-header',
  authToken: 'auth-token',
  fetch: 'fetch-function',
  global: 'global-object',
  headerContainer: 'header-container',
  headersCtor: 'headers-constructor',
  request: 'request-object',
  requestCtor: 'request-constructor',
  sessionHeader: 'session-header',
  sessionStorage: 'session-storage',
  urlApi: 'url-api',
  urlDynamic: 'url-dynamic',
  urlSafe: 'url-safe',
  urlUnsafe: 'url-unsafe',
})

const encode = (path, tag) => `${path}${memberSeparator}${tag}`
function decode(value) {
  const index = value.lastIndexOf(memberSeparator)
  return { path: value.slice(0, index), tag: value.slice(index + 1) }
}

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

function propertyName(ts, name, constantStrings) {
  if (!name) return null
  if (ts.isIdentifier(name) || ts.isStringLiteralLike(name) || ts.isNumericLiteral(name)) {
    return name.text
  }
  if (ts.isComputedPropertyName(name)) {
    const values = constantStrings(name.expression)
    return values?.size === 1 ? [...values][0] : null
  }
  return null
}

function memberAccess(ts, expression, constantStrings) {
  const current = unwrap(ts, expression)
  if (ts.isPropertyAccessExpression(current)) {
    return { receiver: current.expression, member: current.name.text }
  }
  if (ts.isElementAccessExpression(current) && current.argumentExpression) {
    const values = constantStrings(current.argumentExpression)
    if (values?.size === 1) {
      return { receiver: current.expression, member: [...values][0] }
    }
  }
  return null
}

function headerTag(name) {
  const normalized = name.trim().toLowerCase()
  if (normalized === AUTHORIZATION_HEADER) return TAG.authHeader
  if (normalized === SESSION_KEY_HEADER) return TAG.sessionHeader
  return null
}

function isGatewayHost(hostname) {
  return hostname === 'localhost'
    || hostname === '0.0.0.0'
    || hostname === '::1'
    || hostname === '[::1]'
    || /^127(?:\.\d{1,3}){3}$/.test(hostname)
}

function urlTag(value) {
  const candidate = value.trim()
  if (/^(?:data|blob):/i.test(candidate)) return TAG.urlSafe
  try {
    const absolute = new URL(candidate)
    if (absolute.protocol === 'http:' || absolute.protocol === 'https:') {
      return isGatewayHost(absolute.hostname) && /^\/api(?:\/|$)/.test(absolute.pathname)
        ? TAG.urlApi
        : TAG.urlSafe
    }
    return TAG.urlUnsafe
  } catch {
    // Continue with a normalized relative URL.
  }
  try {
    const normalized = new URL(candidate, 'https://gateway.invalid/')
    const path = normalized.pathname
    if (normalized.hostname !== 'gateway.invalid') {
      return isGatewayHost(normalized.hostname) && /^\/api(?:\/|$)/.test(path)
        ? TAG.urlApi
        : TAG.urlSafe
    }
    if (/^\/(?:assets|static)(?:\/|$)/.test(path)) return TAG.urlSafe
    if (/^\/api(?:\/|$)/.test(path)) return TAG.urlApi
    return TAG.urlUnsafe
  } catch {
    return TAG.urlUnsafe
  }
}

function addShapeValue(shape, path, tag) {
  const value = encode(path, tag)
  if (shape.has(value)) return false
  shape.add(value)
  return true
}

function mergeShape(target, source) {
  let changed = false
  for (const value of source) {
    if (!target.has(value)) {
      target.add(value)
      changed = true
    }
  }
  return changed
}

function prependShape(prefix, shape, maxDepth) {
  const result = new Set()
  for (const encoded of shape) {
    const { path, tag } = decode(encoded)
    const next = path ? `${prefix}.${path}` : prefix
    if (next.split('.').length <= maxDepth) result.add(encode(next, tag))
  }
  return result
}

function selectShape(shape, property) {
  const result = new Set()
  for (const encoded of shape) {
    const { path, tag } = decode(encoded)
    if (path === property) result.add(encode('', tag))
    else if (path.startsWith(`${property}.`)) {
      result.add(encode(path.slice(property.length + 1), tag))
    }
  }
  return result
}

function tagsInShape(shape) {
  return new Set([...shape].map(value => decode(value).tag))
}

/**
 * Find HTTP operations only when they are connected to an actual Fetch call.
 * Symbol identity and a finite path/tag lattice make recursive wrappers and
 * alias cycles converge without lexical name pollution.
 */
export function collectHttpBoundaryOperations({
  ts,
  root = process.cwd(),
  sources,
  analysis: suppliedAnalysis,
}) {
  const analysis = suppliedAnalysis ?? createRpcAnalysisProgram({ ts, root, sources })
  const { checker } = analysis
  const activeRels = new Set(sources.map(source => source.rel))
  const available = analysis.sources.filter(source => activeRels.has(source.rel))
  const byRel = new Map(available.map(entry => [entry.rel, entry]))
  const sourceSignal = /\b(?:fetch|Request|RequestInit|Headers|sessionStorage)\b|opensquilla\.wsToken|Authorization|x-opensquilla-session-key|\/(?:api|assets|static)\/|(?:data|blob):/
  const selected = new Set(
    available.filter(entry => sourceSignal.test(entry.source.getFullText())).map(entry => entry.rel),
  )
  const entries = [...selected].map(rel => byRel.get(rel)).filter(Boolean)
  const symbols = new WeakMap()
  const functions = new WeakMap()
  let nextSymbol = 1
  let nextFunction = 1

  function symbolId(symbol) {
    if (!symbol) return null
    let id = symbols.get(symbol)
    if (!id) {
      id = nextSymbol++
      symbols.set(symbol, id)
    }
    return id
  }

  function functionId(node) {
    let id = functions.get(node)
    if (!id) {
      id = nextFunction++
      functions.set(node, id)
    }
    return id
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

  function isUnshadowedIdentifier(node, name) {
    if (!ts.isIdentifier(node) || node.text !== name) return false
    const symbol = rawSymbolAt(node)
    return !(symbol?.declarations ?? []).some(declaration => (
      activeRels.has(analysis.relForSource(declaration.getSourceFile()))
    ))
  }

  function constantStrings(expression, seen = new Set()) {
    const current = unwrap(ts, expression)
    if (ts.isStringLiteralLike(current)) return new Set([current.text])
    if (ts.isNoSubstitutionTemplateLiteral(current)) return new Set([current.text])
    if (ts.isTemplateExpression(current)) {
      if (current.templateSpans.length > 0) return null
      return new Set([current.head.text])
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.PlusToken
    ) {
      const left = constantStrings(current.left, seen)
      const right = constantStrings(current.right, seen)
      if (!left || !right) return null
      const values = new Set()
      for (const a of left) for (const b of right) values.add(`${a}${b}`)
      return values
    }
    if (ts.isConditionalExpression(current)) {
      const yes = constantStrings(current.whenTrue, seen)
      const no = constantStrings(current.whenFalse, seen)
      return yes && no ? new Set([...yes, ...no]) : null
    }
    if (
      ts.isBinaryExpression(current)
      && [ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken]
        .includes(current.operatorToken.kind)
    ) {
      const left = constantStrings(current.left, seen)
      const right = constantStrings(current.right, seen)
      return left && right ? new Set([...left, ...right]) : null
    }
    if (!ts.isIdentifier(current)) return null
    const symbol = canonicalSymbolAt(current) ?? rawSymbolAt(current)
    if (!symbol || seen.has(symbol)) return null
    const nextSeen = new Set(seen).add(symbol)
    const values = new Set()
    let found = false
    for (const declaration of symbol.declarations ?? []) {
      if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
        const nested = constantStrings(declaration.initializer, nextSeen)
        if (!nested) return null
        for (const value of nested) values.add(value)
        found = true
      } else if (ts.isBindingElement(declaration) && declaration.initializer) {
        const nested = constantStrings(declaration.initializer, nextSeen)
        if (!nested) return null
        for (const value of nested) values.add(value)
        found = true
      }
    }
    return found ? values : null
  }

  let maxDepth = 2
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
    const access = memberAccess(ts, expression, constantStrings)
    return access ? 1 + accessDepth(access.receiver) : 0
  }
  for (const { source } of entries) {
    function inspectDepth(node) {
      if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
        maxDepth = Math.max(maxDepth, accessDepth(node))
      }
      if (ts.isVariableDeclaration(node) || ts.isParameter(node)) {
        maxDepth = Math.max(maxDepth, bindingDepth(node.name))
      }
      ts.forEachChild(node, inspectDepth)
    }
    inspectDepth(source)
  }
  maxDepth = Math.max(maxDepth + 1, 8)

  function pathForExpression(expression) {
    const current = unwrap(ts, expression)
    if (ts.isIdentifier(current)) {
      const id = symbolId(rawSymbolAt(current))
      return id ? `s${id}` : null
    }
    const access = memberAccess(ts, current, constantStrings)
    if (!access) return null
    const parent = pathForExpression(access.receiver)
    return parent ? `${parent}.${access.member}` : null
  }

  const valuesByPath = new Map()
  const returnsByFunction = new Map()

  function shapeAtPath(path) {
    const result = new Set()
    if (!path) return result
    for (const [candidate, tags] of valuesByPath) {
      if (candidate !== path && !candidate.startsWith(`${path}.`)) continue
      const suffix = candidate === path ? '' : candidate.slice(path.length + 1)
      for (const tag of tags) result.add(encode(suffix, tag))
    }
    return result
  }

  function bindShapeAtPath(path, shape) {
    if (!path) return false
    let changed = false
    for (const encoded of shape) {
      const { path: suffix, tag } = decode(encoded)
      if (suffix && suffix.split('.').length > maxDepth) continue
      const target = suffix ? `${path}.${suffix}` : path
      let tags = valuesByPath.get(target)
      if (!tags) {
        tags = new Set()
        valuesByPath.set(target, tags)
      }
      if (!tags.has(tag)) {
        tags.add(tag)
        changed = true
      }
    }
    return changed
  }

  function bindPattern(pattern, shape) {
    if (ts.isIdentifier(pattern)) return bindShapeAtPath(pathForExpression(pattern), shape)
    if (ts.isObjectBindingPattern(pattern)) {
      let changed = false
      for (const element of pattern.elements) {
        const name = propertyName(ts, element.propertyName ?? element.name, constantStrings)
        if (name) changed = bindPattern(element.name, selectShape(shape, name)) || changed
      }
      return changed
    }
    if (ts.isArrayBindingPattern(pattern)) {
      let changed = false
      pattern.elements.forEach((element, index) => {
        if (element && !ts.isOmittedExpression(element)) {
          changed = bindPattern(element.name, selectShape(shape, String(index))) || changed
        }
      })
      return changed
    }
    return false
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
          || ts.isPropertyAssignment(declaration)
          || ts.isPropertyDeclaration(declaration)
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
    return callableFromSymbol(rawSymbolAt(symbolNode), seen)
  }

  function globalShape(name) {
    const shape = new Set()
    if (name === 'fetch') addShapeValue(shape, '', TAG.fetch)
    if (name === 'Request') addShapeValue(shape, '', TAG.requestCtor)
    if (name === 'Headers') addShapeValue(shape, '', TAG.headersCtor)
    if (name === 'sessionStorage') addShapeValue(shape, '', TAG.sessionStorage)
    if (['globalThis', 'window', 'self'].includes(name)) {
      addShapeValue(shape, '', TAG.global)
      addShapeValue(shape, 'fetch', TAG.fetch)
      addShapeValue(shape, 'Request', TAG.requestCtor)
      addShapeValue(shape, 'Headers', TAG.headersCtor)
      addShapeValue(shape, 'sessionStorage', TAG.sessionStorage)
    }
    return shape
  }

  function stringShape(expression) {
    const shape = new Set()
    const constants = constantStrings(expression)
    if (constants) {
      for (const value of constants) addShapeValue(shape, '', urlTag(value))
      return shape
    }
    const current = unwrap(ts, expression)
    if (ts.isTemplateExpression(current)) {
      const prefix = current.head.text.trim()
      addShapeValue(shape, '', /^\/api(?:\/|$)/.test(prefix) ? TAG.urlApi : TAG.urlDynamic)
    } else if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.PlusToken
    ) {
      addShapeValue(shape, '', TAG.urlDynamic)
    }
    return shape
  }

  function headerTupleSemantics(expression, seenSymbols = new Set(), seenNodes = new Set()) {
    const current = unwrap(ts, expression)
    const result = new Set()
    if (seenNodes.has(current)) return result
    const nextNodes = new Set(seenNodes).add(current)

    if (ts.isArrayLiteralExpression(current)) {
      for (const element of current.elements) {
        if (ts.isSpreadElement(element)) {
          mergeShape(result, headerTupleSemantics(element.expression, seenSymbols, nextNodes))
          continue
        }
        const tuple = unwrap(ts, element)
        if (!ts.isArrayLiteralExpression(tuple) || tuple.elements.length === 0) continue
        const names = constantStrings(tuple.elements[0])
        if (!names) continue
        for (const name of names) {
          const semantic = headerTag(name)
          if (semantic) addShapeValue(result, '', semantic)
        }
      }
      return result
    }

    if (ts.isConditionalExpression(current)) {
      mergeShape(result, headerTupleSemantics(current.whenTrue, seenSymbols, nextNodes))
      mergeShape(result, headerTupleSemantics(current.whenFalse, seenSymbols, nextNodes))
      return result
    }

    if (!ts.isIdentifier(current)) return result
    const symbol = canonicalSymbolAt(current) ?? rawSymbolAt(current)
    if (!symbol || seenSymbols.has(symbol)) return result
    const nextSymbols = new Set(seenSymbols).add(symbol)
    for (const declaration of symbol.declarations ?? []) {
      if (
        (ts.isVariableDeclaration(declaration) || ts.isBindingElement(declaration))
        && declaration.initializer
      ) {
        mergeShape(
          result,
          headerTupleSemantics(declaration.initializer, nextSymbols, nextNodes),
        )
      }
    }
    return result
  }

  function expressionShape(expression) {
    const current = unwrap(ts, expression)
    const result = stringShape(current)

    if (ts.isIdentifier(current)) {
      if (isUnshadowedIdentifier(current, current.text)) mergeShape(result, globalShape(current.text))
      mergeShape(result, shapeAtPath(pathForExpression(current)))
      return result
    }

    const access = memberAccess(ts, current, constantStrings)
    if (access) {
      mergeShape(result, selectShape(expressionShape(access.receiver), access.member))
      mergeShape(result, shapeAtPath(pathForExpression(current)))
      return result
    }

    if (ts.isObjectLiteralExpression(current)) {
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) {
          mergeShape(result, expressionShape(property.expression))
          continue
        }
        if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue
        const name = propertyName(ts, property.name, constantStrings)
        if (!name) continue
        const initializer = ts.isShorthandPropertyAssignment(property) ? property.name : property.initializer
        const value = expressionShape(initializer)
        const semantic = headerTag(name)
        if (semantic) addShapeValue(value, '', semantic)
        mergeShape(result, prependShape(name, value, maxDepth))
      }
      return result
    }

    if (ts.isArrayLiteralExpression(current)) {
      current.elements.forEach((element, index) => {
        if (!ts.isSpreadElement(element)) {
          mergeShape(result, prependShape(String(index), expressionShape(element), maxDepth))
        }
      })
      return result
    }

    if (ts.isTemplateExpression(current)) {
      for (const span of current.templateSpans) mergeShape(result, expressionShape(span.expression))
      return result
    }

    if (ts.isBinaryExpression(current)) {
      mergeShape(result, expressionShape(current.left))
      mergeShape(result, expressionShape(current.right))
      return result
    }

    if (ts.isConditionalExpression(current)) {
      mergeShape(result, expressionShape(current.whenTrue))
      mergeShape(result, expressionShape(current.whenFalse))
      return result
    }

    if (ts.isCallExpression(current)) {
      const calledMember = memberAccess(ts, current.expression, constantStrings)
      if (calledMember) {
        const receiver = expressionShape(calledMember.receiver)
        const receiverTags = tagsInShape(receiver)
        if (
          calledMember.member === 'getItem'
          && receiverTags.has(TAG.sessionStorage)
          && current.arguments[0]
          && constantStrings(current.arguments[0])?.has(TOKEN_STORAGE_KEY)
        ) addShapeValue(result, '', TAG.authToken)
        if (calledMember.member === 'bind' && receiverTags.has(TAG.fetch)) {
          addShapeValue(result, '', TAG.fetch)
        }
        if (['trim', 'toString', 'valueOf'].includes(calledMember.member)) mergeShape(result, receiver)
      }
      const target = callableForExpression(current.expression)
      if (target) mergeShape(result, returnsByFunction.get(functionId(target)) ?? new Set())
      return result
    }

    if (ts.isNewExpression(current)) {
      const constructorTags = tagsInShape(expressionShape(current.expression))
      if (constructorTags.has(TAG.headersCtor)) {
        addShapeValue(result, '', TAG.headerContainer)
        if (current.arguments?.[0]) {
          mergeShape(result, expressionShape(current.arguments[0]))
          mergeShape(result, headerTupleSemantics(current.arguments[0]))
        }
      }
      if (constructorTags.has(TAG.requestCtor)) {
        addShapeValue(result, '', TAG.request)
        if (current.arguments?.[0]) {
          mergeShape(result, prependShape('$endpoint', expressionShape(current.arguments[0]), maxDepth))
        }
        if (current.arguments?.[1]) {
          mergeShape(result, prependShape('$init', expressionShape(current.arguments[1]), maxDepth))
        }
      }
      return result
    }

    return result
  }

  function typeIsFetch(node) {
    if (!node) return false
    if (ts.isUnionTypeNode(node)) return node.types.some(typeIsFetch)
    if (!ts.isTypeQueryNode(node)) return false
    const name = node.exprName
    if (ts.isIdentifier(name)) return name.text === 'fetch'
    if (ts.isQualifiedName(name)) {
      return ts.isIdentifier(name.left)
        && ['globalThis', 'window'].includes(name.left.text)
        && name.right.text === 'fetch'
    }
    return false
  }

  const declarations = []
  const assignments = []
  const calls = []
  const returned = []
  const indexedRels = new Set()
  function indexEntry(entry) {
    if (!entry || indexedRels.has(entry.rel)) return false
    indexedRels.add(entry.rel)
    if (!entries.includes(entry)) entries.push(entry)
    const { source } = entry
    const functionStack = []
    function index(node) {
      const isFunction = ts.isFunctionDeclaration(node)
        || ts.isFunctionExpression(node)
        || ts.isArrowFunction(node)
        || ts.isMethodDeclaration(node)
      if (isFunction) {
        functionStack.push(node)
        if (ts.isArrowFunction(node) && !ts.isBlock(node.body)) returned.push([node, node.body])
      }
      if ((ts.isParameter(node) || ts.isVariableDeclaration(node)) && typeIsFetch(node.type)) {
        bindPattern(node.name, new Set([encode('', TAG.fetch)]))
      }
      if (ts.isVariableDeclaration(node) && node.initializer) declarations.push(node)
      if (
        ts.isBinaryExpression(node)
        && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
        && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment
      ) assignments.push(node)
      if (ts.isCallExpression(node)) calls.push(node)
      if (ts.isReturnStatement(node) && node.expression && functionStack.length) {
        returned.push([functionStack[functionStack.length - 1], node.expression])
      }
      ts.forEachChild(node, index)
      if (isFunction) functionStack.pop()
    }
    index(source)
    return true
  }
  for (const entry of [...entries]) indexEntry(entry)

  function headerMutation(node) {
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    ) {
      const access = memberAccess(ts, node.left, constantStrings)
      const semantic = access ? headerTag(access.member) : null
      if (access && semantic) {
        const value = expressionShape(node.right)
        addShapeValue(value, '', semantic)
        return bindShapeAtPath(pathForExpression(access.receiver), value)
      }
    }
    if (ts.isCallExpression(node)) {
      const access = memberAccess(ts, node.expression, constantStrings)
      if (
        access
        && ['append', 'delete', 'get', 'has', 'set'].includes(access.member)
        && node.arguments[0]
      ) {
        const names = constantStrings(node.arguments[0])
        if (names?.size === 1) {
          const semantic = headerTag([...names][0])
          if (semantic) {
            const value = node.arguments[1] ? expressionShape(node.arguments[1]) : new Set()
            addShapeValue(value, '', semantic)
            return bindShapeAtPath(pathForExpression(access.receiver), value)
          }
        }
      }
    }
    return false
  }

  function propagateIndexedNodes() {
    let changed = false
    for (const node of declarations) {
      changed = bindPattern(node.name, expressionShape(node.initializer)) || changed
    }
    for (const node of assignments) {
      changed = bindShapeAtPath(pathForExpression(node.left), expressionShape(node.right)) || changed
      changed = headerMutation(node) || changed
    }
    for (const node of calls) {
      const target = callableForExpression(node.expression)
      if (target) {
        const carriesCapability = node.arguments.some(argument => (
          !ts.isSpreadElement(argument)
          && [...tagsInShape(expressionShape(argument))].some(tag => (
            tag === TAG.fetch
            || tag === TAG.requestCtor
            || tag === TAG.headersCtor
            || tag === TAG.request
            || tag === TAG.global
          ))
        ))
        if (carriesCapability) {
          const targetRel = analysis.relForSource(target.getSourceFile())
          if (targetRel && activeRels.has(targetRel)) {
            changed = indexEntry(byRel.get(targetRel)) || changed
          }
        }
        target.parameters.forEach((parameter, index) => {
          const argument = node.arguments[index]
          if (argument && !ts.isSpreadElement(argument)) {
            changed = bindPattern(parameter.name, expressionShape(argument)) || changed
          }
        })
      }
      changed = headerMutation(node) || changed
    }
    return changed
  }

  function propagateReturns() {
    let changed = false
    for (const [functionNode, expression] of returned) {
      const id = functionId(functionNode)
      let result = returnsByFunction.get(id)
      if (!result) {
        result = new Set()
        returnsByFunction.set(id, result)
      }
      changed = mergeShape(result, expressionShape(expression)) || changed
    }
    return changed
  }

  let changed = true
  while (changed) {
    changed = false
    changed = propagateIndexedNodes() || changed
    changed = propagateReturns() || changed
  }

  function requestEvidence(call) {
    const target = expressionShape(call.arguments[0])
    const targetTags = tagsInShape(target)
    let endpoint = target
    let headers = call.arguments[1]
      ? selectShape(expressionShape(call.arguments[1]), 'headers')
      : new Set()
    if (targetTags.has(TAG.request)) {
      endpoint = selectShape(target, '$endpoint')
      headers = selectShape(selectShape(target, '$init'), 'headers')
      if (call.arguments[1]) {
        mergeShape(headers, selectShape(expressionShape(call.arguments[1]), 'headers'))
      }
    }
    const endpointTags = tagsInShape(endpoint)
    const headerTags = tagsInShape(headers)
    const safe = endpointTags.size > 0 && [...endpointTags].every(tag => tag === TAG.urlSafe)
    return { endpointTags, headerTags, safe }
  }

  const operations = []
  for (const { rel, source } of entries) {
    function collect(node) {
      if (
        ts.isCallExpression(node)
        && node.arguments[0]
        && tagsInShape(expressionShape(node.expression)).has(TAG.fetch)
      ) {
        const evidence = requestEvidence(node)
        if (!evidence.safe) {
          const kinds = new Set(['httpRequest'])
          if (
            evidence.endpointTags.has(TAG.urlApi)
            || evidence.endpointTags.has(TAG.urlDynamic)
            || evidence.endpointTags.size === 0
          ) kinds.add('httpApiEndpoint')
          if (evidence.headerTags.has(TAG.authToken)) kinds.add('httpAuthToken')
          if (evidence.headerTags.has(TAG.authHeader)) kinds.add('httpAuthorizationHeader')
          if (evidence.headerTags.has(TAG.sessionHeader)) kinds.add('httpSessionKeyHeader')
          for (const kind of kinds) operations.push({ rel, kind })
        }
      }
      ts.forEachChild(node, collect)
    }
    collect(source)
  }
  return operations
}
