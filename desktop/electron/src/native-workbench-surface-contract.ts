import {
  DESKTOP_ARTIFACT_BRIDGE_CONTRACT,
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V3,
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V4,
} from './desktop-artifact-bridge-contract.js'

export const NATIVE_WORKBENCH_PROTOCOL_VERSION = 1 as const
export const NATIVE_WORKBENCH_PROTOCOL_VERSION_V2 = 2 as const
export const NATIVE_WORKBENCH_PROTOCOL_VERSION_V3 =
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V3
export const NATIVE_WORKBENCH_PROTOCOL_VERSION_V4 =
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION_V4
export const NATIVE_WORKBENCH_MAX_SURFACES = 8
export const NATIVE_WORKBENCH_MAX_HTML_BYTES = 5 * 1024 * 1024
export const NATIVE_WORKBENCH_ARTIFACT_SCHEME = 'opensquilla-artifact'

const SURFACE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const PREVIEW_HOST_PATTERN = /^p-[a-f0-9]{32}\.localhost$/

export interface NativeWorkbenchCreateRequestV1 {
  version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION
  surfaceId: string
  kind: 'artifact-html'
  payload: {
    data: Uint8Array
    name: string
    mime: string
    scopeId: string
    allowRemoteResources: boolean
  }
}

export type NativeWorkbenchPreviewMode = 'full' | 'offline'

export interface NativeWorkbenchArtifactPreviewCreateRequestV2 {
  version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
  surfaceId: string
  kind: 'artifact-preview'
  payload: {
    launchUrl: string
    expectedOrigin: string
    scopeId: string
    mode: NativeWorkbenchPreviewMode
  }
}

export interface NativeWorkbenchUrlPreviewCreateRequestV2 {
  version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
  surfaceId: string
  kind: 'url-preview'
  payload: {
    url: string
    scopeId: string
  }
}

export type NativeWorkbenchCreateRequestV2 =
  | NativeWorkbenchArtifactPreviewCreateRequestV2
  | NativeWorkbenchUrlPreviewCreateRequestV2

export type NativeWorkbenchArtifactPreviewCreateRequestV3 =
  Omit<NativeWorkbenchArtifactPreviewCreateRequestV2, 'version'> & {
    version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
  }

export type NativeWorkbenchUrlPreviewCreateRequestV3 =
  Omit<NativeWorkbenchUrlPreviewCreateRequestV2, 'version'> & {
    version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
  }

export type NativeWorkbenchCreateRequestV3 =
  | NativeWorkbenchArtifactPreviewCreateRequestV3
  | NativeWorkbenchUrlPreviewCreateRequestV3

export type NativeWorkbenchArtifactPreviewCreateRequestV4 =
  Omit<NativeWorkbenchArtifactPreviewCreateRequestV3, 'version'> & {
    version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
  }

export type NativeWorkbenchUrlPreviewCreateRequestV4 =
  Omit<NativeWorkbenchUrlPreviewCreateRequestV3, 'version'> & {
    version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
  }

export type NativeWorkbenchCreateRequestV4 =
  | NativeWorkbenchArtifactPreviewCreateRequestV4
  | NativeWorkbenchUrlPreviewCreateRequestV4

export type NativeWorkbenchCreateRequest =
  | NativeWorkbenchCreateRequestV1
  | NativeWorkbenchCreateRequestV2
  | NativeWorkbenchCreateRequestV3
  | NativeWorkbenchCreateRequestV4

export type NativeWorkbenchInteractiveProtocolVersion =
  | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
  | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
  | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4

export const NATIVE_WORKBENCH_NAVIGATION_ACTIONS = [
  'navigate',
  'back',
  'forward',
  'reload',
  'stop',
  'open-external',
] as const

export type NativeWorkbenchNavigationAction =
  typeof NATIVE_WORKBENCH_NAVIGATION_ACTIONS[number]

export interface NativeWorkbenchNavigationRequest {
  version: NativeWorkbenchInteractiveProtocolVersion
  surfaceId: string
  action: NativeWorkbenchNavigationAction
  url?: string
}

export interface NativeWorkbenchPermissionResponse {
  version: NativeWorkbenchInteractiveProtocolVersion
  surfaceId: string
  requestId: string
  allow: boolean
}

export interface NativeWorkbenchCapabilities {
  latestVersion: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
  protocolVersions: readonly [
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION,
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V2,
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3,
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  ]
  versions: readonly [
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION,
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V2,
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3,
    typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  ]
  kinds: readonly ['artifact-html', 'artifact-preview', 'url-preview']
  modes: readonly ['full', 'offline']
  navigationActions: typeof NATIVE_WORKBENCH_NAVIGATION_ACTIONS
  permissionResponses: true
  artifactBridge: typeof DESKTOP_ARTIFACT_BRIDGE_CONTRACT
  maxSurfaces: typeof NATIVE_WORKBENCH_MAX_SURFACES
}

export const NATIVE_WORKBENCH_CAPABILITIES: NativeWorkbenchCapabilities = {
  latestVersion: NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  protocolVersions: [
    NATIVE_WORKBENCH_PROTOCOL_VERSION,
    NATIVE_WORKBENCH_PROTOCOL_VERSION_V2,
    NATIVE_WORKBENCH_PROTOCOL_VERSION_V3,
    NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  ],
  versions: [
    NATIVE_WORKBENCH_PROTOCOL_VERSION,
    NATIVE_WORKBENCH_PROTOCOL_VERSION_V2,
    NATIVE_WORKBENCH_PROTOCOL_VERSION_V3,
    NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  ],
  kinds: ['artifact-html', 'artifact-preview', 'url-preview'],
  modes: ['full', 'offline'],
  navigationActions: NATIVE_WORKBENCH_NAVIGATION_ACTIONS,
  permissionResponses: true,
  artifactBridge: DESKTOP_ARTIFACT_BRIDGE_CONTRACT,
  maxSurfaces: NATIVE_WORKBENCH_MAX_SURFACES,
}

export interface NativeWorkbenchSurfaceRectRequest {
  surfaceId: string
  x: number
  y: number
  width: number
  height: number
  visible: boolean
}

export interface NativeWorkbenchSurfaceRect {
  x: number
  y: number
  width: number
  height: number
}

export type NativeWorkbenchSurfaceEventType =
  | 'loading'
  | 'ready'
  | 'missing-resource'
  | 'error'
  | 'crashed'
  | 'escape'
  | 'navigation-state'
  | 'permission-request'
  | 'blocked-action'
  | 'capability-expired'
  | 'unresponsive'
  | 'annotation-selected'
  | 'annotation-draft-change'
  | 'annotation-submit'
  | 'annotation-cancel'
  | 'annotation-overlay-fallback'
  | 'agent-edit-released'

export interface NativeWorkbenchSurfaceEvent {
  version:
    | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION
    | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
    | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
    | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
  surfaceId: string
  type: NativeWorkbenchSurfaceEventType
  detail?: {
    message?: string
    path?: string
    reason?: string
    url?: string
    title?: string
    loading?: boolean
    canGoBack?: boolean
    canGoForward?: boolean
    requestId?: string
    permission?: string
    requestingOrigin?: string
    mediaTypes?: string[]
    action?: string
    targetUrl?: string
    annotationId?: string
    body?: string
    selection?: {
      selectionId: string
      tagName: string
      elementPath: string
      domSha256?: string
      elementProofSha256: string
      rect: NativeWorkbenchSurfaceRect
    }
  }
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

export function parseNativeWorkbenchSurfaceId(value: unknown): string {
  if (typeof value !== 'string' || !SURFACE_ID_PATTERN.test(value)) {
    throw new Error('Choose a valid native Workbench surface.')
  }
  return value
}

function parseArtifactBytes(value: unknown): Uint8Array {
  let bytes: Uint8Array | null = null
  if (value instanceof ArrayBuffer) {
    bytes = new Uint8Array(value)
  } else if (ArrayBuffer.isView(value)) {
    bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  }
  if (!bytes || bytes.byteLength === 0) {
    throw new Error('The HTML artifact is empty.')
  }
  if (bytes.byteLength > NATIVE_WORKBENCH_MAX_HTML_BYTES) {
    throw new Error('The HTML artifact exceeds the 5 MiB preview limit.')
  }
  return bytes
}

function parseArtifactName(value: unknown): string {
  if (typeof value !== 'string') throw new Error('The HTML artifact name is invalid.')
  const name = value.trim().split(/[/\\]/).pop()?.trim() || ''
  if (!name || name.length > 255 || /[\u0000-\u001f]/.test(name)) {
    throw new Error('The HTML artifact name is invalid.')
  }
  return name
}

function parseScopeId(value: unknown): string {
  if (typeof value !== 'string') throw new Error('The Workbench scope is invalid.')
  const scopeId = value.trim()
  if (!scopeId || scopeId.length > 512 || /[\u0000-\u001f]/.test(scopeId)) {
    throw new Error('The Workbench scope is invalid.')
  }
  return scopeId
}

function parseHttpUrl(value: unknown, label: string): URL {
  if (typeof value !== 'string' || value.length === 0 || value.length > 8192) {
    throw new Error(`The ${label} is invalid.`)
  }
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    throw new Error(`The ${label} is invalid.`)
  }
  if (
    (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
    || parsed.username
    || parsed.password
  ) {
    throw new Error(`The ${label} must use HTTP or HTTPS without embedded credentials.`)
  }
  return parsed
}

export function parseNativeWorkbenchNavigationUrl(value: unknown): string {
  return parseHttpUrl(value, 'Workbench address').href
}

function parseArtifactPreviewPayload(
  payload: Record<string, unknown>,
): NativeWorkbenchArtifactPreviewCreateRequestV2['payload'] {
  const launchUrl = parseHttpUrl(payload.launchUrl, 'artifact preview address')
  if (
    launchUrl.protocol !== 'http:'
    || !PREVIEW_HOST_PATTERN.test(launchUrl.hostname)
    || !launchUrl.port
    || launchUrl.search
    || launchUrl.hash
  ) {
    throw new Error('The artifact preview address is not a trusted loopback preview.')
  }
  const expectedOrigin = typeof payload.expectedOrigin === 'string'
    ? payload.expectedOrigin
    : ''
  if (expectedOrigin !== launchUrl.origin) {
    throw new Error('The artifact preview origin does not match its launch address.')
  }
  const mode = payload.mode
  if (mode !== 'full' && mode !== 'offline') {
    throw new Error('Choose a supported Workbench preview mode.')
  }
  return {
    launchUrl: launchUrl.href,
    expectedOrigin,
    scopeId: parseScopeId(payload.scopeId),
    mode,
  }
}

export function parseNativeWorkbenchCreateRequest(
  value: unknown,
): NativeWorkbenchCreateRequest {
  const request = objectRecord(value)
  const payload = objectRecord(request?.payload)
  if (!request || !payload) {
    throw new Error('Unsupported native Workbench request.')
  }
  if (
    request.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
    || request.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
    || request.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
  ) {
    const version = request.version
    const surfaceId = parseNativeWorkbenchSurfaceId(request.surfaceId)
    if (request.kind === 'artifact-preview') {
      return {
        version,
        surfaceId,
        kind: 'artifact-preview',
        payload: parseArtifactPreviewPayload(payload),
      }
    }
    if (request.kind === 'url-preview') {
      return {
        version,
        surfaceId,
        kind: 'url-preview',
        payload: {
          url: parseNativeWorkbenchNavigationUrl(payload.url),
          scopeId: parseScopeId(payload.scopeId),
        },
      }
    }
    throw new Error('Unsupported native Workbench request.')
  }
  if (
    request.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION
    || request.kind !== 'artifact-html'
  ) throw new Error('Unsupported native Workbench request.')
  const mime = typeof payload.mime === 'string'
    ? payload.mime.split(';', 1)[0].trim().toLowerCase()
    : ''
  if (mime !== 'text/html') throw new Error('Only HTML artifacts can use this native surface.')
  return {
    version: NATIVE_WORKBENCH_PROTOCOL_VERSION,
    surfaceId: parseNativeWorkbenchSurfaceId(request.surfaceId),
    kind: 'artifact-html',
    payload: {
      data: parseArtifactBytes(payload.data),
      name: parseArtifactName(payload.name),
      mime,
      scopeId: parseScopeId(payload.scopeId),
      allowRemoteResources: payload.allowRemoteResources === true,
    },
  }
}

export function parseNativeWorkbenchNavigationRequest(
  value: unknown,
): NativeWorkbenchNavigationRequest {
  const request = objectRecord(value)
  if (
    (
      request?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
      && request?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
      && request?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
    )
    || !NATIVE_WORKBENCH_NAVIGATION_ACTIONS.includes(
      request.action as NativeWorkbenchNavigationAction,
    )
  ) {
    throw new Error('Unsupported native Workbench navigation request.')
  }
  const action = request.action as NativeWorkbenchNavigationAction
  const url = action === 'navigate' || action === 'open-external'
    ? parseNativeWorkbenchNavigationUrl(request.url)
    : undefined
  if (action !== 'navigate' && action !== 'open-external' && request.url !== undefined) {
    throw new Error('This native Workbench navigation action does not accept an address.')
  }
  return {
    version: request.version,
    surfaceId: parseNativeWorkbenchSurfaceId(request.surfaceId),
    action,
    ...(url ? { url } : {}),
  }
}

export function parseNativeWorkbenchPermissionResponse(
  value: unknown,
): NativeWorkbenchPermissionResponse {
  const response = objectRecord(value)
  if (
    (
      response?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
      && response?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
      && response?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
    )
    || typeof response.allow !== 'boolean'
    || typeof response.requestId !== 'string'
    || !/^[a-f0-9-]{36}$/.test(response.requestId)
  ) {
    throw new Error('The native Workbench permission response is invalid.')
  }
  return {
    version: response.version,
    surfaceId: parseNativeWorkbenchSurfaceId(response.surfaceId),
    requestId: response.requestId,
    allow: response.allow,
  }
}

function finiteNumber(value: unknown, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new Error(`The native Workbench ${label} is invalid.`)
  }
  return value
}

export function parseNativeWorkbenchSurfaceRectRequest(
  value: unknown,
): NativeWorkbenchSurfaceRectRequest {
  const request = objectRecord(value)
  if (!request || typeof request.visible !== 'boolean') {
    throw new Error('The native Workbench bounds are invalid.')
  }
  return {
    surfaceId: parseNativeWorkbenchSurfaceId(request.surfaceId),
    x: finiteNumber(request.x, 'x coordinate'),
    y: finiteNumber(request.y, 'y coordinate'),
    width: finiteNumber(request.width, 'width'),
    height: finiteNumber(request.height, 'height'),
    visible: request.visible,
  }
}

export function clampNativeWorkbenchSurfaceRect(
  request: Pick<NativeWorkbenchSurfaceRectRequest, 'x' | 'y' | 'width' | 'height'>,
  contentBounds: { width: number; height: number },
): NativeWorkbenchSurfaceRect | null {
  const contentWidth = Math.max(0, Math.floor(contentBounds.width))
  const contentHeight = Math.max(0, Math.floor(contentBounds.height))
  const x = Math.min(contentWidth, Math.max(0, Math.floor(request.x)))
  const y = Math.min(contentHeight, Math.max(0, Math.floor(request.y)))
  const requestedWidth = Math.max(0, Math.ceil(request.width))
  const requestedHeight = Math.max(0, Math.ceil(request.height))
  const width = Math.min(requestedWidth, contentWidth - x)
  const height = Math.min(requestedHeight, contentHeight - y)
  return width > 0 && height > 0 ? { x, y, width, height } : null
}

export function nativeWorkbenchCssRectToDip(
  request: Pick<NativeWorkbenchSurfaceRectRequest, 'x' | 'y' | 'width' | 'height'>,
  zoomFactor: number,
): NativeWorkbenchSurfaceRect {
  // Electron's View bounds use device-independent pixels. Browser DOM geometry
  // uses CSS pixels, which differ only by the Control UI zoom factor; the OS
  // devicePixelRatio must not be applied here.
  const factor = Number.isFinite(zoomFactor) && zoomFactor > 0 ? zoomFactor : 1
  return {
    x: request.x * factor,
    y: request.y * factor,
    width: request.width * factor,
    height: request.height * factor,
  }
}

export function nativeWorkbenchArtifactUrl(handle: string): string {
  return `${NATIVE_WORKBENCH_ARTIFACT_SCHEME}://${handle}/index.html`
}

export function nativeWorkbenchNetworkUrlAllowed(
  value: string,
  allowRemoteResources = false,
  resourceType = '',
): boolean {
  try {
    const protocol = new URL(value).protocol
    return protocol === `${NATIVE_WORKBENCH_ARTIFACT_SCHEME}:`
      || protocol === 'data:'
      || protocol === 'blob:'
      || (
        allowRemoteResources
        && protocol === 'https:'
        && ['font', 'image', 'media', 'stylesheet'].includes(resourceType)
      )
  } catch {
    return false
  }
}

export function nativeWorkbenchArtifactRequestIsDocument(
  value: string,
  method: string,
  handle: string,
): boolean {
  try {
    const target = new URL(value)
    return (
      method === 'GET'
      && target.protocol === `${NATIVE_WORKBENCH_ARTIFACT_SCHEME}:`
      && target.hostname === handle
      && target.pathname === '/index.html'
      && target.search === ''
    )
  } catch {
    return false
  }
}

export function nativeWorkbenchV2NetworkUrlAllowed(
  value: string,
  mode: NativeWorkbenchPreviewMode,
  expectedOrigin?: string,
): boolean {
  try {
    const target = new URL(value)
    if (
      target.protocol === 'data:'
      || target.protocol === 'blob:'
      || target.href === 'about:blank'
    ) {
      return true
    }
    if (mode === 'offline') {
      const networkOrigin = target.protocol === 'ws:'
        ? `http://${target.host}`
        : target.protocol === 'wss:'
          ? `https://${target.host}`
          : target.origin
      return Boolean(
        expectedOrigin
        && (
          target.protocol === 'http:'
          || target.protocol === 'https:'
          || target.protocol === 'ws:'
          || target.protocol === 'wss:'
        )
        && networkOrigin === expectedOrigin,
      )
    }
    return target.protocol === 'http:'
      || target.protocol === 'https:'
      || target.protocol === 'ws:'
      || target.protocol === 'wss:'
  } catch {
    return false
  }
}

export function nativeWorkbenchMissingResourceIsLocal(
  value: string,
  expectedOrigin?: string,
): boolean {
  if (!expectedOrigin) return false
  try {
    const target = new URL(value)
    return (
      (target.protocol === 'http:' || target.protocol === 'https:')
      && target.origin === expectedOrigin
    )
  } catch {
    return false
  }
}

export function nativeWorkbenchDownloadAllowed(
  hasUserGesture: unknown,
  candidatePreviewActive = false,
): boolean {
  // Canonical previews may still offer a user-confirmed native save dialog.
  // Candidate bytes are an uncommitted, turn-local inspection surface and
  // must never escape through a download, even when the page synthesizes a
  // trusted user gesture.
  return candidatePreviewActive !== true && hasUserGesture === true
}
