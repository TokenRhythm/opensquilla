export const TRACKED_HTTP_KINDS = [
  'httpApiEndpoint',
  'httpAuthToken',
  'httpAuthorizationHeader',
  'httpSessionKeyHeader',
]

const API_PATH = /^\/api(?:\/|$|[?#])/
const TOKEN_STORAGE_KEY = 'opensquilla.wsToken'
const AUTHORIZATION_HEADER = 'authorization'
const SESSION_KEY_HEADER = 'x-opensquilla-session-key'

function literalText(ts, node) {
  if (ts.isStringLiteralLike(node)) return node.text
  return null
}

function templateStartsWithApi(ts, node) {
  return ts.isTemplateExpression(node) && API_PATH.test(node.head.text.trimStart())
}

function headerKind(name) {
  const normalized = name.trim().toLowerCase()
  if (normalized === AUTHORIZATION_HEADER) return 'httpAuthorizationHeader'
  if (normalized === SESSION_KEY_HEADER) return 'httpSessionKeyHeader'
  return null
}

function propertyNameText(ts, name) {
  if (ts.isIdentifier(name) || ts.isStringLiteralLike(name)) return name.text
  return null
}

function semanticHeaderKind(ts, node) {
  if (ts.isPropertyAccessExpression(node)) return headerKind(node.name.text)
  if (
    ts.isElementAccessExpression(node)
    && node.argumentExpression
    && ts.isStringLiteralLike(node.argumentExpression)
  ) {
    return headerKind(node.argumentExpression.text)
  }
  if (ts.isPropertyAssignment(node)) {
    const name = propertyNameText(ts, node.name)
    return name ? headerKind(name) : null
  }
  if (
    ts.isCallExpression(node)
    && (
      ts.isPropertyAccessExpression(node.expression)
      || ts.isElementAccessExpression(node.expression)
    )
    && node.arguments.length > 0
    && ts.isStringLiteralLike(node.arguments[0])
  ) {
    let member = ''
    if (ts.isPropertyAccessExpression(node.expression)) {
      member = node.expression.name.text
    } else if (
      node.expression.argumentExpression
      && ts.isStringLiteralLike(node.expression.argumentExpression)
    ) {
      member = node.expression.argumentExpression.text
    }
    if (['append', 'delete', 'get', 'has', 'set'].includes(member)) {
      return headerKind(node.arguments[0].text)
    }
  }
  return null
}

/**
 * Collect authored HTTP boundary details without treating every fetch as API
 * debt. Static assets, data/blob URLs, and external resources have no tracked
 * Gateway endpoint or credential literal and therefore remain outside the
 * population.
 */
export function collectHttpBoundaryOperations({ ts, sources }) {
  const operations = []
  for (const { rel, source } of sources) {
    function visit(node) {
      const text = literalText(ts, node)
      if (text !== null) {
        if (API_PATH.test(text.trimStart())) {
          operations.push({ rel, kind: 'httpApiEndpoint' })
        }
        if (text === TOKEN_STORAGE_KEY) {
          operations.push({ rel, kind: 'httpAuthToken' })
        }
      } else if (templateStartsWithApi(ts, node)) {
        operations.push({ rel, kind: 'httpApiEndpoint' })
      }

      const header = semanticHeaderKind(ts, node)
      if (header) operations.push({ rel, kind: header })
      ts.forEachChild(node, visit)
    }
    visit(source)
  }
  return operations
}
