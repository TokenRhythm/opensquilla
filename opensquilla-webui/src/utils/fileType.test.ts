import { describe, expect, it } from 'vitest'
import { fileTypeLabel } from './fileType'

describe('fileTypeLabel', () => {
  it.each([
    ['document.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'DOCX'],
    ['workbook.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'XLSX'],
    ['slides.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'PPTX'],
    ['image.JPEG', 'image/jpeg', 'JPEG'],
    ['archive.7z', 'application/octet-stream', '7Z'],
    ['sample.custom', 'application/vnd.example.custom-format', 'CUSTOM'],
    ['sample.csv', '', 'CSV'],
    ['sample.log', 'text/plain', 'LOG'],
  ])('uses the short extension for %s', (name, mime, label) => {
    expect(fileTypeLabel({ name, mime }, 'File')).toBe(label)
  })

  it.each([
    ['application/msword', 'DOC'],
    ['application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'DOCX'],
    ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'XLSX'],
    ['application/vnd.openxmlformats-officedocument.presentationml.presentation', 'PPTX'],
    ['application/vnd.oasis.opendocument.text', 'ODT'],
    ['APPLICATION/PDF; charset=binary', 'PDF'],
    ['image/png', 'PNG'],
    ['image/svg+xml', 'SVG'],
    ['application/x-zip-compressed', 'ZIP'],
    ['application/x-7z-compressed', '7Z'],
    ['application/vnd.rar', 'RAR'],
    ['application/gzip', 'GZ'],
    ['text/plain; charset=utf-8', 'TXT'],
    ['text/csv', 'CSV'],
    ['application/json', 'JSON'],
    ['audio/mpeg', 'MP3'],
    ['video/quicktime', 'MOV'],
  ])('uses a readable MIME label for an extensionless %s file', (mime, label) => {
    expect(fileTypeLabel({ name: 'sample', mime }, 'File')).toBe(label)
  })

  it.each([
    ['', ''],
    ['sample', 'application/octet-stream'],
    ['sample', 'application/vnd.example.custom-format'],
    ['sample.unreasonablylongextension', 'application/x-unreasonablylongsubtype'],
    ['.hidden', ''],
    ['sample.', ''],
    ['sample', 'constructor'],
    ['sample', '__proto__'],
  ])('keeps unknown labels concise and localized: %s / %s', (name, mime) => {
    expect(fileTypeLabel({ name, mime }, '文件')).toBe('文件')
  })
})
