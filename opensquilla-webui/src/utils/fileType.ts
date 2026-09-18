const MIME_TYPE_LABELS: Readonly<Record<string, string>> = {
  'application/pdf': 'PDF',
  'application/msword': 'DOC',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.template': 'DOTX',
  'application/vnd.ms-word.document.macroenabled.12': 'DOCM',
  'application/vnd.ms-excel': 'XLS',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.template': 'XLTX',
  'application/vnd.ms-excel.sheet.macroenabled.12': 'XLSM',
  'application/vnd.ms-excel.sheet.binary.macroenabled.12': 'XLSB',
  'application/vnd.ms-powerpoint': 'PPT',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PPTX',
  'application/vnd.openxmlformats-officedocument.presentationml.slideshow': 'PPSX',
  'application/vnd.ms-powerpoint.presentation.macroenabled.12': 'PPTM',
  'application/vnd.oasis.opendocument.text': 'ODT',
  'application/vnd.oasis.opendocument.spreadsheet': 'ODS',
  'application/vnd.oasis.opendocument.presentation': 'ODP',
  'application/rtf': 'RTF',
  'text/plain': 'TXT',
  'text/markdown': 'MD',
  'text/tab-separated-values': 'TSV',
  'application/xhtml+xml': 'HTML',
  'image/svg+xml': 'SVG',
  'image/vnd.microsoft.icon': 'ICO',
  'image/x-icon': 'ICO',
  'application/zip': 'ZIP',
  'application/x-zip-compressed': 'ZIP',
  'application/gzip': 'GZ',
  'application/x-gzip': 'GZ',
  'application/x-7z-compressed': '7Z',
  'application/vnd.rar': 'RAR',
  'application/x-rar-compressed': 'RAR',
  'application/x-bzip2': 'BZ2',
  'audio/mpeg': 'MP3',
  'audio/x-m4a': 'M4A',
  'video/quicktime': 'MOV',
  'video/x-msvideo': 'AVI',
  'video/x-matroska': 'MKV',
}

/** Display only: a short file extension or MIME label, never a content/type check. */
export function fileTypeLabel(
  file: { name?: string; mime?: string },
  fallback: string,
): string {
  const name = (file.name || '').trim().split(/[/\\]/).pop() || ''
  const extension = name.match(/[^.]\.([a-z0-9]{1,12})$/i)?.[1]
  if (extension) return extension.toUpperCase()

  const mime = (file.mime || '').split(';', 1)[0].trim().toLowerCase()
  const knownLabel = Object.prototype.hasOwnProperty.call(MIME_TYPE_LABELS, mime)
    ? MIME_TYPE_LABELS[mime]
    : undefined
  if (knownLabel) return knownLabel
  if (mime === 'application/octet-stream') return fallback

  // Simple subtypes are useful (PNG, CSV, JSON); vendor identifiers and long
  // compound subtypes are not useful labels for a compact attachment chip.
  const subtype = mime.match(/^[a-z]+\/(?:x-)?([a-z0-9]{1,12})$/)?.[1]
  return subtype ? subtype.toUpperCase() : fallback
}
