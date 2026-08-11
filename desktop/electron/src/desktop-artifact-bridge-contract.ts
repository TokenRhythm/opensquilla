export const DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION = 3 as const

export const DESKTOP_ARTIFACT_BRIDGE_METHODS = [
  'captureSelection',
  'resolveAnnotationSelection',
  'focusAnnotation',
  'browserInspect',
  'browserAct',
  'screenshot',
  'officeFlush',
  'reloadSurface',
] as const

export type DesktopArtifactBridgeMethod =
  typeof DESKTOP_ARTIFACT_BRIDGE_METHODS[number]

export interface DesktopArtifactBridgeCapabilities {
  version: typeof DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION
  available: boolean
  captureSelection: boolean
  resolveAnnotationSelection: boolean
  focusAnnotation: boolean
  browserInspect: boolean
  browserAct: boolean
  screenshot: boolean
  officeFlush: boolean
  reloadSurface: boolean
}

/**
 * Protocol metadata advertised by Desktop independently of the currently
 * active surface. Actual method availability is queried at call time and is
 * false unless a protocol-v3 active surface has an implementation.
 */
export const DESKTOP_ARTIFACT_BRIDGE_CONTRACT = {
  version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
  surfaceBinding: 'active' as const,
  methods: DESKTOP_ARTIFACT_BRIDGE_METHODS,
  acceptsSurfaceId: false as const,
  acceptsUrl: false as const,
  acceptsJavascriptExpression: false as const,
  acceptsRawCdp: false as const,
}

export const DESKTOP_ARTIFACT_BRIDGE_UNSUPPORTED_CAPABILITIES:
DesktopArtifactBridgeCapabilities = Object.freeze({
  version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
  available: false,
  captureSelection: false,
  resolveAnnotationSelection: false,
  focusAnnotation: false,
  browserInspect: false,
  browserAct: false,
  screenshot: false,
  officeFlush: false,
  reloadSurface: false,
})

interface DesktopArtifactBridgeRequestBase {
  version: typeof DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION
}

export type DesktopArtifactCaptureSelectionRequest = DesktopArtifactBridgeRequestBase

export interface DesktopArtifactResolveAnnotationSelectionRequest
  extends DesktopArtifactBridgeRequestBase {
  activePreviewArtifactId: string
  selectionId: string
  tagName: string
  elementPath: string
  domSha256?: string
  elementProofSha256: string
}

export interface DesktopArtifactFocusAnnotationRequest
  extends DesktopArtifactBridgeRequestBase {
  activePreviewArtifactId: string
  annotationId: string
  scopeId: string
  tagName: string
  elementPath: string
  elementProofSha256: string
}

export type DesktopArtifactBrowserInspectScope =
  | 'document'
  | 'selection'
  | 'viewport'

export interface DesktopArtifactBrowserInspectRequest
  extends DesktopArtifactBridgeRequestBase {
  scope: DesktopArtifactBrowserInspectScope
  maxNodes: number
}

export const DESKTOP_ARTIFACT_BROWSER_ACTIONS = [
  'click',
  'focus',
  'type',
  'press',
  'scroll',
] as const

export type DesktopArtifactBrowserAction =
  typeof DESKTOP_ARTIFACT_BROWSER_ACTIONS[number]

export const DESKTOP_ARTIFACT_BROWSER_KEYS = [
  'Enter',
  'Tab',
  'Escape',
  'Backspace',
  'Delete',
  'Space',
  'ArrowUp',
  'ArrowDown',
  'ArrowLeft',
  'ArrowRight',
  'Home',
  'End',
  'PageUp',
  'PageDown',
] as const

export type DesktopArtifactBrowserKey =
  typeof DESKTOP_ARTIFACT_BROWSER_KEYS[number]

export interface DesktopArtifactBrowserClickRequest
  extends DesktopArtifactBridgeRequestBase {
  action: 'click' | 'focus'
  anchor: string
}

export interface DesktopArtifactBrowserTypeRequest
  extends DesktopArtifactBridgeRequestBase {
  action: 'type'
  anchor: string
  text: string
  replace: boolean
}

export interface DesktopArtifactBrowserPressRequest
  extends DesktopArtifactBridgeRequestBase {
  action: 'press'
  key: DesktopArtifactBrowserKey
}

export interface DesktopArtifactBrowserScrollRequest
  extends DesktopArtifactBridgeRequestBase {
  action: 'scroll'
  direction: 'up' | 'down' | 'left' | 'right'
  amount: 'line' | 'page'
}

export type DesktopArtifactBrowserActRequest =
  | DesktopArtifactBrowserClickRequest
  | DesktopArtifactBrowserTypeRequest
  | DesktopArtifactBrowserPressRequest
  | DesktopArtifactBrowserScrollRequest

export type DesktopArtifactScreenshotRequest = DesktopArtifactBridgeRequestBase
export type DesktopArtifactOfficeFlushRequest = DesktopArtifactBridgeRequestBase
export type DesktopArtifactReloadSurfaceRequest = DesktopArtifactBridgeRequestBase

export type DesktopArtifactSelectionKind =
  | 'none'
  | 'text'
  | 'cell'
  | 'range'
  | 'shape'
  | 'slide'
  | 'dom'

export interface DesktopArtifactSelectionSnapshot {
  kind: DesktopArtifactSelectionKind
  anchor?: string
  text?: string
  truncated?: boolean
}

export interface DesktopArtifactResolvedAnnotationSelection {
  activePreviewArtifactId: string
  selectionId: string
  tagName: string
  elementPath: string
  domSha256?: string
  elementProofSha256: string
  scopeId: string
  rect: {
    x: number
    y: number
    width: number
    height: number
  }
}

export interface DesktopArtifactBrowserNode {
  anchor: string
  role?: string
  name?: string
  text?: string
  interactive?: boolean
  disabled?: boolean
  selected?: boolean
}

export interface DesktopArtifactBrowserSnapshot {
  scope: DesktopArtifactBrowserInspectScope
  nodes: DesktopArtifactBrowserNode[]
  truncated: boolean
}

export interface DesktopArtifactBrowserActResult {
  performed: boolean
  changed: boolean
}

export interface DesktopArtifactScreenshotResult {
  mime: 'image/png'
  data: Uint8Array
  width: number
  height: number
}

export interface DesktopArtifactOfficeFlushResult {
  flushed: boolean
  revision?: string
}

export interface DesktopArtifactReloadSurfaceResult {
  reloaded: true
}

export interface DesktopArtifactFocusAnnotationResult {
  focused: true
  activePreviewArtifactId: string
}

export interface DesktopArtifactBridgeRequestByMethod {
  captureSelection: DesktopArtifactCaptureSelectionRequest
  resolveAnnotationSelection: DesktopArtifactResolveAnnotationSelectionRequest
  focusAnnotation: DesktopArtifactFocusAnnotationRequest
  browserInspect: DesktopArtifactBrowserInspectRequest
  browserAct: DesktopArtifactBrowserActRequest
  screenshot: DesktopArtifactScreenshotRequest
  officeFlush: DesktopArtifactOfficeFlushRequest
  reloadSurface: DesktopArtifactReloadSurfaceRequest
}

export interface DesktopArtifactBridgeValueByMethod {
  captureSelection: DesktopArtifactSelectionSnapshot
  resolveAnnotationSelection: DesktopArtifactResolvedAnnotationSelection
  focusAnnotation: DesktopArtifactFocusAnnotationResult
  browserInspect: DesktopArtifactBrowserSnapshot
  browserAct: DesktopArtifactBrowserActResult
  screenshot: DesktopArtifactScreenshotResult
  officeFlush: DesktopArtifactOfficeFlushResult
  reloadSurface: DesktopArtifactReloadSurfaceResult
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function parseRequest(
  value: unknown,
  allowedKeys: readonly string[],
  label: string,
): Record<string, unknown> {
  const request = objectRecord(value)
  if (
    !request
    || request.version !== DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION
    || Object.keys(request).some(key => !allowedKeys.includes(key))
  ) {
    throw new Error(`The Desktop artifact ${label} request is invalid.`)
  }
  return request
}

function parseVersionOnlyRequest(
  value: unknown,
  label: string,
): DesktopArtifactBridgeRequestBase {
  parseRequest(value, ['version'], label)
  return { version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION }
}

function parseAnchor(value: unknown): string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value)) {
    throw new Error('The Desktop artifact browser anchor is invalid.')
  }
  return value
}

function parseAnnotationElementPath(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0 || value.length > 4096) {
    throw new Error('The Desktop artifact annotation element path is invalid.')
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    throw new Error('The Desktop artifact annotation element path is invalid.')
  }
  if (
    !Array.isArray(parsed)
    || parsed.length < 1
    || parsed.length > 128
    || parsed.some(segment => (
      !Array.isArray(segment)
      || segment.length !== 3
      || typeof segment[0] !== 'string'
      || segment[0].length > 256
      || /[\u0000-\u001f\u007f]/.test(segment[0])
      || typeof segment[1] !== 'string'
      || !/^[a-z][a-z0-9._:-]{0,63}$/.test(segment[1])
      || !Number.isSafeInteger(segment[2])
      || segment[2] < 1
    ))
    || JSON.stringify(parsed) !== value
  ) {
    throw new Error('The Desktop artifact annotation element path is invalid.')
  }
  return value
}

function parseActivePreviewArtifactId(value: unknown): string {
  if (typeof value !== 'string' || !/^art-[A-Za-z0-9_-]{1,200}$/.test(value)) {
    throw new Error('The Desktop artifact preview identity is invalid.')
  }
  return value
}

export function parseDesktopArtifactCaptureSelectionRequest(
  value: unknown,
): DesktopArtifactCaptureSelectionRequest {
  return parseVersionOnlyRequest(value, 'selection capture')
}

export function parseDesktopArtifactResolveAnnotationSelectionRequest(
  value: unknown,
): DesktopArtifactResolveAnnotationSelectionRequest {
  const request = parseRequest(
    value,
    [
      'version',
      'activePreviewArtifactId',
      'selectionId',
      'tagName',
      'elementPath',
      'domSha256',
      'elementProofSha256',
    ],
    'annotation selection resolution',
  )
  if (
    typeof request.selectionId !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(request.selectionId)
    || typeof request.tagName !== 'string'
    || !/^[a-z][a-z0-9._:-]{0,63}$/.test(request.tagName)
    || (request.domSha256 !== undefined && (
      typeof request.domSha256 !== 'string'
      || !/^[a-f0-9]{64}$/.test(request.domSha256)
    ))
    || typeof request.elementProofSha256 !== 'string'
    || !/^[a-f0-9]{64}$/.test(request.elementProofSha256)
  ) {
    throw new Error('The Desktop artifact annotation selection is invalid.')
  }
  return {
    version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
    activePreviewArtifactId: parseActivePreviewArtifactId(
      request.activePreviewArtifactId,
    ),
    selectionId: request.selectionId,
    tagName: request.tagName,
    elementPath: parseAnnotationElementPath(request.elementPath),
    ...(request.domSha256 === undefined ? {} : { domSha256: request.domSha256 }),
    elementProofSha256: request.elementProofSha256,
  }
}

export function parseDesktopArtifactFocusAnnotationRequest(
  value: unknown,
): DesktopArtifactFocusAnnotationRequest {
  const request = parseRequest(
    value,
    [
      'version',
      'activePreviewArtifactId',
      'annotationId',
      'scopeId',
      'tagName',
      'elementPath',
      'elementProofSha256',
    ],
    'annotation focus',
  )
  if (
    typeof request.annotationId !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(request.annotationId)
    || typeof request.scopeId !== 'string'
    || request.scopeId.length === 0
    || request.scopeId.length > 512
    || /[\u0000-\u001f\u007f]/.test(request.scopeId)
    || typeof request.tagName !== 'string'
    || !/^[a-z][a-z0-9._:-]{0,63}$/.test(request.tagName)
    || typeof request.elementProofSha256 !== 'string'
    || !/^[a-f0-9]{64}$/.test(request.elementProofSha256)
  ) {
    throw new Error('The Desktop artifact annotation focus request is invalid.')
  }
  return {
    version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
    activePreviewArtifactId: parseActivePreviewArtifactId(
      request.activePreviewArtifactId,
    ),
    annotationId: request.annotationId,
    scopeId: request.scopeId,
    tagName: request.tagName,
    elementPath: parseAnnotationElementPath(request.elementPath),
    elementProofSha256: request.elementProofSha256,
  }
}

export function parseDesktopArtifactBrowserInspectRequest(
  value: unknown,
): DesktopArtifactBrowserInspectRequest {
  const request = parseRequest(
    value,
    ['version', 'scope', 'maxNodes'],
    'browser inspection',
  )
  const scope = request.scope
  if (scope !== 'document' && scope !== 'selection' && scope !== 'viewport') {
    throw new Error('Choose a supported Desktop artifact inspection scope.')
  }
  const maxNodes = request.maxNodes
  if (!Number.isInteger(maxNodes) || (maxNodes as number) < 1 || (maxNodes as number) > 200) {
    throw new Error('The Desktop artifact browser inspection limit is invalid.')
  }
  return {
    version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
    scope,
    maxNodes: maxNodes as number,
  }
}

export function parseDesktopArtifactBrowserActRequest(
  value: unknown,
): DesktopArtifactBrowserActRequest {
  const base = objectRecord(value)
  const action = base?.action
  if (!DESKTOP_ARTIFACT_BROWSER_ACTIONS.includes(action as DesktopArtifactBrowserAction)) {
    throw new Error('Choose a supported Desktop artifact browser action.')
  }
  if (action === 'click' || action === 'focus') {
    const request = parseRequest(value, ['version', 'action', 'anchor'], 'browser action')
    return {
      version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
      action,
      anchor: parseAnchor(request.anchor),
    }
  }
  if (action === 'type') {
    const request = parseRequest(
      value,
      ['version', 'action', 'anchor', 'text', 'replace'],
      'browser action',
    )
    if (
      typeof request.text !== 'string'
      || request.text.length > 16_384
      || /\u0000/.test(request.text)
      || typeof request.replace !== 'boolean'
    ) {
      throw new Error('The Desktop artifact browser text input is invalid.')
    }
    return {
      version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
      action,
      anchor: parseAnchor(request.anchor),
      text: request.text,
      replace: request.replace,
    }
  }
  if (action === 'press') {
    const request = parseRequest(value, ['version', 'action', 'key'], 'browser action')
    if (!DESKTOP_ARTIFACT_BROWSER_KEYS.includes(request.key as DesktopArtifactBrowserKey)) {
      throw new Error('Choose a supported Desktop artifact browser key.')
    }
    return {
      version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
      action,
      key: request.key as DesktopArtifactBrowserKey,
    }
  }
  const request = parseRequest(
    value,
    ['version', 'action', 'direction', 'amount'],
    'browser action',
  )
  const direction = request.direction
  const amount = request.amount
  if (
    (direction !== 'up' && direction !== 'down'
      && direction !== 'left' && direction !== 'right')
    || (amount !== 'line' && amount !== 'page')
  ) {
    throw new Error('Choose a supported Desktop artifact scroll action.')
  }
  return {
    version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
    action: 'scroll',
    direction,
    amount,
  }
}

export function parseDesktopArtifactScreenshotRequest(
  value: unknown,
): DesktopArtifactScreenshotRequest {
  return parseVersionOnlyRequest(value, 'screenshot')
}

export function parseDesktopArtifactOfficeFlushRequest(
  value: unknown,
): DesktopArtifactOfficeFlushRequest {
  return parseVersionOnlyRequest(value, 'Office flush')
}

export function parseDesktopArtifactReloadSurfaceRequest(
  value: unknown,
): DesktopArtifactReloadSurfaceRequest {
  return parseVersionOnlyRequest(value, 'surface reload')
}
