import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'

function normalized(path) {
  return path.replace(/\\/g, '/')
}

function isTestFile(importer) {
  return /\.(test|spec)\.(?:[cm]?[jt]sx?)$/.test(importer)
}

function isGatewayAdapter(importer) {
  return importer.startsWith('src/adapters/gateway/')
}

function isCompositionRoot(importer) {
  return importer === 'src/main.ts'
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
    isGatewayAdapter(normalizedImporter)
    || isTestFile(normalizedImporter)
    || isGeneratedContract(normalizedImporter)
  ) return null
  return `${normalizedImporter}: generated wire Contract import "${specifier}" is allowed only in a Gateway Adapter or test.`
}

/** Keep generic transports private to Gateway Adapters. */
export function privateGatewayTransportImportViolation({ root, importer, specifier }) {
  const normalizedImporter = normalized(importer)
  const target = resolveSourceImport(root, normalizedImporter, specifier)
  const normalizedTarget = target?.replace(/\.(?:[cm]?[jt]s)$/, '')
  const rpcTransportModule = resolve(root, 'src/adapters/gateway/privateTransports')
  const httpTransportModule = resolve(root, 'src/adapters/gateway/privateHttpTransport')
  if (
    !normalizedTarget
    || (normalizedTarget !== rpcTransportModule && normalizedTarget !== httpTransportModule)
  ) return null
  if (
    isGatewayAdapter(normalizedImporter)
    || isTestFile(normalizedImporter)
    || (normalizedTarget === httpTransportModule && isCompositionRoot(normalizedImporter))
  ) return null
  if (normalizedTarget === httpTransportModule) {
    return `${normalizedImporter}: private Gateway HTTP transport may be imported only by a Gateway Adapter, composition root, or test.`
  }
  return `${normalizedImporter}: private Gateway transports may be imported only by a Gateway Adapter or test.`
}

/** Gateway Adapters consume the private transport Interface, never Pinia directly. */
export function gatewayAdapterRpcStoreImportViolation({ root, importer, specifier }) {
  const normalizedImporter = normalized(importer)
  if (!isGatewayAdapter(normalizedImporter) || isTestFile(normalizedImporter)) return null
  const target = resolveSourceImport(root, normalizedImporter, specifier)
  const rpcStoreModule = resolve(root, 'src/stores/rpc')
  const normalizedTarget = target?.replace(/\.(?:vue|[cm]?[jt]sx?)$/, '')
  if (!normalizedTarget || normalizedTarget !== rpcStoreModule) return null
  if (normalizedImporter === 'src/adapters/gateway/privateTransports.ts') return null
  return `${normalizedImporter}: Gateway Adapters must consume the private transport Interface instead of useRpcStore.`
}

export function boundaryModuleKind({ root, importer, specifier }) {
  const normalizedImporter = normalized(importer)
  const target = resolveSourceImport(root, normalizedImporter, specifier)
  if (!target) return null
  const generatedRoot = resolve(root, 'src/contracts/generated')
  if (isWithin(generatedRoot, target)) return 'generated Contract'
  const normalizedTarget = target.replace(/\.(?:[cm]?[jt]s)$/, '')
  const transportModules = new Set([
    resolve(root, 'src/adapters/gateway/privateTransports'),
    resolve(root, 'src/adapters/gateway/privateHttpTransport'),
  ])
  if (transportModules.has(normalizedTarget)) return 'private Gateway transport'
  return null
}

export function boundaryReexportViolation({ root, importer, specifier }) {
  const kind = boundaryModuleKind({ root, importer, specifier })
  if (!kind) return null
  if (
    kind === 'generated Contract'
    && isGeneratedContract(normalized(importer))
  ) return null
  return `${normalized(importer)}: ${kind} modules must not be re-exported through a barrel.`
}

/**
 * Track boundary symbols imported into a module so alias re-exports can be
 * rejected as strictly as direct ``export ... from`` declarations.
 */
export function importedBoundarySymbols(ts, source, { root, importer }) {
  const symbols = new Map()
  for (const statement of source.statements) {
    if (
      !ts.isImportDeclaration(statement)
      || !ts.isStringLiteralLike(statement.moduleSpecifier)
      || !statement.importClause
    ) continue
    const kind = boundaryModuleKind({
      root,
      importer,
      specifier: statement.moduleSpecifier.text,
    })
    if (!kind) continue
    if (statement.importClause.name) symbols.set(statement.importClause.name.text, kind)
    const bindings = statement.importClause.namedBindings
    if (bindings && ts.isNamespaceImport(bindings)) {
      symbols.set(bindings.name.text, kind)
    } else if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) symbols.set(element.name.text, kind)
    }
  }

  // Production JavaScript occasionally uses CommonJS even though the WebUI is
  // otherwise ESM. Treat a literal require() exactly like an import so a .js
  // barrel cannot launder a private transport or generated wire symbol.
  function collectRequires(node) {
    if (
      ts.isVariableDeclaration(node)
      && node.initializer
      && ts.isCallExpression(node.initializer)
      && ts.isIdentifier(node.initializer.expression)
      && node.initializer.expression.text === 'require'
      && node.initializer.arguments.length === 1
      && ts.isStringLiteralLike(node.initializer.arguments[0])
    ) {
      const kind = boundaryModuleKind({
        root,
        importer,
        specifier: node.initializer.arguments[0].text,
      })
      if (kind && ts.isIdentifier(node.name)) symbols.set(node.name.text, kind)
      if (kind && ts.isObjectBindingPattern(node.name)) {
        for (const element of node.name.elements) {
          if (ts.isIdentifier(element.name)) symbols.set(element.name.text, kind)
        }
      }
    }
    ts.forEachChild(node, collectRequires)
  }
  collectRequires(source)
  return symbols
}

function referencedBoundaryKinds(ts, node, symbols) {
  const kinds = new Set()
  function visit(current) {
    if (ts.isIdentifier(current)) {
      const kind = symbols.get(current.text)
      if (kind) kinds.add(kind)
    }
    ts.forEachChild(current, visit)
  }
  visit(node)
  return kinds
}

function exported(ts, node) {
  return Boolean(ts.getModifiers(node)?.some(modifier => (
    modifier.kind === ts.SyntaxKind.ExportKeyword
  )))
}

function propagateBoundaryAliases(ts, source, imported) {
  const symbols = new Map(imported)
  let changed = true
  while (changed) {
    changed = false
    for (const statement of source.statements) {
      if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue
          const [kind] = referencedBoundaryKinds(ts, declaration.initializer, symbols)
          if (kind && !symbols.has(declaration.name.text)) {
            symbols.set(declaration.name.text, kind)
            changed = true
          }
        }
      } else if (ts.isTypeAliasDeclaration(statement)) {
        const [kind] = referencedBoundaryKinds(ts, statement.type, symbols)
        if (kind && !symbols.has(statement.name.text)) {
          symbols.set(statement.name.text, kind)
          changed = true
        }
      } else if (ts.isInterfaceDeclaration(statement)) {
        const [kind] = referencedBoundaryKinds(ts, statement, symbols)
        if (kind && !symbols.has(statement.name.text)) {
          symbols.set(statement.name.text, kind)
          changed = true
        }
      }
    }
  }
  return symbols
}

export function localBoundaryReexportViolations(ts, source, { root, importer }) {
  const imported = importedBoundarySymbols(ts, source, { root, importer })
  const symbols = propagateBoundaryAliases(ts, source, imported)
  const failures = []
  for (const statement of source.statements) {
    if (
      ts.isExportDeclaration(statement)
      && !statement.moduleSpecifier
      && statement.exportClause
      && ts.isNamedExports(statement.exportClause)
    ) {
      for (const element of statement.exportClause.elements) {
        const local = (element.propertyName ?? element.name).text
        const kind = symbols.get(local)
        if (kind) {
          failures.push(
            `${normalized(importer)}: ${kind} symbol ${local} must not be re-exported through a barrel.`,
          )
        }
      }
    }

    if (ts.isVariableStatement(statement) && exported(ts, statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (!ts.isIdentifier(declaration.name) || !declaration.initializer) continue
        for (const kind of referencedBoundaryKinds(ts, declaration.initializer, symbols)) {
          failures.push(
            `${normalized(importer)}: ${kind} symbol ${declaration.name.text} must not be exported through a barrel.`,
          )
        }
      }
    }

    if (
      (ts.isTypeAliasDeclaration(statement) || ts.isInterfaceDeclaration(statement))
      && exported(ts, statement)
    ) {
      for (const kind of referencedBoundaryKinds(ts, statement, symbols)) {
        failures.push(
          `${normalized(importer)}: ${kind} symbol ${statement.name.text} must not be exported through a barrel.`,
        )
      }
    }

    if (ts.isExportAssignment(statement)) {
      for (const kind of referencedBoundaryKinds(ts, statement.expression, symbols)) {
        failures.push(
          `${normalized(importer)}: ${kind} symbols must not be exported as the default module value.`,
        )
      }
    }
  }

  // CommonJS export assignment for production .js barrels.
  function visitCommonJsExports(node) {
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && /^(?:module\.exports|exports(?:\.[A-Za-z_$][\w$]*)?)$/.test(
        node.left.getText(source).replace(/\s/g, ''),
      )
    ) {
      for (const kind of referencedBoundaryKinds(ts, node.right, symbols)) {
        failures.push(
          `${normalized(importer)}: ${kind} symbols must not be exported through a CommonJS barrel.`,
        )
      }
    }
    ts.forEachChild(node, visitCommonJsExports)
  }
  visitCommonJsExports(source)
  return failures
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

function namedMemberReceiver(ts, expression, source, memberName) {
  const member = unwrapExpression(ts, expression)
  if (ts.isPropertyAccessExpression(member) && member.name.text === memberName) {
    return member.expression.getText(source).replace(/\s/g, '')
  }
  if (
    ts.isElementAccessExpression(member)
    && member.argumentExpression
    && ts.isStringLiteralLike(member.argumentExpression)
    && member.argumentExpression.text === memberName
  ) {
    return member.expression.getText(source).replace(/\s/g, '')
  }
  return null
}

/** Return the receiver text for a direct named member invocation. */
export function namedMemberCallReceiverText(ts, node, source, memberName) {
  if (!ts.isCallExpression(node)) return null
  return namedMemberReceiver(ts, node.expression, source, memberName)
}

/** Return the receiver of a named member reference, invoked or extracted. */
export function namedMemberReferenceReceiverText(ts, node, source, memberName) {
  return namedMemberReceiver(ts, node, source, memberName)
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
  return destructuredMemberSourceText(ts, node, source, 'call')
}

/** Return the source/type of an object binding that extracts a named member. */
export function destructuredMemberSourceText(ts, node, source, memberName) {
  if (!ts.isBindingElement(node)) return null
  const property = node.propertyName ?? node.name
  const isMatch = (
    (ts.isIdentifier(property) || ts.isStringLiteralLike(property))
    && property.text === memberName
  )
  if (!isMatch || !ts.isObjectBindingPattern(node.parent)) return null
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
