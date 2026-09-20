import { canonicalSessionKey } from '@/utils/chat/sessionKeys'

export const REFERENCE_V1 = 1 as const

export type ReferenceKind = 'session' | 'workspace_file' | 'workspace_directory' | 'artifact' | 'document' | 'external_url'
export type ReferenceRunStatus = 'queued' | 'running' | 'idle' | 'failed' | 'missing'

export interface ReferenceState {
  available: boolean
  runStatus?: ReferenceRunStatus | null
  revision?: string
}

export interface ReferenceV1 {
  version: typeof REFERENCE_V1
  kind: ReferenceKind
  id: string
  label: string
  scope: { sessionKey?: string; workspaceId?: string; gatewayInstanceId?: string }
  locator?: { relativePath?: string; startLine?: number; endLine?: number; pagePath?: string }
  state?: ReferenceState
  capabilities: {
    open?: boolean
    copy?: boolean
    download?: boolean
    reveal?: boolean
  }
}

export type SessionReferenceV1 = Omit<ReferenceV1, 'kind' | 'scope' | 'state'> & {
  kind: 'session'
  scope: ReferenceV1['scope'] & { sessionKey: string }
  state: ReferenceState & { runStatus: ReferenceRunStatus | null }
}

export type WorkspaceFileReferenceV1 = ReferenceV1 & {
  kind: 'workspace_file'
  locator: { relativePath: string; startLine?: number; endLine?: number }
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function nonEmptyString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function safeRelativePath(value: string): boolean {
  const candidate = value
  return Boolean(candidate)
    && !candidate.startsWith('/')
    && !/[\\:\u0000-\u001f\u007f]/.test(candidate)
    && candidate.split('/').every(part => part && part !== '.' && part !== '..')
}

const REFERENCE_RUN_STATUSES = new Set<ReferenceRunStatus>([
  'queued', 'running', 'idle', 'failed', 'missing',
])

/** Normalize only the stable ReferenceV1 wire shape emitted by the Gateway. */
export function normalizeSessionReferenceV1(value: unknown): SessionReferenceV1 | null {
  const source = record(value)
  if (!source || source.version !== REFERENCE_V1 || source.kind !== 'session') return null
  const rawId = nonEmptyString(source.id)
  const scope = record(source.scope)
  const rawKey = nonEmptyString(scope?.sessionKey) || rawId
  // Persisted display previews may shorten identity strings. They are not
  // resolvable references: never use a prefix or merge distinct shortened IDs.
  if (/[\u2026]|\.{3}/.test(rawId) || /[\u2026]|\.{3}/.test(rawKey)) return null
  if (!rawKey || rawKey.length > 2048 || /[\u0000-\u001f\u007f]/.test(rawKey)) return null
  const key = canonicalSessionKey(rawKey)
  if (rawId && canonicalSessionKey(rawId) !== key) return null
  const state = record(source.state)
  const capabilities = record(source.capabilities)
  const revision = nonEmptyString(state?.revision)
  const runStatus = typeof state?.runStatus === 'string'
    && REFERENCE_RUN_STATUSES.has(state.runStatus as ReferenceRunStatus)
    ? state.runStatus as ReferenceRunStatus
    : null
  const label = nonEmptyString(source.label) || key
  return {
    version: REFERENCE_V1,
    kind: 'session',
    id: key,
    label: label.slice(0, 512),
    scope: {
      sessionKey: key,
      ...(nonEmptyString(scope?.gatewayInstanceId) ? { gatewayInstanceId: nonEmptyString(scope?.gatewayInstanceId) } : {}),
    },
    state: {
      available: state?.available !== false,
      runStatus,
      ...(revision ? { revision: revision.slice(0, 256) } : {}),
    },
    capabilities: {
      open: capabilities?.open !== false,
      copy: capabilities?.copy !== false,
    },
  }
}

/** Normalize any supported ReferenceV1 value; session references get key canonicalization. */
export function normalizeReferenceV1(value: unknown): ReferenceV1 | null {
  const source = record(value)
  if (!source || source.version !== REFERENCE_V1) return null
  if (source.kind === 'session') return normalizeSessionReferenceV1(source)
  if (typeof source.kind !== 'string' || ![
    'workspace_file', 'workspace_directory', 'artifact', 'document', 'external_url',
  ].includes(source.kind)) return null
  const id = nonEmptyString(source.id)
  if (!id) return null
  if (source.kind === 'external_url') {
    try {
      const url = new URL(id)
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null
    } catch {
      return null
    }
  }
  const scopeSource = record(source.scope)
  const state = record(source.state)
  const capabilities = record(source.capabilities)
  const locatorSource = record(source.locator)
  const startLine = typeof locatorSource?.startLine === 'number'
    && Number.isSafeInteger(locatorSource.startLine) && locatorSource.startLine > 0
    ? locatorSource.startLine
    : undefined
  const endLine = typeof locatorSource?.endLine === 'number'
    && Number.isSafeInteger(locatorSource.endLine) && locatorSource.endLine > 0
    ? locatorSource.endLine
    : undefined
  const locator = locatorSource
    ? {
        ...(nonEmptyString(locatorSource.relativePath)
          ? { relativePath: nonEmptyString(locatorSource.relativePath).slice(0, 4096) }
          : {}),
        ...(startLine !== undefined ? { startLine } : {}),
        ...(endLine !== undefined ? { endLine } : {}),
        ...(nonEmptyString(locatorSource.pagePath)
          ? { pagePath: nonEmptyString(locatorSource.pagePath).slice(0, 4096) }
          : {}),
      }
    : undefined
  if (
    (source.kind === 'workspace_file' || source.kind === 'workspace_directory')
    && (!locator?.relativePath || !safeRelativePath(locator.relativePath)
      || locatorSource?.relativePath !== locator.relativePath
      || (locatorSource?.startLine !== undefined && startLine === undefined)
      || (locatorSource?.endLine !== undefined && endLine === undefined)
      || (endLine !== undefined && endLine < (startLine ?? 1)))
  ) return null
  const revision = nonEmptyString(state?.revision)
  const runStatus = typeof state?.runStatus === 'string'
    && REFERENCE_RUN_STATUSES.has(state.runStatus as ReferenceRunStatus)
    ? state.runStatus as ReferenceRunStatus
    : null
  return {
    version: REFERENCE_V1,
    kind: source.kind as Exclude<ReferenceKind, 'session'>,
    id,
    label: (nonEmptyString(source.label) || id).slice(0, 512),
    scope: {
      ...(nonEmptyString(scopeSource?.sessionKey) ? { sessionKey: nonEmptyString(scopeSource?.sessionKey) } : {}),
      ...(nonEmptyString(scopeSource?.workspaceId) ? { workspaceId: nonEmptyString(scopeSource?.workspaceId) } : {}),
      ...(nonEmptyString(scopeSource?.gatewayInstanceId) ? { gatewayInstanceId: nonEmptyString(scopeSource?.gatewayInstanceId) } : {}),
    },
    ...(locator && Object.keys(locator).length ? { locator } : {}),
    state: {
      available: state?.available !== false,
      ...(runStatus ? { runStatus } : {}),
      ...(revision ? { revision: revision.slice(0, 256) } : {}),
    },
    capabilities: {
      ...(typeof capabilities?.open === 'boolean' ? { open: capabilities.open } : {}),
      ...(typeof capabilities?.copy === 'boolean' ? { copy: capabilities.copy } : {}),
      ...(typeof capabilities?.download === 'boolean' ? { download: capabilities.download } : {}),
      ...(typeof capabilities?.reveal === 'boolean' ? { reveal: capabilities.reveal } : {}),
    },
  }
}

export function normalizeWorkspaceFileReferenceV1(value: unknown): WorkspaceFileReferenceV1 | null {
  const reference = normalizeReferenceV1(value)
  return reference?.kind === 'workspace_file' && reference.locator?.relativePath
    ? reference as WorkspaceFileReferenceV1
    : null
}

function routeBasePath(basePath?: string): string {
  const source = basePath
    || (typeof document !== 'undefined'
      ? document.getElementById('opensquilla-data')?.dataset.basePath
      : undefined)
    || '/control'
  const trimmed = source.trim()
  if (!trimmed || trimmed === '/') return ''
  return `/${trimmed.replace(/^\/+|\/+$/g, '')}`
}

function sessionRouteUrl(origin: string, sessionKey: string, basePath?: string): string {
  const url = new URL(`${routeBasePath(basePath)}/chat`, origin)
  url.searchParams.set('session', canonicalSessionKey(sessionKey))
  return url.toString()
}

/** URL opened by this WebUI instance; uses the Vue Router base path. */
export function sessionApplicationUrl(sessionKey: string, basePath?: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
  return sessionRouteUrl(origin, sessionKey, basePath)
}

// The browser's application link honors its current router base path.
// Desktop callers use sessionDesktopLink instead.
export function sessionApplicationLink(sessionKey: string): string {
  return sessionApplicationUrl(sessionKey)
}

/** Strict Electron deep link; the desktop shell validates the action and key. */
export function sessionDesktopLink(sessionKey: string): string {
  return `opensquilla://open/session/${encodeURIComponent(canonicalSessionKey(sessionKey))}`
}

/** URL for a Gateway-hosted Control UI, deliberately dropping websocket query tokens. */
export function sessionGatewayUrl(
  sessionKey: string,
  gatewayEndpoint: string,
  basePath?: string,
): string {
  let endpoint: URL
  try {
    endpoint = new URL(gatewayEndpoint)
  } catch {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
    return sessionRouteUrl(origin, sessionKey, basePath)
  }
  if (!['http:', 'https:', 'ws:', 'wss:'].includes(endpoint.protocol) || endpoint.origin === 'null') {
    const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
    return sessionRouteUrl(origin, sessionKey, basePath)
  }
  endpoint.protocol = ['wss:', 'https:'].includes(endpoint.protocol) ? 'https:' : 'http:'
  endpoint.pathname = '/'
  endpoint.search = ''
  endpoint.hash = ''
  return sessionRouteUrl(endpoint.origin, sessionKey, basePath)
}

export function sessionGatewayLink(sessionKey: string, basePath?: string): string {
  let endpoint = ''
  try { endpoint = localStorage.getItem('opensquilla.wsUrl') || '' } catch { /* private mode */ }
  if (!endpoint) {
    const protocol = typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    endpoint = `${protocol}//${typeof window !== 'undefined' ? window.location.host : 'localhost'}/ws`
  }
  return sessionGatewayUrl(sessionKey, endpoint, basePath)
}
