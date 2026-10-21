import { describe, expect, it } from 'vitest'
import { resourcePreviewKind, resourcePreviewLimit } from './resourcePreview'

describe('common file preview dispatch', () => {
  it.each([
    ['report.DOCX', 'application/octet-stream', 'docx'],
    ['slides.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'pptx'],
    ['sheets.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'],
    ['document.pdf', 'application/pdf', 'pdf'], ['page.HTM', 'text/plain', 'html'],
    ['table.csv', 'application/vnd.ms-excel', 'csv'], ['table.tsv', 'text/tab-separated-values', 'csv'],
    ['photo.jpg', 'IMAGE/JPEG; charset=binary', 'image'], ['picture.png', 'image/png', 'image'],
    ['animated.png', 'image/apng', 'image'], ['photo.webp', 'image/webp', 'image'],
    ['photo.avif', 'image/avif', 'image'], ['animation.gif', 'image/gif', 'image'],
    ['drawing.svg', 'image/svg+xml', 'image'], ['icon.ico', 'image/vnd.microsoft.icon', 'image'],
    ['image.bmp', 'image/bmp', 'image'], ['song.mp3', 'audio/mpeg', 'audio'],
    ['song.flac', 'audio/x-flac', 'audio'], ['song.m4a', 'audio/x-m4a', 'audio'],
    ['song.aac', 'audio/vnd.dlna.adts', 'audio'], ['song.wav', 'audio/x-wav', 'audio'],
    ['song.ogg', 'audio/ogg', 'audio'], ['clip.mp4', 'video/mp4', 'video'],
    ['clip.webm', 'video/webm', 'video'], ['clip.ogv', 'video/ogg', 'video'],
  ])('routes %s with %s to %s', (name, mime, expected) => {
    expect(resourcePreviewKind({ name, mime })).toBe(expected)
  })
  it.each(['md', 'json', 'jsonl', 'yaml', 'yml', 'toml', 'xml', 'py', 'ts', 'tsx', 'js', 'css', 'sh', 'log', 'txt'])('keeps %s text in source preview', ext => {
    expect(resourcePreviewKind({ name: `example.${ext}`, mime: 'text/plain; charset=utf-8', kind: 'text' })).toBe('source')
  })
  it.each([
    ['old.doc', 'application/msword'], ['old.xls', 'application/vnd.ms-excel'],
    ['old.ppt', 'application/vnd.ms-powerpoint'], ['bundle.zip', 'application/zip'],
    ['photo.heic', 'image/heic'], ['scan.tiff', 'image/tiff'], ['unknown.bin', 'application/octet-stream'],
  ])('does not promise a renderer for unsupported %s', (name, mime) => {
    expect(resourcePreviewKind({ name, mime })).toBe('unsupported')
  })
  it('bounds parsed text and binary previews separately', () => {
    for (const kind of ['source', 'html', 'csv'] as const) expect(resourcePreviewLimit(kind)).toBe(5 * 1024 * 1024)
    for (const kind of ['docx', 'pptx', 'xlsx', 'pdf', 'image', 'audio', 'video'] as const) expect(resourcePreviewLimit(kind)).toBe(30 * 1024 * 1024)
  })
})
