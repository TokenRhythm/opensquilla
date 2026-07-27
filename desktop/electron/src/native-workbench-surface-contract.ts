export const NATIVE_WORKBENCH_PROTOCOL_VERSION = 1 as const
export const NATIVE_WORKBENCH_MAX_HTML_BYTES = 5 * 1024 * 1024
export const NATIVE_WORKBENCH_ARTIFACT_SCHEME = 'opensquilla-artifact'

const SURFACE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/

export interface NativeWorkbenchCreateRequest {
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

export interface NativeWorkbenchSurfaceEvent {
  version: typeof NATIVE_WORKBENCH_PROTOCOL_VERSION
  surfaceId: string
  type: NativeWorkbenchSurfaceEventType
  detail?: {
    message?: string
    path?: string
    reason?: string
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

export function parseNativeWorkbenchCreateRequest(
  value: unknown,
): NativeWorkbenchCreateRequest {
  const request = objectRecord(value)
  const payload = objectRecord(request?.payload)
  if (
    request?.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION
    || request.kind !== 'artifact-html'
    || !payload
  ) {
    throw new Error('Unsupported native Workbench request.')
  }
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
