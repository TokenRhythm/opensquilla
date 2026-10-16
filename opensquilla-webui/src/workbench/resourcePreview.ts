import { OFFICE_PREVIEW_MIMES, isOfficePreviewFormat, type OfficePreviewFormat } from '@/utils/workbench/artifactPreview'

/** Presentation only. Authority always remains with the resource's transport. */
export type ResourcePreviewKind = OfficePreviewFormat | 'source' | 'image' | 'pdf' | 'html' | 'csv' | 'audio' | 'video' | 'unsupported'
export function resourcePreviewKind(file: { name: string; mime: string; kind?: string }): ResourcePreviewKind {
  const ext = file.name.split('.').pop()?.toLowerCase() || ''
  const mime = file.mime.split(';')[0]!.trim().toLowerCase()
  for (const format of ['docx', 'pptx', 'xlsx'] as const) {
    if (mime === OFFICE_PREVIEW_MIMES[format] || isOfficePreviewFormat(ext) && ext === format) return format
  }
  if (mime === 'application/pdf' || ext === 'pdf') return 'pdf'
  if (mime === 'text/html' || ['html', 'htm'].includes(ext)) return 'html'
  if (mime === 'text/csv' || ['csv', 'tsv'].includes(ext)) return 'csv'
  if (/^image\/(png|apng|jpeg|gif|webp|avif|bmp|x-icon|vnd.microsoft.icon|svg\+xml)$/.test(mime)) return 'image'
  if (/^audio\/(mpeg|mp4|x-m4a|ogg|wav|x-wav|webm|flac|x-flac|aac|aacp|vnd.dlna.adts)$/.test(mime)) return 'audio'
  if (/^video\/(mp4|webm|ogg)$/.test(mime)) return 'video'
  return file.kind === 'text' ? 'source' : 'unsupported'
}

export function resourcePreviewLimit(kind: ResourcePreviewKind): number {
  return ['html', 'csv', 'source'].includes(kind) ? 5 * 1024 * 1024 : 30 * 1024 * 1024
}
