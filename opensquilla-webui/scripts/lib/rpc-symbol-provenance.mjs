import { resolve } from 'node:path'

import { resolveSourceImport } from './rpc-architecture-imports.mjs'

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

function moduleKey(path) {
  return path.replace(/\.(?:vue|[cm]?[jt]sx?)$/, '')
}

function unwrap(ts, expression) {
  let current = expression
  while (
    ts.isParenthesizedExpression(current)
    || ts.isAsExpression(current)
    || ts.isTypeAssertionExpression(current)
    || ts.isNonNullExpression(current)
    || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(current))
  ) current = current.expression
  return current
}

function propertyName(ts, node) {
  if (!node) return null
  if (ts.isIdentifier(node) || ts.isStringLiteralLike(node)) return node.text
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
    && ts.isStringLiteralLike(current.argumentExpression)
  ) {
    return { receiver: current.expression, member: current.argumentExpression.text }
  }
  return null
}

function expressionPath(ts, expression) {
  const current = unwrap(ts, expression)
  if (ts.isIdentifier(current)) return current.text
  if (ts.isPropertyAccessExpression(current)) {
    const parent = expressionPath(ts, current.expression)
    return parent ? `${parent}.${current.name.text}` : null
  }
  if (
    ts.isElementAccessExpression(current)
    && current.argumentExpression
    && ts.isStringLiteralLike(current.argumentExpression)
  ) {
    const parent = expressionPath(ts, current.expression)
    return parent ? `${parent}.${current.argumentExpression.text}` : null
  }
  return null
}

function typeReferenceName(ts, node) {
  if (!node) return null
  if (ts.isTypeReferenceNode(node) && ts.isIdentifier(node.typeName)) {
    return node.typeName.text
  }
  return null
}

function declarationMembers(ts, declaration) {
  if (ts.isInterfaceDeclaration(declaration)) return declaration.members
  if (
    ts.isTypeAliasDeclaration(declaration)
    && ts.isTypeLiteralNode(declaration.type)
  ) return declaration.type.members
  return []
}

function typeNodeReferences(ts, node, names) {
  let found = false
  function visit(current) {
    if (ts.isTypeReferenceNode(current) && ts.isIdentifier(current.typeName)) {
      if (names.has(current.typeName.text)) found = true
    } else if (
      ts.isTypeQueryNode(current)
      && ts.isIdentifier(current.exprName)
      && names.has(current.exprName.text)
    ) {
      found = true
    }
    if (!found) ts.forEachChild(current, visit)
  }
  visit(node)
  return found
}

function sourceModuleMetadata(ts, source) {
  const imports = new Map()
  const namespaces = new Map()
  const reexports = new Map()
  const localExports = new Map()
  const declaredExports = new Map()
  const starReexports = []

  function isExported(node) {
    return Boolean(ts.getModifiers(node)?.some(modifier => (
      modifier.kind === ts.SyntaxKind.ExportKeyword
    )))
  }

  for (const statement of source.statements) {
    if (
      ts.isImportDeclaration(statement)
      && ts.isStringLiteralLike(statement.moduleSpecifier)
      && statement.importClause
    ) {
      const specifier = statement.moduleSpecifier.text
      if (statement.importClause.name) {
        imports.set(statement.importClause.name.text, { specifier, imported: 'default' })
      }
      const bindings = statement.importClause.namedBindings
      if (bindings && ts.isNamespaceImport(bindings)) {
        namespaces.set(bindings.name.text, specifier)
      } else if (bindings && ts.isNamedImports(bindings)) {
        for (const element of bindings.elements) {
          imports.set(element.name.text, {
            specifier,
            imported: (element.propertyName ?? element.name).text,
          })
        }
      }
    }

    if (
      ts.isExportDeclaration(statement)
      && statement.moduleSpecifier
      && ts.isStringLiteralLike(statement.moduleSpecifier)
    ) {
      const specifier = statement.moduleSpecifier.text
      if (!statement.exportClause) {
        starReexports.push(specifier)
      } else if (ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          reexports.set(element.name.text, {
            specifier,
            imported: (element.propertyName ?? element.name).text,
          })
        }
      }
    } else if (
      ts.isExportDeclaration(statement)
      && statement.exportClause
      && ts.isNamedExports(statement.exportClause)
    ) {
      for (const element of statement.exportClause.elements) {
        localExports.set(
          element.name.text,
          (element.propertyName ?? element.name).text,
        )
      }
    }

    if (isExported(statement)) {
      if (
        (ts.isFunctionDeclaration(statement)
          || ts.isClassDeclaration(statement)
          || ts.isInterfaceDeclaration(statement)
          || ts.isTypeAliasDeclaration(statement))
        && statement.name
      ) {
        declaredExports.set(statement.name.text, statement.name.text)
      } else if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (ts.isIdentifier(declaration.name)) {
            declaredExports.set(declaration.name.text, declaration.name.text)
          }
        }
      }
    }
    if (
      ts.getModifiers(statement)?.some(modifier => (
        modifier.kind === ts.SyntaxKind.DefaultKeyword
      ))
      && statement.name
    ) {
      declaredExports.set('default', statement.name.text)
    }
  }
  return {
    imports,
    namespaces,
    reexports,
    localExports,
    declaredExports,
    starReexports,
  }
}

function requireSpecifier(ts, expression) {
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

function isDirectInvocation(ts, node) {
  let current = node
  while (
    current.parent
    && (
      ts.isParenthesizedExpression(current.parent)
      || ts.isAsExpression(current.parent)
      || ts.isTypeAssertionExpression(current.parent)
      || ts.isNonNullExpression(current.parent)
    )
  ) current = current.parent
  return Boolean(
    current.parent
    && ts.isCallExpression(current.parent)
    && unwrap(ts, current.parent.expression) === node,
  )
}

/**
 * Collect raw RPC operations from symbols proven to originate at the RPC
 * store/client boundary. Receiver spelling is never used as evidence.
 */
export function collectRpcTransportOperations({ ts, root, sources }) {
  const byModuleKey = new Map()
  for (const { rel } of sources) {
    byModuleKey.set(moduleKey(resolve(root, rel)), rel)
  }
  const metadata = new Map(
    sources.map(({ rel, source }) => [rel, sourceModuleMetadata(ts, source)]),
  )

  function resolveModule(importer, specifier) {
    const absolute = resolveSourceImport(root, importer, specifier)
    return absolute ? byModuleKey.get(moduleKey(absolute)) ?? null : null
  }

  function resolveExportOrigin(rel, exported, seen = new Set()) {
    const key = `${moduleKey(rel)}::${exported}`
    if (SEED_EXPORTS.has(key)) return SEED_EXPORTS.get(key)
    if (moduleKey(rel) === 'src/types/rpc') return 'wire-type'
    if (seen.has(key)) return null
    seen.add(key)
    const info = metadata.get(rel)
    if (!info) return null

    const direct = info.reexports.get(exported)
    if (direct) {
      const target = resolveModule(rel, direct.specifier)
      if (target) {
        const origin = resolveExportOrigin(target, direct.imported, seen)
        if (origin) return origin
      }
    }
    const local = info.localExports.get(exported)
    if (local) {
      const imported = info.imports.get(local)
      if (imported) {
        const target = resolveModule(rel, imported.specifier)
        if (target) {
          const origin = resolveExportOrigin(target, imported.imported, seen)
          if (origin) return origin
        }
      }
    }
    for (const specifier of info.starReexports) {
      const target = resolveModule(rel, specifier)
      if (!target) continue
      const origin = resolveExportOrigin(target, exported, seen)
      if (origin) return origin
    }
    return null
  }

  /** Resolve an exported name to its concrete declaration, through barrels. */
  function resolveExportBinding(rel, exported, seen = new Set()) {
    const key = `${rel}::${exported}`
    if (seen.has(key)) return null
    const nextSeen = new Set(seen).add(key)
    const info = metadata.get(rel)
    if (!info) return null

    const direct = info.reexports.get(exported)
    if (direct) {
      const target = resolveModule(rel, direct.specifier)
      if (target) {
        const binding = resolveExportBinding(target, direct.imported, nextSeen)
        if (binding) return binding
      }
    }

    const local = info.localExports.get(exported)
    if (local) {
      const imported = info.imports.get(local)
      if (imported) {
        const target = resolveModule(rel, imported.specifier)
        if (target) {
          const binding = resolveExportBinding(target, imported.imported, nextSeen)
          if (binding) return binding
        }
      }
      return { rel, local }
    }

    const declared = info.declaredExports.get(exported)
    if (declared) return { rel, local: declared }
    for (const specifier of info.starReexports) {
      const target = resolveModule(rel, specifier)
      if (!target) continue
      const binding = resolveExportBinding(target, exported, nextSeen)
      if (binding) return binding
    }
    return null
  }

  const stateByRel = new Map()
  for (const { rel, source } of sources) {
    const info = metadata.get(rel)
    const importedOrigins = new Map()
    const namespaceModules = new Map()
    for (const [local, imported] of info.imports) {
      const target = resolveModule(rel, imported.specifier)
      if (!target) continue
      const origin = resolveExportOrigin(target, imported.imported)
      if (origin) importedOrigins.set(local, origin)
    }
    for (const [local, specifier] of info.namespaces) {
      const target = resolveModule(rel, specifier)
      if (target) namespaceModules.set(local, target)
    }

    // CommonJS imports participate in the same provenance graph.
    function collectRequires(node) {
      if (ts.isVariableDeclaration(node) && node.initializer) {
        const specifier = requireSpecifier(ts, node.initializer)
        const target = specifier ? resolveModule(rel, specifier) : null
        if (target && ts.isIdentifier(node.name)) {
          namespaceModules.set(node.name.text, target)
        } else if (target && ts.isObjectBindingPattern(node.name)) {
          for (const element of node.name.elements) {
            if (!ts.isIdentifier(element.name)) continue
            const imported = propertyName(ts, element.propertyName ?? element.name)
            const origin = imported && resolveExportOrigin(target, imported)
            if (origin) importedOrigins.set(element.name.text, origin)
          }
        }
      }
      ts.forEachChild(node, collectRequires)
    }
    collectRequires(source)

    const factoryNames = new Set(
      [...importedOrigins]
        .filter(([, origin]) => origin === 'factory')
        .map(([name]) => name),
    )
    const clientTypeNames = new Set(
      [...importedOrigins]
        .filter(([, origin]) => origin === 'client-type')
        .map(([name]) => name),
    )
    const typeDeclarations = new Map()
    function collectTypes(node) {
      if (ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) {
        typeDeclarations.set(node.name.text, node)
      }
      ts.forEachChild(node, collectTypes)
    }
    collectTypes(source)

    // Only a type that ultimately names the RPC boundary is provenance. A
    // structurally similar local interface is deliberately not a seed.
    const rpcTypeNames = new Set(clientTypeNames)
    let typeChanged = true
    while (typeChanged) {
      typeChanged = false
      for (const [name, declaration] of typeDeclarations) {
        if (rpcTypeNames.has(name)) continue
        const knownRpcTypes = new Set([...rpcTypeNames, ...clientTypeNames])
        const aliasesRpcType = (
          ts.isTypeAliasDeclaration(declaration)
          && !ts.isTypeLiteralNode(declaration.type)
          && typeNodeReferences(ts, declaration.type, knownRpcTypes)
        )
        const extendsRpcType = (
          ts.isInterfaceDeclaration(declaration)
          && declaration.heritageClauses?.some(clause => (
            typeNodeReferences(ts, clause, knownRpcTypes)
          ))
        )
        if (aliasesRpcType || extendsRpcType) {
          rpcTypeNames.add(name)
          typeChanged = true
        }
      }
    }

    const rpcPropertiesByType = new Map()
    for (const [name, declaration] of typeDeclarations) {
      const properties = new Set()
      for (const member of declarationMembers(ts, declaration)) {
        const property = propertyName(ts, member.name)
        if (
          property
          && member.type
          && typeNodeReferences(ts, member.type, new Set([...rpcTypeNames, ...factoryNames]))
        ) properties.add(property)
      }
      if (properties.size) rpcPropertiesByType.set(name, properties)
    }

    const functions = new Map()
    for (const statement of source.statements) {
      if (ts.isFunctionDeclaration(statement) && statement.name) {
        functions.set(statement.name.text, statement)
      } else if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (
            ts.isIdentifier(declaration.name)
            && declaration.initializer
            && (
              ts.isArrowFunction(declaration.initializer)
              || ts.isFunctionExpression(declaration.initializer)
            )
          ) functions.set(declaration.name.text, declaration.initializer)
        }
      }
    }

    stateByRel.set(rel, {
      rel,
      source,
      info,
      importedOrigins,
      namespaceModules,
      factoryNames,
      rpcTypeNames,
      rpcPropertiesByType,
      functions,
      rpcObjects: new Set(),
      rpcPropertyPaths: new Set(),
      rpcCapabilities: new Map(),
      returnShapes: new Map(),
      rpcMemberPaths: new Map(),
      returnMemberShapes: new Map(),
    })
  }

  function resolveCallable(state, expression) {
    const current = unwrap(ts, expression)
    if (ts.isIdentifier(current)) {
      if (state.functions.has(current.text)) {
        return { state, local: current.text }
      }
      const imported = state.info.imports.get(current.text)
      if (!imported) return null
      const target = resolveModule(state.rel, imported.specifier)
      const binding = target
        ? resolveExportBinding(target, imported.imported)
        : null
      const targetState = binding ? stateByRel.get(binding.rel) : null
      return targetState && targetState.functions.has(binding.local)
        ? { state: targetState, local: binding.local }
        : null
    }
    const access = memberAccess(ts, current)
    if (access && ts.isIdentifier(access.receiver)) {
      const target = state.namespaceModules.get(access.receiver.text)
      const binding = target ? resolveExportBinding(target, access.member) : null
      const targetState = binding ? stateByRel.get(binding.rel) : null
      return targetState && targetState.functions.has(binding.local)
        ? { state: targetState, local: binding.local }
        : null
    }
    return null
  }

  function factoryOriginForExpression(state, expression) {
    const current = unwrap(ts, expression)
    if (ts.isIdentifier(current)) {
      return state.importedOrigins.get(current.text) ?? null
    }
    const access = memberAccess(ts, current)
    if (access && ts.isIdentifier(access.receiver)) {
      const target = state.namespaceModules.get(access.receiver.text)
      return target ? resolveExportOrigin(target, access.member) : null
    }
    return null
  }

  function typeIsRpc(state, type) {
    const name = typeReferenceName(ts, type)
    if (name && state.rpcTypeNames.has(name)) return true
    if (!type || ts.isTypeLiteralNode(type)) return false
    return typeNodeReferences(
      ts,
      type,
      new Set([...state.rpcTypeNames, ...state.factoryNames]),
    )
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

  function pathShape(state, path) {
    const shape = new Set()
    for (const candidate of [...state.rpcObjects, ...state.rpcPropertyPaths]) {
      if (candidate === path) shape.add('')
      else if (candidate.startsWith(`${path}.`)) shape.add(candidate.slice(path.length + 1))
    }
    return shape
  }

  function prependShape(prefix, shape) {
    return new Set([...shape].map(suffix => suffix ? `${prefix}.${suffix}` : prefix))
  }

  const memberShapeSeparator = '\0'

  function encodeMemberShape(path, member) {
    return `${path}${memberShapeSeparator}${member}`
  }

  function decodeMemberShape(encoded) {
    const separator = encoded.lastIndexOf(memberShapeSeparator)
    return {
      path: encoded.slice(0, separator),
      member: encoded.slice(separator + 1),
    }
  }

  function prependMemberShape(prefix, shape) {
    return new Set([...shape].map((encoded) => {
      const { path, member } = decodeMemberShape(encoded)
      return encodeMemberShape(path ? `${prefix}.${path}` : prefix, member)
    }))
  }

  function rpcShapeForExpression(state, expression) {
    const current = unwrap(ts, expression)
    const path = expressionPath(ts, current)
    if (path) {
      const shape = pathShape(state, path)
      if (shape.size) return shape
    }
    if (ts.isCallExpression(current)) {
      if (factoryOriginForExpression(state, current.expression) === 'factory') {
        return new Set([''])
      }
      const target = resolveCallable(state, current.expression)
      return target
        ? new Set(target.state.returnShapes.get(target.local) ?? [])
        : new Set()
    }
    if (ts.isNewExpression(current) && current.expression) {
      return factoryOriginForExpression(state, current.expression) === 'client-type'
        ? new Set([''])
        : new Set()
    }
    if (ts.isObjectLiteralExpression(current)) {
      const shape = new Set()
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) {
          for (const suffix of rpcShapeForExpression(state, property.expression)) {
            shape.add(suffix)
          }
        } else if (
          ts.isPropertyAssignment(property)
          || ts.isShorthandPropertyAssignment(property)
        ) {
          const name = propertyName(ts, property.name)
          const initializer = ts.isShorthandPropertyAssignment(property)
            ? property.name
            : property.initializer
          if (!name) continue
          for (const suffix of prependShape(name, rpcShapeForExpression(state, initializer))) {
            shape.add(suffix)
          }
        }
      }
      return shape
    }
    if (ts.isConditionalExpression(current)) {
      return new Set([
        ...rpcShapeForExpression(state, current.whenTrue),
        ...rpcShapeForExpression(state, current.whenFalse),
      ])
    }
    return new Set()
  }

  function isRpcExpression(state, expression) {
    return rpcShapeForExpression(state, expression).has('')
  }

  function isRpcMemberReceiver(state, expression, member) {
    if (isRpcExpression(state, expression)) return true
    const path = expressionPath(ts, expression)
    return Boolean(path && state.rpcMemberPaths.get(path)?.has(member))
  }

  function rpcFunctionMembers(state, expression) {
    const current = unwrap(ts, expression)
    if (ts.isIdentifier(current)) {
      return new Set(state.rpcCapabilities.get(current.text) ?? [])
    }
    const direct = memberAccess(ts, current)
    if (
      direct
      && TRACKED_RPC_MEMBERS.includes(direct.member)
      && isRpcMemberReceiver(state, direct.receiver, direct.member)
    ) return new Set([direct.member])

    if (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) {
      const members = new Set()
      function visit(node) {
        if (ts.isFunctionLike(node) && node !== current) return
        const access = memberAccess(ts, node)
        if (
          access
          && TRACKED_RPC_MEMBERS.includes(access.member)
          && isRpcMemberReceiver(state, access.receiver, access.member)
        ) members.add(access.member)
        ts.forEachChild(node, visit)
      }
      visit(current)
      return members
    }
    return new Set()
  }

  function rpcMemberShapeForExpression(state, expression) {
    const current = unwrap(ts, expression)
    const path = expressionPath(ts, current)
    if (path) {
      const shape = new Set()
      for (const [candidate, members] of state.rpcMemberPaths) {
        if (candidate !== path && !candidate.startsWith(`${path}.`)) continue
        const suffix = candidate === path ? '' : candidate.slice(path.length + 1)
        for (const member of members) shape.add(encodeMemberShape(suffix, member))
      }
      if (shape.size) return shape
    }
    if (ts.isCallExpression(current)) {
      const target = resolveCallable(state, current.expression)
      return target
        ? new Set(target.state.returnMemberShapes.get(target.local) ?? [])
        : new Set()
    }
    if (ts.isObjectLiteralExpression(current)) {
      const shape = new Set()
      for (const property of current.properties) {
        if (ts.isSpreadAssignment(property)) {
          for (const encoded of rpcMemberShapeForExpression(state, property.expression)) {
            shape.add(encoded)
          }
          continue
        }
        if (
          !ts.isPropertyAssignment(property)
          && !ts.isShorthandPropertyAssignment(property)
        ) continue
        const name = propertyName(ts, property.name)
        const initializer = ts.isShorthandPropertyAssignment(property)
          ? property.name
          : property.initializer
        if (!name) continue
        for (const encoded of prependMemberShape(
          name,
          rpcMemberShapeForExpression(state, initializer),
        )) shape.add(encoded)
        if (TRACKED_RPC_MEMBERS.includes(name)) {
          const forwardedMembers = rpcFunctionMembers(state, initializer)
          if (forwardedMembers.has(name)) shape.add(encodeMemberShape('', name))
        }
      }
      return shape
    }
    if (ts.isConditionalExpression(current)) {
      return new Set([
        ...rpcMemberShapeForExpression(state, current.whenTrue),
        ...rpcMemberShapeForExpression(state, current.whenFalse),
      ])
    }
    return new Set()
  }

  function bindShapeAtPath(state, path, shape) {
    let changed = false
    for (const suffix of shape) {
      const target = suffix ? `${path}.${suffix}` : path
      if (suffix) changed = addSetValue(state.rpcPropertyPaths, target) || changed
      else changed = addSetValue(state.rpcObjects, target) || changed
    }
    return changed
  }

  function bindMemberShapeAtPath(state, path, shape) {
    let changed = false
    const grouped = new Map()
    for (const encoded of shape) {
      const { path: suffix, member } = decodeMemberShape(encoded)
      const target = suffix ? `${path}.${suffix}` : path
      let members = grouped.get(target)
      if (!members) {
        members = new Set()
        grouped.set(target, members)
      }
      members.add(member)
    }
    for (const [target, incoming] of grouped) {
      let members = state.rpcMemberPaths.get(target)
      // A lone callback named supportsMethod/on/etc. is an ordinary option,
      // not an RPC client. A forwarded `call` capability anchors the receiver
      // to the RPC boundary; its sibling lifecycle capabilities then inherit
      // the same provenance. Direct RpcClient objects are tracked separately.
      if (
        !incoming.has('call')
        && !members?.has('call')
        && !state.rpcObjects.has(target)
      ) continue
      if (!members) {
        members = new Set()
        state.rpcMemberPaths.set(target, members)
      }
      for (const member of incoming) {
        changed = addSetValue(members, member) || changed
      }
    }
    return changed
  }

  function bindPattern(state, pattern, shape, memberShape = new Set()) {
    let changed = false
    if (ts.isIdentifier(pattern)) {
      changed = bindShapeAtPath(state, pattern.text, shape) || changed
      return bindMemberShapeAtPath(state, pattern.text, memberShape) || changed
    }
    if (!ts.isObjectBindingPattern(pattern)) return false
    for (const element of pattern.elements) {
      if (!ts.isIdentifier(element.name)) continue
      const member = propertyName(ts, element.propertyName ?? element.name)
      if (!member) continue
      if (shape.has('') && TRACKED_RPC_MEMBERS.includes(member)) {
        let capabilities = state.rpcCapabilities.get(element.name.text)
        if (!capabilities) {
          capabilities = new Set()
          state.rpcCapabilities.set(element.name.text, capabilities)
        }
        changed = addSetValue(capabilities, member) || changed
      }
      const nestedShape = new Set()
      for (const suffix of shape) {
        if (suffix === member) nestedShape.add('')
        else if (suffix.startsWith(`${member}.`)) nestedShape.add(suffix.slice(member.length + 1))
      }
      changed = bindShapeAtPath(state, element.name.text, nestedShape) || changed

      const nestedMemberShape = new Set()
      for (const encoded of memberShape) {
        const { path, member: capability } = decodeMemberShape(encoded)
        if (path === member) nestedMemberShape.add(encodeMemberShape('', capability))
        else if (path.startsWith(`${member}.`)) {
          nestedMemberShape.add(encodeMemberShape(path.slice(member.length + 1), capability))
        }
        if (path === '' && capability === member) {
          let capabilities = state.rpcCapabilities.get(element.name.text)
          if (!capabilities) {
            capabilities = new Set()
            state.rpcCapabilities.set(element.name.text, capabilities)
          }
          changed = addSetValue(capabilities, capability) || changed
        }
      }
      changed = bindMemberShapeAtPath(
        state,
        element.name.text,
        nestedMemberShape,
      ) || changed
    }
    return changed
  }

  // Explicit boundary types are initial provenance seeds. Structural types
  // gain provenance only when a value descended from a seed flows into them.
  for (const state of stateByRel.values()) {
    function collectTypedBindings(node) {
      if ((ts.isParameter(node) || ts.isVariableDeclaration(node)) && node.type) {
        if (typeIsRpc(state, node.type)) bindPattern(state, node.name, new Set(['']))
        const typeName = typeReferenceName(ts, node.type)
        if (ts.isIdentifier(node.name)) {
          for (const property of state.rpcPropertiesByType.get(typeName) ?? []) {
            state.rpcPropertyPaths.add(`${node.name.text}.${property}`)
          }
        }
      }
      ts.forEachChild(node, collectTypedBindings)
    }
    collectTypedBindings(state.source)
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
      if (ts.isVariableDeclaration(node) && node.initializer) {
        changed = bindPattern(
          state,
          node.name,
          rpcShapeForExpression(state, node.initializer),
          rpcMemberShapeForExpression(state, node.initializer),
        ) || changed
      }
      if (
        ts.isBinaryExpression(node)
        && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      ) {
        const target = expressionPath(ts, node.left)
        if (target) {
          changed = bindShapeAtPath(
            state,
            target,
            rpcShapeForExpression(state, node.right),
          ) || changed
          changed = bindMemberShapeAtPath(
            state,
            target,
            rpcMemberShapeForExpression(state, node.right),
          ) || changed
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(state.source)

    for (const [name, functionNode] of state.functions) {
      for (const expression of returnExpressions(functionNode)) {
        for (const suffix of rpcShapeForExpression(state, expression)) {
          changed = addMapSetValue(state.returnShapes, name, suffix) || changed
        }
        for (const encoded of rpcMemberShapeForExpression(state, expression)) {
          changed = addMapSetValue(state.returnMemberShapes, name, encoded) || changed
        }
      }
    }
    return changed
  }

  function propagateCalls(state) {
    let changed = false
    function visit(node) {
      if (ts.isCallExpression(node)) {
        const target = resolveCallable(state, node.expression)
        const functionNode = target && target.state.functions.get(target.local)
        if (target && functionNode) {
          for (let index = 0; index < functionNode.parameters.length; index += 1) {
            const argument = node.arguments[index]
            if (!argument || ts.isSpreadElement(argument)) continue
            changed = bindPattern(
              target.state,
              functionNode.parameters[index].name,
              rpcShapeForExpression(state, argument),
              rpcMemberShapeForExpression(state, argument),
            ) || changed
          }
        }
      }
      ts.forEachChild(node, visit)
    }
    visit(state.source)
    return changed
  }

  // Whole-program fixed point: an RPC seed can be wrapped in an object,
  // returned by a factory, then passed through several imported composables.
  // Every reported member must therefore retain a concrete path back to a
  // store/client seed rather than merely having a compatible shape.
  let changed = true
  while (changed) {
    changed = false
    for (const state of stateByRel.values()) {
      changed = propagateLocal(state) || changed
    }
    for (const state of stateByRel.values()) {
      changed = propagateCalls(state) || changed
    }
  }

  const operations = []
  for (const state of stateByRel.values()) {
    function collectMemberOperations(node) {
      const access = memberAccess(ts, node)
      if (
        access
        && TRACKED_RPC_MEMBERS.includes(access.member)
        && isRpcMemberReceiver(state, access.receiver, access.member)
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
        && ts.isObjectBindingPattern(node.parent)
        && ts.isIdentifier(node.name)
      ) {
        for (const member of state.rpcCapabilities.get(node.name.text) ?? []) {
          operations.push({ rel: state.rel, kind: `${member}Reference` })
        }
      }
      ts.forEachChild(node, collectMemberOperations)
    }
    collectMemberOperations(state.source)
  }
  return operations
}
