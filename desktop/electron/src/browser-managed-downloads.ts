import type { DownloadItem } from 'electron'
import { randomUUID } from 'node:crypto'
import { chmod, mkdtemp, readFile, rm, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DesktopBrowserError } from './desktop-browser.js'

export const BROWSER_DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024
const MAX_PAGE_DOWNLOADS = 8
const MAX_TEXT_CHARS = 65_536
const DEFAULT_TEXT_CHARS = 16_384

export interface BrowserDownloadOwner {
  sessionKey: string
  targetRef: string
  webContentsId: number
}

interface DownloadRecord {
  id: string
  owner: BrowserDownloadOwner
  directory: string
  path: string
  name: string
  mimeType: string
  size: number
  item?: DownloadItem
  completed: boolean
  settled: boolean
  resolve(value: Record<string, unknown>): void
  reject(reason: unknown): void
  detach(): void
}

/** Page-owned download artifacts. Their private filesystem locations never cross the bridge. */
export class BrowserManagedDownloads {
  private readonly records = new Map<string, DownloadRecord>()
  private readonly armed = new Map<number, DownloadRecord>()

  async arm(owner: BrowserDownloadOwner, signal: AbortSignal) {
    if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The download request ended.', 504)
    if (this.armed.has(owner.webContentsId)) {
      throw new DesktopBrowserError('DOWNLOAD_PENDING', 'A download is already pending for this page.')
    }
    const previous = [...this.records.values()].filter(record => record.owner.targetRef === owner.targetRef)
    if (previous.length >= MAX_PAGE_DOWNLOADS) {
      // Only completed artifacts can be evicted. An in-flight capture remains owned by its request.
      const oldest = previous.find(record => record.completed)
      if (!oldest) throw new DesktopBrowserError('DOWNLOAD_LIMIT', 'Finish the pending page downloads first.')
      await this.remove(oldest)
    }
    let directory: string | undefined
    try {
      directory = await mkdtemp(join(tmpdir(), 'opensquilla-browser-download-'))
      if (process.platform !== 'win32') await chmod(directory, 0o700)
    } catch {
      if (directory) await rm(directory, { recursive: true, force: true }).catch(() => undefined)
      throw new DesktopBrowserError('DOWNLOAD_UNAVAILABLE', 'A private download artifact could not be created.')
    }
    if (signal.aborted) {
      await rm(directory, { recursive: true, force: true }).catch(() => undefined)
      throw new DesktopBrowserError('TIMEOUT', 'The download request ended.', 504)
    }
    let resolve!: (value: Record<string, unknown>) => void
    let reject!: (reason: unknown) => void
    const completed = new Promise<Record<string, unknown>>((yes, no) => { resolve = yes; reject = no })
    void completed.catch(() => undefined)
    const record: DownloadRecord = { id: `download-${randomUUID()}`, owner: { ...owner }, directory,
      path: join(directory, 'artifact'), name: '', mimeType: '', size: 0,
      completed: false, settled: false, resolve, reject, detach: () => {} }
    const abort = () => this.fail(record, new DesktopBrowserError('TIMEOUT', 'The download request ended.', 504))
    signal.addEventListener('abort', abort, { once: true })
    record.detach = () => signal.removeEventListener('abort', abort)
    this.records.set(record.id, record)
    this.armed.set(owner.webContentsId, record)
    return {
      completed,
      cancel: () => this.fail(record, new DesktopBrowserError('DOWNLOAD_CANCELLED', 'The download was cancelled.')),
    }
  }

  /** Called synchronously by the Session's download listener, before Electron opens a save dialog. */
  capture(owner: BrowserDownloadOwner, item: DownloadItem): boolean {
    const record = this.armed.get(owner.webContentsId)
    if (!record || !this.sameOwner(record, owner)) return false
    this.armed.delete(owner.webContentsId)
    record.item = item
    record.name = item.getFilename().replace(/[\u0000-\u001f\u007f/\\]/g, '_').slice(0, 240)
    record.mimeType = item.getMimeType().slice(0, 256)
    const previousDetach = record.detach
    const updated = () => {
      if (item.getReceivedBytes() > BROWSER_DOWNLOAD_MAX_BYTES || item.getTotalBytes() > BROWSER_DOWNLOAD_MAX_BYTES) {
        this.fail(record, new DesktopBrowserError('DOWNLOAD_TOO_LARGE', 'The download exceeds 8 MiB.'))
      }
    }
    const done = (_event: unknown, state: string) => {
      void (async () => {
        if (record.settled) return
        if (state !== 'completed') {
          this.fail(record, new DesktopBrowserError('DOWNLOAD_FAILED', 'The browser download did not complete.'))
          return
        }
        try {
          const metadata = await stat(record.path)
          if (!metadata.isFile() || metadata.size > BROWSER_DOWNLOAD_MAX_BYTES) {
            this.fail(record, new DesktopBrowserError('DOWNLOAD_TOO_LARGE', 'The download exceeds 8 MiB.'))
            return
          }
          if (process.platform !== 'win32') await chmod(record.path, 0o600)
          if (record.settled) return
          record.size = metadata.size
          record.completed = true
          record.settled = true
          record.detach()
          record.resolve(this.metadata(record))
        } catch {
          this.fail(record, new DesktopBrowserError('DOWNLOAD_UNAVAILABLE', 'The download artifact is unavailable.'))
        }
      })()
    }
    record.detach = () => { previousDetach(); item.removeListener('updated', updated); item.removeListener('done', done) }
    item.on('updated', updated)
    item.once('done', done)
    try {
      item.setSavePath(record.path)
      updated()
    } catch {
      this.fail(record, new DesktopBrowserError('DOWNLOAD_UNAVAILABLE', 'The download could not be saved.'))
    }
    return true
  }

  async inspect(owner: BrowserDownloadOwner, downloadId: string, maxChars = DEFAULT_TEXT_CHARS) {
    const record = this.records.get(downloadId)
    if (!record || !this.sameOwner(record, owner) || !record.completed) {
      throw new DesktopBrowserError('DOWNLOAD_NOT_FOUND', 'The completed download is not owned by this page.', 404)
    }
    const metadata = this.metadata(record)
    let bytes: Buffer
    try { bytes = await readFile(record.path) } catch {
      throw new DesktopBrowserError('DOWNLOAD_UNAVAILABLE', 'The download artifact is unavailable.')
    }
    if (bytes.length > BROWSER_DOWNLOAD_MAX_BYTES) {
      throw new DesktopBrowserError('DOWNLOAD_TOO_LARGE', 'The download exceeds 8 MiB.')
    }
    let text: string
    try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes) } catch {
      return { ...metadata, textAvailable: false, reason: 'not_utf8' }
    }
    if (text.includes('\0')) return { ...metadata, textAvailable: false, reason: 'binary_content' }
    const limit = Math.max(1, Math.min(MAX_TEXT_CHARS, Math.floor(maxChars)))
    const end = limit < text.length && /[\uD800-\uDBFF]/.test(text[limit - 1] ?? '') ? limit - 1 : limit
    return { ...metadata, textAvailable: true, text: text.slice(0, end), truncated: text.length > end,
      characterCount: text.length }
  }

  async disposePage(owner: BrowserDownloadOwner): Promise<void> {
    const records = [...this.records.values()].filter(record => this.sameOwner(record, owner))
    for (const record of records) {
      if (!record.settled) this.fail(record, new DesktopBrowserError('TARGET_NOT_FOUND', 'The download page closed.', 404))
    }
    await Promise.all(records.map(record => this.remove(record)))
  }

  private sameOwner(record: DownloadRecord, owner: BrowserDownloadOwner) {
    return record.owner.sessionKey === owner.sessionKey && record.owner.targetRef === owner.targetRef
      && record.owner.webContentsId === owner.webContentsId
  }

  private metadata(record: DownloadRecord) {
    return { downloadId: record.id, name: record.name, mimeType: record.mimeType,
      byteLength: record.size, state: 'completed' }
  }

  private fail(record: DownloadRecord, error: DesktopBrowserError): void {
    if (record.settled) return
    record.settled = true
    record.detach()
    if (this.armed.get(record.owner.webContentsId) === record) this.armed.delete(record.owner.webContentsId)
    if (record.item) {
      // A cancellation can finish asynchronously on Windows. Remove any final
      // partial file after Chromium has released its handle as well.
      record.item.once('done', () => { void this.remove(record) })
      try { record.item.cancel() } catch {}
    }
    record.reject(error)
    void this.remove(record)
  }

  private async remove(record: DownloadRecord): Promise<void> {
    this.records.delete(record.id)
    await rm(record.directory, { recursive: true, force: true }).catch(() => undefined)
  }
}
