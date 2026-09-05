import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'

import { createRpcAnalysisProgram } from './rpc-typescript-program.mjs'

function normalized(path) {
  return path.replace(/\\/g, '/')
}

function isTestFile(importer) {
  return /\.(test|spec)\.(?:[cm]?[jt]sx?)$/.test(importer)
}

function isTestingSupport(importer) {
  return importer.startsWith('src/testing/')
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

/** Keep the Pinia RPC store seed private to the composition root. */
export function gatewayAdapterRpcStoreImportViolation({ root, importer, specifier }) {
  const normalizedImporter = normalized(importer)
  const target = resolveSourceImport(root, normalizedImporter, specifier)
  const rpcStoreModule = resolve(root, 'src/stores/rpc')
  const normalizedTarget = target?.replace(/\.(?:vue|[cm]?[jt]sx?)$/, '')
  if (!normalizedTarget || normalizedTarget !== rpcStoreModule) return null
  if (isCompositionRoot(normalizedImporter) || isTestFile(normalizedImporter)) return null
  return `${normalizedImporter}: useRpcStore may be imported only by the composition root or tests.`
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
 * Whole-program export and composition fence.  TypeScript symbols retain the
 * declaration behind barrels and aliases, so private/generated values cannot
 * be laundered by renaming them and Adapter imports of useRpcStore cannot hide
 * behind an index module.
 */
export function collectBoundaryArchitectureViolations({
  ts,
  root,
  sources,
  analysis: suppliedAnalysis,
}) {
  const analysis = suppliedAnalysis ?? createRpcAnalysisProgram({ ts, root, sources })
  const { checker } = analysis
  const failures = []
  const originBySymbol = new Map()
  const sourceByRel = new Map(analysis.sources.map(entry => [entry.rel, entry.source]))
  const requesterSymbol = analysis.exportedSymbol('src/adapters/gateway/privateTransports.ts', 'RpcRequester')

  function markExportOrigins(rel, kind, only = null) {
    const source = sourceByRel.get(rel)
    const moduleSymbol = source ? analysis.symbolAt(source) : null
    if (!moduleSymbol) return
    for (const symbol of checker.getExportsOfModule(moduleSymbol)) {
      if (only && symbol.getName() !== only) continue
      originBySymbol.set(symbol, kind)
      const canonical = analysis.canonicalSymbol(symbol)
      if (canonical) originBySymbol.set(canonical, kind)
    }
  }

  for (const { rel } of analysis.sources) {
    if (isGeneratedContract(rel)) markExportOrigins(rel, 'generated Contract')
    if (
      rel === 'src/adapters/gateway/privateTransports.ts'
      || rel === 'src/adapters/gateway/privateHttpTransport.ts'
    ) markExportOrigins(rel, 'private Gateway transport')
  }
  markExportOrigins('src/stores/rpc.ts', 'RPC store factory', 'useRpcStore')

  function rawSymbol(node) {
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
      if (ts.isParameter(declaration) && declaration.type) {
        const [kind] = typeBoundaryKinds(declaration.type, nextSeen)
        if (kind) return kind
      }
      if (ts.isVariableDeclaration(declaration) && declaration.initializer) {
        const [kind] = valueBoundaryKinds(declaration.initializer, nextSeen)
        if (kind) return kind
      }
      if (ts.isTypeAliasDeclaration(declaration)) {
        const [kind] = typeBoundaryKinds(declaration.type, nextSeen)
        if (kind) return kind
      }
      if (ts.isInterfaceDeclaration(declaration)) {
        const [kind] = typeBoundaryKinds(declaration, nextSeen)
        if (kind) return kind
      }
      if (ts.isFunctionDeclaration(declaration) || ts.isMethodDeclaration(declaration)) {
        const [kind] = functionExposureKinds(declaration, nextSeen)
        if (kind) return kind
      }
      if (ts.isExportAssignment(declaration)) {
        const [kind] = valueBoundaryKinds(declaration.expression, nextSeen)
        if (kind) return kind
      }
    }
    return null
  }

  function typeBoundaryKinds(node, seen = new Set()) {
    const kinds = new Set()
    function visit(current) {
      if (ts.isIdentifier(current)) {
        const kind = symbolOrigin(rawSymbol(current), seen)
        if (kind) kinds.add(kind)
      }
      ts.forEachChild(current, visit)
    }
    visit(node)
    return kinds
  }

  function requireBoundaryKind(expression, importerRel) {
    const current = unwrapExpression(ts, expression)
    let specifier = null
    let exported = 'default'
    if (
      ts.isCallExpression(current)
      && ts.isIdentifier(current.expression)
      && current.expression.text === 'require'
      && current.arguments.length === 1
      && ts.isStringLiteralLike(current.arguments[0])
    ) specifier = current.arguments[0].text
    const access = (
      ts.isPropertyAccessExpression(current)
      || (
        ts.isElementAccessExpression(current)
        && current.argumentExpression
        && ts.isStringLiteralLike(current.argumentExpression)
      )
    ) ? current : null
    if (access) {
      const receiver = unwrapExpression(ts, access.expression)
      if (
        ts.isCallExpression(receiver)
        && ts.isIdentifier(receiver.expression)
        && receiver.expression.text === 'require'
        && receiver.arguments.length === 1
        && ts.isStringLiteralLike(receiver.arguments[0])
      ) {
        specifier = receiver.arguments[0].text
        exported = ts.isPropertyAccessExpression(access)
          ? access.name.text
          : access.argumentExpression.text
      }
    }
    if (!specifier) return null
    const record = analysis.resolveRecord(importerRel, specifier)
    const symbol = record ? analysis.exportedSymbol(record.rel, exported) : null
    return symbolOrigin(symbol)
      ?? (record && isGeneratedContract(record.rel) ? 'generated Contract' : null)
      ?? (record && (
        record.rel === 'src/adapters/gateway/privateTransports.ts'
        || record.rel === 'src/adapters/gateway/privateHttpTransport.ts'
      ) ? 'private Gateway transport' : null)
  }

  function valueBoundaryKinds(expression, seen = new Set()) {
    const current = unwrapExpression(ts, expression)
    const kinds = new Set()
    const stateRel = analysis.relForSource(current.getSourceFile())
    const required = stateRel ? requireBoundaryKind(current, stateRel) : null
    if (required) kinds.add(required)
    if (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)) {
      if (valueBoundaryKinds(current.expression, seen).has('private Gateway transport')) {
        kinds.add('private Gateway transport')
      }
    }
    if (ts.isIdentifier(current) || ts.isPropertyAccessExpression(current)) {
      const node = ts.isPropertyAccessExpression(current) ? current.name : current
      const kind = symbolOrigin(rawSymbol(node), seen)
      if (kind) kinds.add(kind)
      return kinds
    }
    if (
      ts.isElementAccessExpression(current)
      && current.argumentExpression
      && ts.isStringLiteralLike(current.argumentExpression)
    ) {
      const kind = symbolOrigin(rawSymbol(current.argumentExpression), seen)
        ?? symbolOrigin(rawSymbol(current), seen)
      if (kind) kinds.add(kind)
      return kinds
    }
    if (ts.isObjectLiteralExpression(current)) {
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) {
          for (const kind of valueBoundaryKinds(property.expression, seen)) kinds.add(kind)
        } else if (ts.isPropertyAssignment(property)) {
          for (const kind of valueBoundaryKinds(property.initializer, seen)) kinds.add(kind)
        } else if (ts.isShorthandPropertyAssignment(property)) {
          for (const kind of valueBoundaryKinds(property.name, seen)) kinds.add(kind)
        } else if (
          ts.isMethodDeclaration(property)
          || ts.isGetAccessorDeclaration(property)
          || ts.isSetAccessorDeclaration(property)
        ) {
          for (const kind of functionExposureKinds(property, seen)) kinds.add(kind)
        }
      }
      return kinds
    }
    if (ts.isArrayLiteralExpression(current)) {
      for (const element of current.elements) {
        if (!ts.isSpreadElement(element)) {
          for (const kind of valueBoundaryKinds(element, seen)) kinds.add(kind)
        }
      }
      return kinds
    }
    if (ts.isConditionalExpression(current)) {
      for (const kind of valueBoundaryKinds(current.whenTrue, seen)) kinds.add(kind)
      for (const kind of valueBoundaryKinds(current.whenFalse, seen)) kinds.add(kind)
      return kinds
    }
    if (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) {
      return functionExposureKinds(current, seen)
    }
    if (ts.isClassExpression(current)) {
      for (const heritage of current.heritageClauses ?? []) {
        for (const kind of typeBoundaryKinds(heritage, seen)) kinds.add(kind)
      }
      for (const member of current.members) {
        if (member.type) {
          for (const kind of typeBoundaryKinds(member.type, seen)) kinds.add(kind)
        }
        if (
          ts.isMethodDeclaration(member)
          || ts.isConstructorDeclaration(member)
          || ts.isGetAccessorDeclaration(member)
          || ts.isSetAccessorDeclaration(member)
        ) {
          for (const kind of functionExposureKinds(member, seen)) kinds.add(kind)
        } else if (member.initializer) {
          for (const kind of valueBoundaryKinds(member.initializer, seen)) kinds.add(kind)
        }
      }
      return kinds
    }
    if (ts.isCallExpression(current)) {
      if (ts.isPropertyAccessExpression(current.expression) && current.expression.name.text === 'bind') {
        for (const kind of valueBoundaryKinds(current.expression.expression, seen)) kinds.add(kind)
      }
      const signature = checker.getResolvedSignature(current)
      const declaration = signature?.declaration
      if (declaration?.type) {
        for (const kind of typeBoundaryKinds(declaration.type, seen)) kinds.add(kind)
      }
      if (declaration?.body) {
        for (const kind of functionReturnKinds(declaration, seen)) kinds.add(kind)
      }
      return kinds
    }
    return kinds
  }

  function functionExposureKinds(node, seen = new Set()) {
    const kinds = new Set()
    // A typed Adapter factory consumes its one private request dependency.
    // Return annotations and returned values remain subject to the export fence.
    const parameterType = node.parameters?.length === 1 ? node.parameters[0].type : null
    const injectsRequester = requesterSymbol && ts.isFunctionDeclaration(node)
      && isGatewayAdapter(analysis.relForSource(node.getSourceFile()) ?? '')
      && parameterType && ts.isTypeReferenceNode(parameterType)
      && analysis.canonicalSymbol(rawSymbol(parameterType.typeName)) === requesterSymbol
      && node.type && ts.isTypeReferenceNode(node.type) && !node.typeParameters?.length
    for (const parameter of node.parameters ?? []) {
      if (parameter.type && !injectsRequester) {
        for (const kind of typeBoundaryKinds(parameter.type, seen)) kinds.add(kind)
      }
    }
    if (node.type) {
      for (const kind of typeBoundaryKinds(node.type, seen)) kinds.add(kind)
    }
    for (const parameter of node.typeParameters ?? []) {
      if (parameter.constraint) {
        for (const kind of typeBoundaryKinds(parameter.constraint, seen)) kinds.add(kind)
      }
    }
    for (const kind of functionReturnKinds(node, seen)) kinds.add(kind)
    return kinds
  }

  function functionReturnKinds(node, seen) {
    const kinds = new Set()
    if (seen.has(node)) return kinds
    const nextSeen = new Set(seen).add(node)
    if (node.body && !ts.isBlock(node.body)) {
      for (const kind of valueBoundaryKinds(node.body, nextSeen)) kinds.add(kind)
      return kinds
    }
    function visit(current) {
      if (ts.isFunctionLike(current) && current !== node) return
      if (ts.isReturnStatement(current) && current.expression) {
        for (const kind of valueBoundaryKinds(current.expression, nextSeen)) kinds.add(kind)
        return
      }
      ts.forEachChild(current, visit)
    }
    if (node.body) visit(node.body)
    return kinds
  }

  function exportedStatementKinds(statement) {
    const kinds = new Set()
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (declaration.type) {
          for (const kind of typeBoundaryKinds(declaration.type)) kinds.add(kind)
        }
        if (declaration.initializer) {
          for (const kind of valueBoundaryKinds(declaration.initializer)) kinds.add(kind)
        }
      }
    } else if (ts.isFunctionDeclaration(statement)) {
      for (const kind of functionExposureKinds(statement)) kinds.add(kind)
    } else if (ts.isClassDeclaration(statement)) {
      for (const heritage of statement.heritageClauses ?? []) {
        for (const kind of typeBoundaryKinds(heritage)) kinds.add(kind)
      }
      for (const member of statement.members) {
        if (member.type) for (const kind of typeBoundaryKinds(member.type)) kinds.add(kind)
        if (
          ts.isMethodDeclaration(member)
          || ts.isConstructorDeclaration(member)
          || ts.isGetAccessorDeclaration(member)
          || ts.isSetAccessorDeclaration(member)
        ) {
          for (const kind of functionExposureKinds(member)) kinds.add(kind)
        } else if (member.initializer) {
          for (const kind of valueBoundaryKinds(member.initializer)) kinds.add(kind)
        }
      }
    } else if (ts.isTypeAliasDeclaration(statement)) {
      for (const kind of typeBoundaryKinds(statement.type)) kinds.add(kind)
    } else if (ts.isInterfaceDeclaration(statement)) {
      for (const kind of typeBoundaryKinds(statement)) kinds.add(kind)
    }
    return kinds
  }

  function transitiveReexportBoundaryKinds(rel, statement) {
    if (
      !ts.isExportDeclaration(statement)
      || !statement.moduleSpecifier
      || !ts.isStringLiteralLike(statement.moduleSpecifier)
    ) return []
    // Direct boundary re-exports are reported by the import/reference scan in
    // the architecture gate.  This pass is for a barrel whose target is an
    // intermediate module, where a lexical path check would otherwise lose the
    // private/generated origin.
    if (boundaryModuleKind({
      root,
      importer: rel,
      specifier: statement.moduleSpecifier.text,
    })) return []
    const target = analysis.resolveRecord(rel, statement.moduleSpecifier.text)
    if (!target) return []
    const targetSource = sourceByRel.get(target.rel)
    const targetModule = targetSource ? analysis.symbolAt(targetSource) : null
    if (!targetModule) return []
    let exportedSymbols = []
    if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
      exportedSymbols = statement.exportClause.elements
        .map(element => analysis.exportedSymbol(
          target.rel,
          (element.propertyName ?? element.name).text,
        ))
        .filter(Boolean)
    } else {
      // `export * from` and `export * as ns from` both expose the target
      // module's exported symbols.  Namespace exports are conservative here:
      // exposing any private member makes the namespace itself a leak.
      exportedSymbols = checker.getExportsOfModule(targetModule)
    }
    return exportedSymbols
      .map(symbol => symbolOrigin(symbol))
      .filter(Boolean)
  }

  for (const { rel, source } of analysis.sources) {
    const generated = isGeneratedContract(rel)
    const privateBoundaryModule = (
      rel === 'src/adapters/gateway/privateTransports.ts'
      || rel === 'src/adapters/gateway/privateHttpTransport.ts'
    )
    if (!isCompositionRoot(rel) && !isTestFile(rel) && !isTestingSupport(rel)) {
      function namespaceExportsStoreFactory(name) {
        const raw = rawSymbol(name)
        const moduleSymbol = analysis.canonicalSymbol(raw) ?? raw
        if (!moduleSymbol) return false
        return checker.getExportsOfModule(moduleSymbol).some(symbol => (
          symbolOrigin(symbol) === 'RPC store factory'
        ))
      }

      function checkStoreImports(node) {
        if (
          ts.isImportSpecifier(node)
          || ts.isImportClause(node)
          || ts.isNamespaceImport(node)
          || ts.isImportEqualsDeclaration(node)
        ) {
          const name = node.name
          const importsStoreFactory = name && (
            symbolOrigin(rawSymbol(name)) === 'RPC store factory'
            || (
              (ts.isNamespaceImport(node) || ts.isImportEqualsDeclaration(node))
              && namespaceExportsStoreFactory(name)
            )
          )
          if (importsStoreFactory) {
            failures.push(
              `${rel}: useRpcStore may be imported only by the composition root or tests.`,
            )
          }
        }
        if (
          ts.isPropertyAccessExpression(node)
          && symbolOrigin(rawSymbol(node.name)) === 'RPC store factory'
        ) {
          failures.push(
            `${rel}: useRpcStore may be imported only by the composition root or tests.`,
          )
        }
        const required = requireBoundaryKind(node, rel)
        if (required === 'RPC store factory') {
          failures.push(
            `${rel}: useRpcStore may be imported only by the composition root or tests.`,
          )
        }
        ts.forEachChild(node, checkStoreImports)
      }
      checkStoreImports(source)
    }

    for (const statement of source.statements) {
      const isExported = Boolean(ts.getModifiers(statement)?.some(modifier => (
        modifier.kind === ts.SyntaxKind.ExportKeyword
      )))
      if (isExported && !privateBoundaryModule && !isTestingSupport(rel)) {
        for (const kind of exportedStatementKinds(statement)) {
          if (kind === 'generated Contract' && generated) continue
          if (
            kind === 'private Gateway transport'
            && rel === 'src/adapters/gateway/gatewayAdapters.ts'
          ) continue
          failures.push(`${rel}: exported declaration exposes ${kind} symbols.`)
        }
      }
      if (
        ts.isExportAssignment(statement)
        && !privateBoundaryModule
        && !isTestingSupport(rel)
      ) {
        for (const kind of valueBoundaryKinds(statement.expression)) {
          if (kind === 'generated Contract' && generated) continue
          failures.push(`${rel}: default export exposes ${kind} symbols.`)
        }
      }
      if (
        ts.isExportDeclaration(statement)
        && !statement.moduleSpecifier
        && statement.exportClause
        && ts.isNamedExports(statement.exportClause)
        && !privateBoundaryModule
        && !isTestingSupport(rel)
      ) {
        for (const element of statement.exportClause.elements) {
          const kind = symbolOrigin(rawSymbol(element.propertyName ?? element.name))
          if (!kind) continue
          if (kind === 'generated Contract' && generated) continue
          failures.push(`${rel}: local export exposes ${kind} symbols.`)
        }
      }
      if (!privateBoundaryModule) {
        for (const kind of transitiveReexportBoundaryKinds(rel, statement)) {
          if (kind === 'generated Contract' && generated) continue
          failures.push(`${rel}: ${kind} modules must not be re-exported through a barrel.`)
        }
      }
    }

    function checkCjsExports(node) {
      if (
        ts.isBinaryExpression(node)
        && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && /^(?:module\.exports|exports)(?:\.[A-Za-z_$][\w$]*|\[['"][^'"]+['"]\])?$/.test(
          node.left.getText(source).replace(/\s/g, ''),
        )
      ) {
        for (const kind of valueBoundaryKinds(node.right)) {
          if (kind === 'generated Contract' && generated) continue
          failures.push(`${rel}: CommonJS export exposes ${kind} symbols.`)
        }
      }
      if (
        ts.isCallExpression(node)
        && ts.isPropertyAccessExpression(node.expression)
        && node.expression.expression.getText(source) === 'Object'
        && node.expression.name.text === 'assign'
        && node.arguments.length >= 2
        && /^(?:module\.exports|exports)$/.test(node.arguments[0].getText(source).replace(/\s/g, ''))
      ) {
        for (const argument of node.arguments.slice(1)) {
          for (const kind of valueBoundaryKinds(argument)) {
            if (kind === 'generated Contract' && generated) continue
            failures.push(`${rel}: CommonJS export exposes ${kind} symbols.`)
          }
        }
      }
      if (
        ts.isCallExpression(node)
        && ts.isPropertyAccessExpression(node.expression)
        && node.expression.expression.getText(source) === 'Object'
        && ['defineProperty', 'defineProperties'].includes(node.expression.name.text)
        && /^(?:module\.exports|exports)$/.test(
          node.arguments[0]?.getText(source).replace(/\s/g, '') ?? '',
        )
      ) {
        const descriptor = node.expression.name.text === 'defineProperty'
          ? node.arguments[2]
          : node.arguments[1]
        if (descriptor) {
          for (const kind of valueBoundaryKinds(descriptor)) {
            if (kind === 'generated Contract' && generated) continue
            failures.push(`${rel}: CommonJS export exposes ${kind} symbols.`)
          }
        }
      }
      ts.forEachChild(node, checkCjsExports)
    }
    if (!privateBoundaryModule) checkCjsExports(source)
  }
  return [...new Set(failures)]
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
