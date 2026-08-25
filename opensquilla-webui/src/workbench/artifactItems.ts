import type { ArtifactPayload } from '@/types/rpc'
import type { WorkbenchPreviewDescriptor } from '@/types/workbenchResources'
import {
  artifactFileTitle,
  isOfficeArtifact,
} from '@/utils/chat/artifacts'
import {
  artifactUsesWorkbenchPreview,
  artifactWorkbenchPreviewKind,
} from '@/utils/workbench/artifactPreview'
import type { WorkbenchItem } from './types'

const BASE64_URL_ALPHABET =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'

function fnv64(bytes: Uint8Array, seed: bigint): string {
  let hash = seed
  for (const byte of bytes) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return hash.toString(16).padStart(16, '0')
}

function privateIdentityDigest(value: string): string {
  const bytes = new TextEncoder().encode(value)
  return [
    fnv64(bytes, 0xcbf29ce484222325n),
    fnv64(bytes, 0x84222325cbf29ce4n),
  ].join('')
}

function base64Url(bytes: Uint8Array): string {
  let result = ''
  for (let index = 0; index < bytes.length; index += 3) {
    const first = bytes[index] ?? 0
    const second = bytes[index + 1] ?? 0
    const third = bytes[index + 2] ?? 0
    const packed = (first << 16) | (second << 8) | third
    result += BASE64_URL_ALPHABET[(packed >>> 18) & 63]
    result += BASE64_URL_ALPHABET[(packed >>> 12) & 63]
    if (index + 1 < bytes.length) result += BASE64_URL_ALPHABET[(packed >>> 6) & 63]
    if (index + 2 < bytes.length) result += BASE64_URL_ALPHABET[packed & 63]
  }
  return result
}

/**
 * Artifact identities are normally server-issued IDs and can be represented
 * losslessly. Long legacy fallbacks use a 128-bit digest plus byte length so a
 * tab key stays inside the native surface ID limit without the 32-bit collision
 * class that could alias two old-history artifacts.
 */
function artifactIdentityToken(value: string): string {
  const bytes = new TextEncoder().encode(value)
  const encoded = base64Url(bytes)
  return encoded.length <= 56
    ? `v${bytes.length.toString(36)}-${encoded}`
    : `h${bytes.length.toString(36)}-${privateIdentityDigest(value)}`
}

function artifactIdentity(artifact: ArtifactPayload): string {
  return String(
    artifact.id
      || artifact.key
      || artifact.download_url
      || `${artifact.name || 'artifact'}:${artifact.mime || ''}:${artifact.size || ''}`,
  )
}

export function artifactWorkbenchItemId(
  sessionKey: string,
  artifact: ArtifactPayload,
  resourceIdentity?: string,
): string {
  return [
    'artifact-preview',
    privateIdentityDigest(sessionKey),
    artifactIdentityToken(resourceIdentity || artifactIdentity(artifact)),
  ].join(':')
}

export function artifactCollectionWorkbenchItemId(sessionKey: string): string {
  return [
    'artifact-collection',
    privateIdentityDigest(sessionKey),
  ].join(':')
}

export function createArtifactCollectionWorkbenchItem(options: {
  artifacts: readonly ArtifactPayload[]
  sessionKey: string
  title: string
}): WorkbenchItem {
  const { artifacts, sessionKey, title } = options
  return {
    id: artifactCollectionWorkbenchItemId(sessionKey),
    kind: 'artifact-collection',
    title,
    scope: { type: 'session', id: sessionKey },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: {
      artifacts: [...artifacts],
      sessionKey,
    },
  }
}

export function createArtifactPreviewWorkbenchItem(options: {
  artifact: ArtifactPayload
  initialSection?: 'preview' | 'source'
  initialSectionRequestId?: number
  navigationArtifacts?: readonly ArtifactPayload[]
  nativeHtml: boolean
  preparedPreview?: WorkbenchPreviewDescriptor
  previewLeaseEligible?: boolean
  resourceIdentity?: string
  sessionKey: string
}): WorkbenchItem {
  const {
    artifact,
    initialSection = 'preview',
    initialSectionRequestId = 0,
    navigationArtifacts = [],
    nativeHtml,
    preparedPreview,
    previewLeaseEligible = true,
    resourceIdentity,
    sessionKey,
  } = options
  const kind = artifactWorkbenchPreviewKind(artifact)
  return {
    id: artifactWorkbenchItemId(sessionKey, artifact, resourceIdentity),
    kind: 'artifact-preview',
    title: artifactFileTitle(artifact),
    scope: { type: 'session', id: sessionKey },
    hostKind: nativeHtml && kind === 'html' ? 'native-webcontents' : 'dom',
    // DOM previews own cancellable fetches and Blob URLs. Recreate them on
    // activation instead of retaining every opened document indefinitely.
    // Native HTML surfaces have their own isolated lifecycle and stay mounted.
    retention: nativeHtml && kind === 'html' ? 'keep-alive' : 'dispose-on-suspend',
    payload: {
      artifact,
      initialSection,
      initialSectionRequestId,
      navigationArtifacts: [...navigationArtifacts],
      ...(preparedPreview
        ? {
            preparedPreview: {
              ...preparedPreview,
              resource: { ...preparedPreview.resource },
              ...(preparedPreview.adapter
                ? { adapter: { ...preparedPreview.adapter } }
                : {}),
            },
          }
        : {}),
      previewLeaseEligible,
      resourceIdentity: resourceIdentity || artifactIdentity(artifact),
      sessionKey,
    },
  }
}

export function initialSectionFromWorkbenchItem(
  item: WorkbenchItem | null,
): 'preview' | 'source' {
  return item?.kind === 'artifact-preview' && item.payload.initialSection === 'source'
    ? 'source'
    : 'preview'
}

export function initialSectionRequestIdFromWorkbenchItem(
  item: WorkbenchItem | null,
): number {
  const value = item?.kind === 'artifact-preview'
    ? item.payload.initialSectionRequestId
    : 0
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : 0
}

/**
 * Mark an explicit user request to open a preview section. Metadata-only item
 * refreshes must preserve the existing request id instead of calling this.
 */
export function requestInitialSectionForWorkbenchItem(
  item: WorkbenchItem,
  existing: WorkbenchItem | null,
): WorkbenchItem {
  const previousRequestId = existing?.id === item.id
    ? initialSectionRequestIdFromWorkbenchItem(existing)
    : 0
  const initialSectionRequestId = previousRequestId < Number.MAX_SAFE_INTEGER
    ? previousRequestId + 1
    : 1
  return {
    ...item,
    payload: {
      ...item.payload,
      initialSectionRequestId,
    },
  }
}

/**
 * Returns only the fail-closed preview policy accepted by the Workbench RPC.
 * Arbitrary item payloads cannot opt a normal document preview into this path.
 */
export function preparedPreviewFromWorkbenchItem(
  item: WorkbenchItem | null,
): WorkbenchPreviewDescriptor | null {
  if (item?.kind !== 'artifact-preview') return null
  const value = item.payload.preparedPreview
  if (!value || typeof value !== 'object') return null
  const preview = value as Record<string, unknown>
  const resource = preview.resource
  if (!resource || typeof resource !== 'object') return null
  const ref = resource as Record<string, unknown>
  if (
    preview.mode !== 'isolated'
    || preview.sandboxProfile !== 'opaque-offline'
    || preview.network !== false
    || typeof preview.protocolVersion !== 'number'
    || typeof ref.type !== 'string'
    || typeof ref.id !== 'string'
  ) return null
  return value as WorkbenchPreviewDescriptor
}

export function artifactsFromWorkbenchItem(
  item: WorkbenchItem | null,
): readonly ArtifactPayload[] {
  if (item?.kind !== 'artifact-collection') return []
  const artifacts = item.payload.artifacts
  return Array.isArray(artifacts)
    ? artifacts.filter(
      (artifact): artifact is ArtifactPayload =>
        Boolean(artifact) && typeof artifact === 'object',
    )
    : []
}

export function artifactFromWorkbenchItem(
  item: WorkbenchItem | null,
): ArtifactPayload | null {
  if (item?.kind !== 'artifact-preview') return null
  const artifact = item.payload.artifact
  return artifact && typeof artifact === 'object'
    ? artifact as ArtifactPayload
    : null
}

export function navigationArtifactsFromWorkbenchItem(
  item: WorkbenchItem | null,
): readonly ArtifactPayload[] {
  if (item?.kind !== 'artifact-preview') return []
  const artifacts = item.payload.navigationArtifacts
  return Array.isArray(artifacts)
    ? artifacts.filter(
      (artifact): artifact is ArtifactPayload =>
        Boolean(artifact) && typeof artifact === 'object',
    )
    : []
}

export function previewableNavigationArtifactsFromWorkbenchItem(
  item: WorkbenchItem | null,
): readonly ArtifactPayload[] {
  return navigationArtifactsFromWorkbenchItem(item)
    .filter(artifact => artifactUsesWorkbenchPreview(artifact) || isOfficeArtifact(artifact))
}

export function sessionKeyFromWorkbenchItem(item: WorkbenchItem | null): string {
  if (!item || item.scope.type !== 'session') return ''
  return item.scope.id
}
