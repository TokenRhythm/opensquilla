import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'

function normalized(path) {
  return path.replace(/\\/g, '/')
}

function isTestFile(importer) {
  return /\.(test|spec)\.(ts|tsx)$/.test(importer)
}

function isAdapter(importer) {
  return importer.startsWith('src/adapters/')
}

function isGeneratedContract(importer) {
  return importer.startsWith('src/contracts/generated/')
}

function isWithin(parent, candidate) {
  const rel = relative(parent, candidate)
  return rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel))
}

/** Resolve source imports without requiring the target file to exist. */
export function resolveSourceImport(root, importer, specifier) {
  const sourceRoot = resolve(root, 'src')
  const cleanSpecifier = specifier.split(/[?#]/, 1)[0]
  if (cleanSpecifier.startsWith('@/')) {
    return resolve(sourceRoot, cleanSpecifier.slice(2))
  }
  if (cleanSpecifier.startsWith('./') || cleanSpecifier.startsWith('../')) {
    return resolve(dirname(resolve(root, importer)), cleanSpecifier)
  }
  return null
}

export function generatedContractImportViolation({ root, importer, specifier }) {
  const normalizedImporter = normalized(importer)
  const target = resolveSourceImport(root, normalizedImporter, specifier)
  const generatedRoot = resolve(root, 'src/contracts/generated')
  if (!target || !isWithin(generatedRoot, target)) return null
  if (
    isAdapter(normalizedImporter)
    || isTestFile(normalizedImporter)
    || isGeneratedContract(normalizedImporter)
  ) return null
  return `${normalizedImporter}: generated wire Contract import "${specifier}" is allowed only in an Adapter or test.`
}

/** Return a statically knowable module reference from TypeScript module syntax. */
export function moduleReferenceSpecifier(ts, node) {
  if (
    (ts.isImportDeclaration(node) || ts.isExportDeclaration(node))
    && node.moduleSpecifier
    && ts.isStringLiteralLike(node.moduleSpecifier)
  ) {
    return node.moduleSpecifier.text
  }
  if (
    ts.isCallExpression(node)
    && (
      node.expression.kind === ts.SyntaxKind.ImportKeyword
      || (ts.isIdentifier(node.expression) && node.expression.text === 'require')
    )
    && node.arguments.length === 1
    && ts.isStringLiteralLike(node.arguments[0])
  ) {
    return node.arguments[0].text
  }
  if (
    ts.isImportTypeNode(node)
    && ts.isLiteralTypeNode(node.argument)
    && ts.isStringLiteralLike(node.argument.literal)
  ) {
    return node.argument.literal.text
  }
  if (
    ts.isImportEqualsDeclaration(node)
    && ts.isExternalModuleReference(node.moduleReference)
    && node.moduleReference.expression
    && ts.isStringLiteralLike(node.moduleReference.expression)
  ) {
    return node.moduleReference.expression.text
  }
  return null
}

function unwrapExpression(ts, expression) {
  let current = expression
  while (
    ts.isParenthesizedExpression(current)
    || ts.isAsExpression(current)
    || ts.isTypeAssertionExpression(current)
    || ts.isNonNullExpression(current)
    || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(current))
  ) {
    current = current.expression
  }
  return current
}

function callMemberReceiver(ts, expression, source) {
  const member = unwrapExpression(ts, expression)
  if (ts.isPropertyAccessExpression(member) && member.name.text === 'call') {
    return member.expression.getText(source).replace(/\s/g, '')
  }
  if (
    ts.isElementAccessExpression(member)
    && member.argumentExpression
    && ts.isStringLiteralLike(member.argumentExpression)
    && member.argumentExpression.text === 'call'
  ) {
    return member.expression.getText(source).replace(/\s/g, '')
  }
  return null
}

/** Return the receiver text for a direct `.call(...)` or `["call"](...)`. */
export function callMemberReceiverText(ts, node, source) {
  if (!ts.isCallExpression(node)) return null
  return callMemberReceiver(ts, node.expression, source)
}

/** Return the receiver of any `.call` member reference, invoked or extracted. */
export function callMemberReferenceReceiverText(ts, node, source) {
  return callMemberReceiver(ts, node, source)
}

/** Whether a `.call` member reference is the callee of a direct invocation. */
export function isDirectCallMemberReference(ts, node) {
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
  ) {
    current = current.parent
  }
  return Boolean(
    current.parent
    && ts.isCallExpression(current.parent)
    && unwrapExpression(ts, current.parent.expression) === node,
  )
}

/** Return the source/type of an object binding that extracts `call`. */
export function destructuredCallSourceText(ts, node, source) {
  if (!ts.isBindingElement(node)) return null
  const property = node.propertyName ?? node.name
  const isCall = (
    (ts.isIdentifier(property) || ts.isStringLiteralLike(property))
    && property.text === 'call'
  )
  if (!isCall || !ts.isObjectBindingPattern(node.parent)) return null
  const owner = node.parent.parent
  if (ts.isVariableDeclaration(owner) && owner.initializer) {
    return owner.initializer.getText(source).replace(/\s/g, '')
  }
  if (ts.isParameter(owner) && owner.type) {
    return owner.type.getText(source).replace(/\s/g, '')
  }
  return null
}

/** Syntactic provenance used only for extracted/destructured capabilities. */
export function isRpcCapabilityReceiverText(receiver) {
  const compact = receiver.replace(/\s/g, '')
  return (
    /(?:rpc|rpcstore|gateway|client)!?$/i.test(compact)
    || /useRpc(?:Store)?\(\)!?$/i.test(compact)
    || /Rpc(?:Client|Store)|GatewayClient/.test(compact)
  )
}

/** Standard prototype helpers are not RPC clients despite using `.call`. */
export function isKnownNonRpcCallReceiver(receiver) {
  return /^(?:Object|Array|String|Number|Boolean|BigInt|Symbol|RegExp|Date|Function)\.prototype\.[A-Za-z_$][\w$]*$/.test(receiver)
}
