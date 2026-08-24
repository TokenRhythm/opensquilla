import {
  NATIVE_WORKBENCH_PROTOCOL_VERSION_V3,
  NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  parseNativeWorkbenchSurfaceId,
} from './native-workbench-surface-contract.js'

export type NativeWorkbenchAnnotationProtocolVersion =
  | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
  | typeof NATIVE_WORKBENCH_PROTOCOL_VERSION_V4

export const NATIVE_WORKBENCH_ANNOTATION_BODY_MAX_BYTES = 16 * 1024
export const NATIVE_WORKBENCH_ANNOTATION_ELEMENT_PATH_MAX_LENGTH = 4096
export const NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH = 304
export const NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HEIGHT = 160

const OPAQUE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/
const SHA256_PATTERN = /^[a-f0-9]{64}$/

export interface NativeWorkbenchAnnotationCapabilities {
  version: NativeWorkbenchAnnotationProtocolVersion
  available: boolean
  picker: boolean
  trustedOverlay: boolean
  overlayCopyVersion?: 1
  reason?: string
}

export interface NativeWorkbenchAnnotationOverlayCopy {
  targetLabel: string
  contextLabel: string
  bodyLabel: string
  placeholder: string
  newlineHint: string
  cancelLabel: string
  submitLabel: string
  emptyBodyMessage: string
}

export interface NativeWorkbenchAnnotationModeRequest {
  version: NativeWorkbenchAnnotationProtocolVersion
  surfaceId: string
  enabled: boolean
}

export interface NativeWorkbenchAnnotationOverlayShowRequest {
  version: NativeWorkbenchAnnotationProtocolVersion
  surfaceId: string
  selectionId: string
  annotationId: string
  initialBody: string
  overlayCopyVersion?: 1
  copy?: NativeWorkbenchAnnotationOverlayCopy
}

export interface NativeWorkbenchAnnotationOverlayCloseRequest {
  version: NativeWorkbenchAnnotationProtocolVersion
  surfaceId: string
  annotationId?: string
}

export interface NativeWorkbenchAnnotationRect {
  x: number
  y: number
  width: number
  height: number
}

/**
 * A bounded, untrusted candidate emitted to the trusted Control UI. The
 * Gateway must still match it against the canonical revision before creating
 * an editable source anchor.
 */
export interface NativeWorkbenchAnnotationSelection {
  selectionId: string
  tagName: string
  elementPath: string
  domSha256?: string
  elementProofSha256: string
  rect: NativeWorkbenchAnnotationRect
}

export interface NativeWorkbenchAnnotationSelectionCandidate
  extends Omit<NativeWorkbenchAnnotationSelection, 'selectionId'> {
  viewportWidth: number
  viewportHeight: number
}

export interface NativeWorkbenchAnnotationGeometry {
  rect: NativeWorkbenchAnnotationRect
  viewportWidth: number
  viewportHeight: number
}

export type NativeWorkbenchAnnotationOverlayMessage =
  | {
      version: 1
      type: 'draft-changed' | 'submit'
      body: string
    }
  | {
      version: 1
      type: 'cancel'
    }

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

const OVERLAY_COPY_KEYS = [
  'targetLabel',
  'contextLabel',
  'bodyLabel',
  'placeholder',
  'newlineHint',
  'cancelLabel',
  'submitLabel',
  'emptyBodyMessage',
] as const

function boundedOverlayCopyText(value: unknown): string {
  if (typeof value !== 'string') {
    throw new Error('The native Workbench annotation overlay copy is invalid.')
  }
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!normalized || normalized.length > 240) {
    throw new Error('The native Workbench annotation overlay copy is invalid.')
  }
  return normalized
}

function parseOverlayCopy(value: unknown): NativeWorkbenchAnnotationOverlayCopy {
  const copy = objectRecord(value)
  if (!copy || Object.keys(copy).some(key => !OVERLAY_COPY_KEYS.includes(
    key as typeof OVERLAY_COPY_KEYS[number],
  ))) {
    throw new Error('The native Workbench annotation overlay copy is invalid.')
  }
  return Object.fromEntries(OVERLAY_COPY_KEYS.map(key => [
    key,
    boundedOverlayCopyText(copy[key]),
  ])) as unknown as NativeWorkbenchAnnotationOverlayCopy
}

function parseExactRequest(
  value: unknown,
  allowedKeys: readonly string[],
  label: string,
): Record<string, unknown> {
  const request = objectRecord(value)
  if (
    !request
    || (request.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
      && request.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4)
    || Object.keys(request).some(key => !allowedKeys.includes(key))
  ) {
    throw new Error(`The native Workbench annotation ${label} request is invalid.`)
  }
  return request
}

export function parseNativeWorkbenchAnnotationOpaqueId(
  value: unknown,
  label: 'annotation' | 'selection',
): string {
  if (typeof value !== 'string' || !OPAQUE_ID_PATTERN.test(value)) {
    throw new Error(`The native Workbench ${label} identifier is invalid.`)
  }
  return value
}

export function parseNativeWorkbenchAnnotationBody(value: unknown): string {
  if (
    typeof value !== 'string'
    || new TextEncoder().encode(value).byteLength
      > NATIVE_WORKBENCH_ANNOTATION_BODY_MAX_BYTES
  ) {
    throw new Error('The native Workbench annotation body is invalid.')
  }
  return value
}

export function parseNativeWorkbenchAnnotationModeRequest(
  value: unknown,
): NativeWorkbenchAnnotationModeRequest {
  const request = parseExactRequest(value, ['version', 'surfaceId', 'enabled'], 'mode')
  if (typeof request.enabled !== 'boolean') {
    throw new Error('The native Workbench annotation mode is invalid.')
  }
  return {
    version: request.version as NativeWorkbenchAnnotationProtocolVersion,
    surfaceId: parseNativeWorkbenchSurfaceId(request.surfaceId),
    enabled: request.enabled,
  }
}

export function parseNativeWorkbenchAnnotationOverlayShowRequest(
  value: unknown,
): NativeWorkbenchAnnotationOverlayShowRequest {
  const request = parseExactRequest(
    value,
    [
      'version',
      'surfaceId',
      'selectionId',
      'annotationId',
      'initialBody',
      'overlayCopyVersion',
      'copy',
    ],
    'overlay',
  )
  let localizedCopy: Pick<
    NativeWorkbenchAnnotationOverlayShowRequest,
    'overlayCopyVersion' | 'copy'
  > = {}
  if (request.overlayCopyVersion !== undefined || request.copy !== undefined) {
    if (request.overlayCopyVersion !== 1 || request.copy === undefined) {
      throw new Error('The native Workbench annotation overlay copy is invalid.')
    }
    localizedCopy = {
      overlayCopyVersion: 1,
      copy: parseOverlayCopy(request.copy),
    }
  }
  return {
    version: request.version as NativeWorkbenchAnnotationProtocolVersion,
    surfaceId: parseNativeWorkbenchSurfaceId(request.surfaceId),
    selectionId: parseNativeWorkbenchAnnotationOpaqueId(request.selectionId, 'selection'),
    annotationId: parseNativeWorkbenchAnnotationOpaqueId(request.annotationId, 'annotation'),
    initialBody: request.initialBody === undefined
      ? ''
      : parseNativeWorkbenchAnnotationBody(request.initialBody),
    ...localizedCopy,
  }
}

export function parseNativeWorkbenchAnnotationOverlayCloseRequest(
  value: unknown,
): NativeWorkbenchAnnotationOverlayCloseRequest {
  const request = parseExactRequest(
    value,
    ['version', 'surfaceId', 'annotationId'],
    'overlay close',
  )
  return {
    version: request.version as NativeWorkbenchAnnotationProtocolVersion,
    surfaceId: parseNativeWorkbenchSurfaceId(request.surfaceId),
    ...(request.annotationId === undefined
      ? {}
      : {
          annotationId: parseNativeWorkbenchAnnotationOpaqueId(
            request.annotationId,
            'annotation',
          ),
        }),
  }
}

export function parseNativeWorkbenchAnnotationOverlayMessage(
  value: unknown,
): NativeWorkbenchAnnotationOverlayMessage {
  const message = objectRecord(value)
  const type = message?.type
  if (
    !message
    || message.version !== 1
    || (type !== 'draft-changed' && type !== 'submit' && type !== 'cancel')
  ) {
    throw new Error('The trusted annotation overlay message is invalid.')
  }
  if (type === 'cancel') {
    if (Object.keys(message).some(key => !['version', 'type'].includes(key))) {
      throw new Error('The trusted annotation overlay message is invalid.')
    }
    return { version: 1, type }
  }
  if (Object.keys(message).some(key => !['version', 'type', 'body'].includes(key))) {
    throw new Error('The trusted annotation overlay message is invalid.')
  }
  return {
    version: 1,
    type,
    body: parseNativeWorkbenchAnnotationBody(message.body),
  }
}

export function parseNativeWorkbenchAnnotationSelection(
  value: unknown,
): NativeWorkbenchAnnotationSelectionCandidate {
  const selection = objectRecord(value)
  const rect = objectRecord(selection?.rect)
  const finiteRect = rect
    && ['x', 'y', 'width', 'height'].every(key => (
      typeof rect[key] === 'number'
      && Number.isFinite(rect[key])
      && Math.abs(rect[key] as number) <= 1_000_000
    ))
  if (
    !selection
    || Object.keys(selection).some(key => ![
      'ok',
      'tagName',
      'elementPath',
      'domSha256',
      'elementProofSha256',
      'rect',
      'viewportWidth',
      'viewportHeight',
    ].includes(key))
    || (selection.ok !== undefined && selection.ok !== true)
    || typeof selection.tagName !== 'string'
    || !/^[a-z][a-z0-9._:-]{0,63}$/.test(selection.tagName)
    || typeof selection.elementPath !== 'string'
    || selection.elementPath.length === 0
    || selection.elementPath.length > NATIVE_WORKBENCH_ANNOTATION_ELEMENT_PATH_MAX_LENGTH
    || (selection.domSha256 !== undefined && (
      typeof selection.domSha256 !== 'string'
      || !SHA256_PATTERN.test(selection.domSha256)
    ))
    || typeof selection.elementProofSha256 !== 'string'
    || !SHA256_PATTERN.test(selection.elementProofSha256)
    || !finiteRect
    || !Number.isFinite(selection.viewportWidth)
    || !Number.isFinite(selection.viewportHeight)
    || (selection.viewportWidth as number) <= 0
    || (selection.viewportHeight as number) <= 0
    || (selection.viewportWidth as number) > 1_000_000
    || (selection.viewportHeight as number) > 1_000_000
  ) {
    throw new Error('The native Workbench DOM selection is invalid.')
  }
  return {
    tagName: selection.tagName,
    elementPath: selection.elementPath,
    ...(selection.domSha256 === undefined ? {} : { domSha256: selection.domSha256 }),
    elementProofSha256: selection.elementProofSha256,
    rect: {
      x: rect.x as number,
      y: rect.y as number,
      width: Math.max(0, rect.width as number),
      height: Math.max(0, rect.height as number),
    },
    viewportWidth: selection.viewportWidth as number,
    viewportHeight: selection.viewportHeight as number,
  }
}

export function parseNativeWorkbenchAnnotationGeometry(
  value: unknown,
): NativeWorkbenchAnnotationGeometry {
  const geometry = objectRecord(value)
  const rect = objectRecord(geometry?.rect)
  const finiteRect = rect
    && ['x', 'y', 'width', 'height'].every(key => (
      typeof rect[key] === 'number'
      && Number.isFinite(rect[key])
      && Math.abs(rect[key] as number) <= 1_000_000
    ))
  if (
    !geometry
    || Object.keys(geometry).some(key => ![
      'ok',
      'rect',
      'viewportWidth',
      'viewportHeight',
    ].includes(key))
    || (geometry.ok !== undefined && geometry.ok !== true)
    || !finiteRect
    || !Number.isFinite(geometry.viewportWidth)
    || !Number.isFinite(geometry.viewportHeight)
    || (geometry.viewportWidth as number) <= 0
    || (geometry.viewportHeight as number) <= 0
    || (geometry.viewportWidth as number) > 1_000_000
    || (geometry.viewportHeight as number) > 1_000_000
  ) {
    throw new Error('The native Workbench annotation geometry is invalid.')
  }
  return {
    rect: {
      x: rect.x as number,
      y: rect.y as number,
      width: Math.max(0, rect.width as number),
      height: Math.max(0, rect.height as number),
    },
    viewportWidth: geometry.viewportWidth as number,
    viewportHeight: geometry.viewportHeight as number,
  }
}
