import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { access, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { BrowserManagedDownloads, BROWSER_DOWNLOAD_MAX_BYTES } from '../dist/browser-managed-downloads.js'

class Download extends EventEmitter {
  filename = '../synthetic-note.txt'
  mime = 'text/plain'
  total = 0
  received = 0
  cancelled = false
  getFilename() { return this.filename }
  getMimeType() { return this.mime }
  getTotalBytes() { return this.total }
  getReceivedBytes() { return this.received }
  setSavePath(path) { this.path = path }
  cancel() { this.cancelled = true; this.emit('done', {}, 'cancelled') }
  async finish(bytes) {
    await writeFile(this.path, bytes)
    this.received = bytes.length
    this.emit('updated')
    if (!this.cancelled) this.emit('done', {}, 'completed')
  }
}

const owner = { sessionKey: 'synthetic-task', targetRef: 'page-owner', webContentsId: 10 }
const other = { sessionKey: 'synthetic-other', targetRef: 'page-other', webContentsId: 11 }
const store = new BrowserManagedDownloads()
const controller = new AbortController()
const armed = await store.arm(owner, controller.signal)
assert.equal(store.capture(other, new Download()), false, 'Do not consume another page download')
const item = new Download()
assert.equal(store.capture(owner, item), true)
assert.equal(store.capture(owner, new Download()), false, 'Only one download belongs to an armed click')
const original = 'First line\n\nSecond line — 字符\n'
await item.finish(Buffer.from(original))
const receipt = await armed.completed
assert.equal(receipt.state, 'completed')
assert.equal(receipt.name, '.._synthetic-note.txt')
assert.equal(JSON.stringify(receipt).includes(item.path), false)
assert.equal((await store.inspect(owner, receipt.downloadId)).text, original)
const limited = await store.inspect(owner, receipt.downloadId, 5)
assert.equal(limited.text, 'First')
assert.equal(limited.truncated, true)
await assert.rejects(store.inspect(other, receipt.downloadId), error => error.code === 'DOWNLOAD_NOT_FOUND')
await assert.rejects(store.inspect({ ...owner, sessionKey: 'different-task' }, receipt.downloadId),
  error => error.code === 'DOWNLOAD_NOT_FOUND')
await assert.rejects(store.inspect(owner, item.path), error => error.code === 'DOWNLOAD_NOT_FOUND')
armed.cancel()
assert.equal((await store.inspect(owner, receipt.downloadId)).text, original, 'A completed receipt remains readable')

const binaryArm = await store.arm(owner, new AbortController().signal)
const binary = new Download()
store.capture(owner, binary)
await binary.finish(Buffer.from([0xff, 0xfe, 0x00]))
const binaryResult = await binaryArm.completed
assert.equal((await store.inspect(owner, binaryResult.downloadId)).textAvailable, false)

const unicodeArm = await store.arm(owner, new AbortController().signal)
const unicode = new Download()
store.capture(owner, unicode)
await unicode.finish(Buffer.from('a😀b'))
const unicodeResult = await unicodeArm.completed
assert.equal((await store.inspect(owner, unicodeResult.downloadId, 2)).text, 'a')

const pdfArm = await store.arm(owner, new AbortController().signal)
const pdf = new Download()
pdf.filename = '../synthetic-paper.pdf'
pdf.mime = 'application/pdf'
store.capture(owner, pdf)
const pdfBytes = Buffer.from('%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n')
await pdf.finish(pdfBytes)
const pdfReceipt = await pdfArm.completed
const pdfExport = await store.exportPdf(owner, pdfReceipt.downloadId)
assert.equal(pdfExport.byteLength, pdfBytes.length)
assert.equal(Buffer.from(pdfExport.dataBase64, 'base64').compare(pdfBytes), 0)
assert.match(pdfExport.sha256, /^[0-9a-f]{64}$/)
assert.equal(JSON.stringify(pdfExport).includes(pdf.path), false)
await assert.rejects(store.exportPdf(other, pdfReceipt.downloadId), error => error.code === 'DOWNLOAD_NOT_FOUND')
await assert.rejects(store.exportPdf({ ...owner, targetRef: 'other-page' }, pdfReceipt.downloadId),
  error => error.code === 'DOWNLOAD_NOT_FOUND')

const fakePdfArm = await store.arm(owner, new AbortController().signal)
const fakePdf = new Download()
fakePdf.filename = 'synthetic-fake.pdf'
fakePdf.mime = 'application/pdf'
store.capture(owner, fakePdf)
await fakePdf.finish(Buffer.from('<html>Not a PDF</html>'))
const fakePdfReceipt = await fakePdfArm.completed
await assert.rejects(store.exportPdf(owner, fakePdfReceipt.downloadId), error => error.code === 'DOWNLOAD_NOT_PDF')

const wrongMimeArm = await store.arm(owner, new AbortController().signal)
const wrongMime = new Download()
wrongMime.filename = 'synthetic-text.txt'
wrongMime.mime = 'text/plain'
store.capture(owner, wrongMime)
await wrongMime.finish(pdfBytes)
const wrongMimeReceipt = await wrongMimeArm.completed
await assert.rejects(store.exportPdf(owner, wrongMimeReceipt.downloadId), error => error.code === 'DOWNLOAD_NOT_PDF')

const genericPdfArm = await store.arm(owner, new AbortController().signal)
const genericPdf = new Download()
genericPdf.filename = 'synthetic-generic.bin'
genericPdf.mime = 'application/octet-stream'
store.capture(owner, genericPdf)
await genericPdf.finish(pdfBytes)
const genericPdfReceipt = await genericPdfArm.completed
assert.equal((await store.exportPdf(owner, genericPdfReceipt.downloadId)).sha256, pdfExport.sha256)

const largeArm = await store.arm(owner, new AbortController().signal)
const large = new Download()
large.total = BROWSER_DOWNLOAD_MAX_BYTES + 1
store.capture(owner, large)
await assert.rejects(largeArm.completed, error => error.code === 'DOWNLOAD_TOO_LARGE')
assert.equal(large.cancelled, true)

const abort = new AbortController()
const cancelledArm = await store.arm(owner, abort.signal)
const partial = new Download()
store.capture(owner, partial)
await writeFile(partial.path, 'unfinished')
abort.abort()
await assert.rejects(cancelledArm.completed, error => error.code === 'TIMEOUT')
assert.equal(partial.cancelled, true)

const waiting = await store.arm(other, new AbortController().signal)
await store.disposePage(other)
await assert.rejects(waiting.completed, error => error.code === 'TARGET_NOT_FOUND')
assert.equal(store.capture(other, new Download()), false)
await store.disposePage(owner)
await assert.rejects(store.inspect(owner, receipt.downloadId), error => error.code === 'DOWNLOAD_NOT_FOUND')
await assert.rejects(store.exportPdf(owner, pdfReceipt.downloadId), error => error.code === 'DOWNLOAD_NOT_FOUND')
await assert.rejects(access(dirname(item.path)))
await assert.rejects(access(dirname(binary.path)))
await assert.rejects(access(dirname(unicode.path)))
console.log('Managed browser downloads passed: ownership, exact text, bounded content, cancellation and cleanup.')
