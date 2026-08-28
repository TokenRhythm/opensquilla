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

function functionTypeForMember(ts, member) {
  if (ts.isMethodSignature(member) || ts.isMethodDeclaration(member)) return member
  if (
    ts.isPropertySignature(member)
    && member.type
    && ts.isFunctionTypeNode(member.type)
  ) return member.type
  return null
}

function isStringType(ts, node) {
  return Boolean(node && node.kind === ts.SyntaxKind.StringKeyword)
}

function isRpcCallMember(ts, member) {
  if (propertyName(ts, member.name) !== 'call') return false
  const signature = functionTypeForMember(ts, member)
  if (!signature || signature.parameters.length < 1) return false
  return isStringType(ts, signature.parameters[0].type)
}

function typeLiteralLooksLikeRpc(ts, node, supportTypeNames) {
  if (!node || !ts.isTypeLiteralNode(node)) return false
  const names = new Set(node.members.map(member => propertyName(ts, member.name)))
  if (node.members.some(member => isRpcCallMember(ts, member))) return true
  if (names.has('waitForConnection') && !names.has('rpc')) return true
  return node.members.some(member => (
    propertyName(ts, member.name) === 'on'
    && typeNodeReferences(ts, member, supportTypeNames)
  ))
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
  const starReexports = []

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
  }
  return { imports, namespaces, reexports, localExports, starReexports }
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

  const operations = []
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

    const supportTypeNames = new Set(
      [...importedOrigins]
        .filter(([, origin]) => ['rpc-support-type', 'wire-type', 'client-type'].includes(origin))
        .map(([name]) => name),
    )
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

    const rpcTypeNames = new Set(clientTypeNames)
    let changed = true
    while (changed) {
      changed = false
      for (const [name, declaration] of typeDeclarations) {
        if (rpcTypeNames.has(name)) continue
        const members = declarationMembers(ts, declaration)
        const memberNames = new Set(members.map(member => propertyName(ts, member.name)))
        const hasDistinctiveMember = (
          members.some(member => isRpcCallMember(ts, member))
          || (memberNames.has('waitForConnection') && !memberNames.has('rpc'))
          || (
            !memberNames.has('rpc')
            && members.length <= 8
            && ['supportsMethod', 'supportsEvent', 'markMethodUnavailable']
              .some(member => memberNames.has(member))
          )
        )
        const hasEventHandler = members.some(member => (
          propertyName(ts, member.name) === 'on'
          && typeNodeReferences(ts, member, supportTypeNames)
        ))
        const aliasesRpcType = ts.isTypeAliasDeclaration(declaration)
          && typeNodeReferences(ts, declaration.type, new Set([...rpcTypeNames, ...factoryNames]))
        if (hasDistinctiveMember || hasEventHandler || aliasesRpcType) {
          rpcTypeNames.add(name)
          changed = true
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
          && (
            typeNodeReferences(ts, member.type, new Set([...rpcTypeNames, ...factoryNames]))
            || typeLiteralLooksLikeRpc(ts, member.type, supportTypeNames)
          )
        ) {
          properties.add(property)
        }
      }
      if (properties.size) rpcPropertiesByType.set(name, properties)
    }

    const rpcObjects = new Set()
    const rpcPropertyPaths = new Set()
    const rpcCapabilities = new Map()

    function factoryOriginForExpression(expression) {
      const current = unwrap(ts, expression)
      if (ts.isIdentifier(current)) return importedOrigins.get(current.text) ?? null
      const access = memberAccess(ts, current)
      if (access && ts.isIdentifier(access.receiver)) {
        const target = namespaceModules.get(access.receiver.text)
        return target ? resolveExportOrigin(target, access.member) : null
      }
      return null
    }

    function typeIsRpc(type) {
      const name = typeReferenceName(ts, type)
      if (name && rpcTypeNames.has(name)) return true
      return Boolean(type && (
        typeNodeReferences(ts, type, new Set([...rpcTypeNames, ...factoryNames]))
        || typeLiteralLooksLikeRpc(ts, type, supportTypeNames)
      ))
    }

    function addTypedBinding(name, type) {
      if (!type || !ts.isIdentifier(name)) return
      if (typeIsRpc(type)) rpcObjects.add(name.text)
      const typeName = typeReferenceName(ts, type)
      for (const property of rpcPropertiesByType.get(typeName) ?? []) {
        rpcPropertyPaths.add(`${name.text}.${property}`)
      }
    }

    function collectTypedBindings(node) {
      if (ts.isParameter(node) || ts.isVariableDeclaration(node)) {
        addTypedBinding(node.name, node.type)
      }
      ts.forEachChild(node, collectTypedBindings)
    }
    collectTypedBindings(source)

    function isRpcExpression(expression) {
      const current = unwrap(ts, expression)
      if (ts.isIdentifier(current)) return rpcObjects.has(current.text)
      const path = expressionPath(ts, current)
      if (path && rpcPropertyPaths.has(path)) return true
      if (ts.isCallExpression(current)) {
        return factoryOriginForExpression(current.expression) === 'factory'
      }
      if (ts.isNewExpression(current) && current.expression) {
        return factoryOriginForExpression(current.expression) === 'client-type'
      }
      return false
    }

    function propagateBindings() {
      let didChange = false
      function visit(node) {
        if (ts.isVariableDeclaration(node) && node.initializer) {
          if (ts.isIdentifier(node.name) && isRpcExpression(node.initializer)) {
            if (!rpcObjects.has(node.name.text)) {
              rpcObjects.add(node.name.text)
              didChange = true
            }
          } else if (ts.isObjectBindingPattern(node.name)) {
            const sourceIsRpc = isRpcExpression(node.initializer)
            const sourcePath = expressionPath(ts, node.initializer)
            for (const element of node.name.elements) {
              if (!ts.isIdentifier(element.name)) continue
              const property = propertyName(ts, element.propertyName ?? element.name)
              if (!property) continue
              if (sourceIsRpc && TRACKED_RPC_MEMBERS.includes(property)) {
                if (!rpcCapabilities.has(element.name.text)) {
                  rpcCapabilities.set(element.name.text, property)
                  didChange = true
                }
              } else if (
                sourcePath
                && rpcPropertyPaths.has(`${sourcePath}.${property}`)
                && !rpcObjects.has(element.name.text)
              ) {
                rpcObjects.add(element.name.text)
                didChange = true
              }
            }
          }
        }
        if (
          ts.isBinaryExpression(node)
          && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
          && ts.isIdentifier(node.left)
          && isRpcExpression(node.right)
          && !rpcObjects.has(node.left.text)
        ) {
          rpcObjects.add(node.left.text)
          didChange = true
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
      return didChange
    }
    while (propagateBindings()) {}

    function collectMemberOperations(node) {
      const access = memberAccess(ts, node)
      if (
        access
        && TRACKED_RPC_MEMBERS.includes(access.member)
        && isRpcExpression(access.receiver)
        && !ts.isTypeOfExpression(node.parent)
      ) {
        operations.push({
          rel,
          kind: isDirectInvocation(ts, node)
            ? access.member
            : `${access.member}Reference`,
        })
      }
      if (
        ts.isBindingElement(node)
        && ts.isObjectBindingPattern(node.parent)
      ) {
        const owner = node.parent.parent
        const sourceIsRpc = (
          ts.isVariableDeclaration(owner)
          && Boolean(owner.initializer)
          && isRpcExpression(owner.initializer)
        ) || (
          ts.isParameter(owner)
          && Boolean(owner.type)
          && typeIsRpc(owner.type)
        )
        const member = propertyName(ts, node.propertyName ?? node.name)
        if (sourceIsRpc && member && TRACKED_RPC_MEMBERS.includes(member)) {
          operations.push({ rel, kind: `${member}Reference` })
        }
      }
      ts.forEachChild(node, collectMemberOperations)
    }
    collectMemberOperations(source)
  }
  return operations
}
