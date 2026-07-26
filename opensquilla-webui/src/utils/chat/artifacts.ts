import type { ArtifactPayload } from '@/types/rpc'
import type { IconName } from '@/utils/icons'

const ARTIFACT_MIME_CATEGORIES: Record<string, string> = {
  'application/json': 'data', 'application/ndjson': 'data', 'application/pdf': 'document',
  'application/x-ndjson': 'data', 'text/csv': 'data', 'text/html': 'document',
  'text/markdown': 'document', 'text/plain': 'document', 'text/tab-separated-values': 'data',
}

const ARTIFACT_EXTENSION_CATEGORIES: Record<string, string> = {
  aac: 'audio', flac: 'audio', m4a: 'audio', mp3: 'audio', oga: 'audio', ogg: 'audio', opus: 'audio', wav: 'audio',
  csv: 'data', htm: 'document', html: 'document', ipynb: 'data', json: 'data',
  jsonl: 'data', log: 'document', markdown: 'document', md: 'document',
  ndjson: 'data', pdf: 'document', sql: 'code', tsv: 'data', txt: 'document',
}

export function artifactMime(artifact: ArtifactPayload): string {
  return artifact?.mime ? String(artifact.mime).toLowerCase() : ''
}

export function artifactName(artifact: ArtifactPayload): string {
  return artifact?.name ? String(artifact.name) : 'artifact'
}

export function artifactExtension(name: string): string {
  const trimmed = String(name || '').trim().toLowerCase()
  const idx = trimmed.lastIndexOf('.')
  if (idx < 0 || idx === trimmed.length - 1) return ''
  return trimmed.slice(idx + 1)
}

export function artifactCategory(artifact: ArtifactPayload): string {
  const mime = artifactMime(artifact)
  if (mime.startsWith('image/')) return 'visual'
  if (mime.startsWith('audio/')) return 'audio'
  if (ARTIFACT_MIME_CATEGORIES[mime]) return ARTIFACT_MIME_CATEGORIES[mime]
  if (!mime || mime === 'application/octet-stream') {
    const ext = artifactExtension(artifactName(artifact))
    if (ARTIFACT_EXTENSION_CATEGORIES[ext]) return ARTIFACT_EXTENSION_CATEGORIES[ext]
  }
  return 'file'
}

export function artifactCategoryLabel(artifact: ArtifactPayload): string {
  const cat = artifactCategory(artifact)
  switch (cat) {
    case 'data': return 'data'
    case 'document': return 'doc'
    case 'code': return 'code'
    case 'audio': return 'audio'
    default: return 'file'
  }
}

export function artifactIconName(artifact: ArtifactPayload): IconName {
  const cat = artifactCategory(artifact)
  if (cat === 'visual') return 'image'
  if (cat === 'data') return 'table'
  if (cat === 'code') return 'fileCode'
  if (cat === 'audio') return 'music'
  return 'fileText'
}

export function artifactFileTitle(artifact: ArtifactPayload): string {
  return artifactName(artifact)
}

/** Short uppercase type badge, e.g. PNG, CSV, PDF, SQL. */
export function artifactKindPill(artifact: ArtifactPayload): string {
  const ext = artifactExtension(artifactName(artifact))
  if (ext) return ext.toUpperCase()
  const mime = artifactMime(artifact)
  const subtype = mime.includes('/') ? mime.slice(mime.indexOf('/') + 1) : mime
  const cleaned = subtype.replace(/^x[-.]/, '').replace(/[+.].*$/, '')
  return cleaned ? cleaned.toUpperCase() : artifactCategoryLabel(artifact).toUpperCase()
}

/** Human-readable byte size, e.g. "727 KB". Empty when size is unknown. */
export function artifactSizeLabel(artifact: ArtifactPayload): string {
  if (!artifact?.size) return ''
  return `${Math.max(1, Math.round(Number(artifact.size) / 1024))} KB`
}

/**
 * Compact meta line shown in cards: `TYPE · size`, e.g. `PNG · 727 KB`.
 * No "Preview file"/"Download file" prefix and no doubled uppercase category.
 */
export function artifactFileSubtitle(artifact: ArtifactPayload): string {
  return [artifactKindPill(artifact), artifactSizeLabel(artifact)].filter(Boolean).join(' · ')
}

/**
 * Previewable types get an Open affordance: images plus documents
 * (pdf / html / markdown / plain text). Data and code are download-only.
 */
export function canPreview(artifact: ArtifactPayload): boolean {
  const cat = artifactCategory(artifact)
  return cat === 'visual' || cat === 'document'
}

export function artifactActionLabel(artifact: ArtifactPayload): string {
  return canPreview(artifact) ? 'Open' : 'Download'
}

export function artifactMeta(artifact: ArtifactPayload): string {
  const mime = artifact?.mime ? String(artifact.mime) : ''
  const size = artifactSizeLabel(artifact)
  return [mime, size].filter(Boolean).join(' · ')
}

export interface ArtifactUrlOptions {
  sessionKey?: string
  absolute?: boolean
  includeSessionKey?: boolean
}

export function artifactDownloadUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: ArtifactUrlOptions = {},
): string {
  let raw = artifact?.download_url ? String(artifact.download_url) : ''
  if (!raw && artifact?.id) raw = `/api/v1/artifacts/${encodeURIComponent(artifact.id)}`
  if (!raw) return ''
  try {
    const url = new URL(raw, baseOrigin)
    const base = new URL(baseOrigin)
    const sameOrigin = url.origin === base.origin
    if (sameOrigin) {
      url.searchParams.delete('token')
      url.searchParams.delete('sessionKey')
      url.searchParams.delete('session_key')
    }
    const artifactSession = artifact.sessionKey || artifact.session_key
    const sessionKey = options.sessionKey || (artifactSession ? String(artifactSession) : '')
    if (
      sameOrigin &&
      options.includeSessionKey === true &&
      sessionKey &&
      !url.searchParams.get('sessionKey') &&
      !url.searchParams.get('session_key')
    ) {
      url.searchParams.set('sessionKey', sessionKey)
    }
    if (!sameOrigin || options.absolute) return url.toString()
    return url.pathname + url.search + url.hash
  } catch { return raw }
}

export function artifactPreviewUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: ArtifactUrlOptions = {},
): string {
  return artifactDownloadUrl(artifact, baseOrigin, options)
}

/**
 * Small thumbnail URL for grid/inline previews. Prefers the backend-supplied
 * `thumbnail_url` (a `{download_url}?variant=thumb` webp); when it is absent we
 * fall back to the full download URL so older artifacts still render a preview.
 */
export function artifactThumbnailUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: ArtifactUrlOptions = {},
): string {
  const thumb = artifact?.thumbnail_url ? String(artifact.thumbnail_url) : ''
  if (thumb) return artifactDownloadUrl({ ...artifact, download_url: thumb }, baseOrigin, options)
  return artifactDownloadUrl(artifact, baseOrigin, options)
}
