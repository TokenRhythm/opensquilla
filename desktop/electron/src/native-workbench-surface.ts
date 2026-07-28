import {
  type BrowserWindow,
  session,
  type Session,
  WebContentsView,
} from 'electron'
import { randomUUID } from 'node:crypto'
import {
  clampNativeWorkbenchSurfaceRect,
  NATIVE_WORKBENCH_ARTIFACT_SCHEME,
  NATIVE_WORKBENCH_PROTOCOL_VERSION,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchNetworkUrlAllowed,
  type NativeWorkbenchCreateRequest,
  type NativeWorkbenchSurfaceEvent,
  type NativeWorkbenchSurfaceRect,
  type NativeWorkbenchSurfaceRectRequest,
} from './native-workbench-surface-contract.js'
import { installDesktopZoomShortcuts } from './desktop-zoom-shortcuts.js'

function artifactHtmlCsp(allowRemoteResources: boolean): string {
  const remote = allowRemoteResources ? ' https:' : ''
  return [
    "default-src 'none'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "child-src 'none'",
    "form-action 'none'",
    "script-src 'self' 'unsafe-inline'",
    `style-src 'self' 'unsafe-inline'${remote}`,
    `img-src 'self' data: blob:${remote}`,
    `media-src 'self' data: blob:${remote}`,
    `font-src 'self' data:${remote}`,
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "manifest-src 'none'",
  ].join('; ')
}

interface NativeWorkbenchSurfaceRecord {
  id: string
  scopeId: string
  handle: string
  documentUrl: string
  owner: BrowserWindow
  previewSession: Session
  view: WebContentsView
  requestedRect: NativeWorkbenchSurfaceRect | null
  rect: NativeWorkbenchSurfaceRect | null
  visibleRequested: boolean
  initialDocumentCommitted: boolean
  disposed: boolean
  crashed: boolean
  missingResourceReported: boolean
  subresourceRequestCount: number
  removeZoomShortcuts: () => void
}

// A single-file preview cannot legitimately need an unbounded number of
// subresources. Keeping this budget in the main process prevents artifact
// scripts from flooding the custom protocol and renderer-to-Control-UI events.
const NATIVE_WORKBENCH_MAX_SUBRESOURCE_REQUESTS = 256

export interface NativeWorkbenchSurfaceResult {
  ok: boolean
  message?: string
}

export interface NativeWorkbenchSurfaceManagerOptions {
  getWindow(): BrowserWindow | null
  emit(event: NativeWorkbenchSurfaceEvent): void
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function notFoundResponse(): Response {
  return new Response('Not found', {
    status: 404,
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    },
  })
}

/**
 * Owns the native content surfaces independently from Vue. Renderer input is
 * already schema-checked before reaching this class; all navigation, network,
 * permission and lifecycle policy is still enforced here in the main process.
 */
export class NativeWorkbenchSurfaceManager {
  private readonly surfaces = new Map<string, NativeWorkbenchSurfaceRecord>()
  private readonly surfaceQueues = new Map<string, Promise<void>>()
  private readonly hookedWindows = new WeakSet<BrowserWindow>()
  private readonly unresponsiveWindows = new WeakSet<BrowserWindow>()
  private activeSurfaceId: string | null = null

  constructor(private readonly options: NativeWorkbenchSurfaceManagerOptions) {}

  async createSurface(
    request: NativeWorkbenchCreateRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    return await this.queueSurfaceOperation(
      request.surfaceId,
      () => this.createSurfaceNow(request),
    )
  }

  private async createSurfaceNow(
    request: NativeWorkbenchCreateRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const previous = this.surfaces.get(request.surfaceId)
    if (previous) await this.destroyRecord(previous)
    const owner = this.options.getWindow()
    if (!owner || owner.isDestroyed()) {
      return { ok: false, message: 'The OpenSquilla window is unavailable.' }
    }

    this.hookWindow(owner)
    const handle = randomUUID()
    const documentUrl = nativeWorkbenchArtifactUrl(handle)
    const previewSession = session.fromPartition(
      `opensquilla-artifact-preview:${randomUUID()}`,
      { cache: false },
    )
    const record: NativeWorkbenchSurfaceRecord = {
      id: request.surfaceId,
      scopeId: request.payload.scopeId,
      handle,
      documentUrl,
      owner,
      previewSession,
      view: new WebContentsView({
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          webSecurity: true,
          webviewTag: false,
          disableDialogs: true,
          disableHtmlFullscreenWindowResize: true,
          session: previewSession,
        },
      }),
      requestedRect: null,
      rect: null,
      visibleRequested: false,
      initialDocumentCommitted: false,
      disposed: false,
      crashed: false,
      missingResourceReported: false,
      subresourceRequestCount: 0,
      removeZoomShortcuts: () => {},
    }
    record.removeZoomShortcuts = installDesktopZoomShortcuts(
      record.view.webContents,
      owner.webContents,
      () => this.refreshBounds(owner),
    )
    this.surfaces.set(record.id, record)

    try {
      await this.configureSession(
        record,
        request.payload.data,
        request.payload.allowRemoteResources,
      )
      this.configureWebContents(record)
      record.view.setVisible(false)
      owner.contentView.addChildView(record.view)
      this.emit(record, 'loading')
      await record.view.webContents.loadURL(record.documentUrl)
      if (record.disposed || this.surfaces.get(record.id) !== record) {
        await this.destroyRecord(record)
        return { ok: false, message: 'The native Workbench surface was closed.' }
      }
      if (record.crashed) {
        return { ok: false, message: 'The native Workbench surface renderer failed.' }
      }
      return { ok: true }
    } catch (error) {
      this.failRecord(record, 'error', { message: errorMessage(error) })
      await this.destroyRecord(record)
      return { ok: false, message: errorMessage(error) }
    }
  }

  setSurfaceRect(request: NativeWorkbenchSurfaceRectRequest): NativeWorkbenchSurfaceResult {
    const record = this.surfaces.get(request.surfaceId)
    if (!record || record.disposed) {
      return { ok: false, message: 'The native Workbench surface no longer exists.' }
    }
    if (record.crashed) {
      return { ok: false, message: 'The native Workbench surface renderer crashed.' }
    }
    if (record.owner.isDestroyed()) {
      void this.destroySurface(record.id)
      return { ok: false, message: 'The OpenSquilla window is unavailable.' }
    }
    record.requestedRect = {
      x: request.x,
      y: request.y,
      width: request.width,
      height: request.height,
    }
    record.rect = this.resolveSurfaceRect(record)
    record.visibleRequested = request.visible && record.rect !== null
    if (record.visibleRequested) {
      this.activateRecord(record)
    } else {
      this.hideRecord(record)
    }
    return { ok: true }
  }

  activateSurface(surfaceId: string): NativeWorkbenchSurfaceResult {
    const record = this.surfaces.get(surfaceId)
    if (!record || record.disposed) {
      return { ok: false, message: 'The native Workbench surface no longer exists.' }
    }
    if (record.crashed) {
      return { ok: false, message: 'The native Workbench surface renderer crashed.' }
    }
    record.visibleRequested = record.rect !== null
    if (record.visibleRequested) this.activateRecord(record)
    return { ok: true }
  }

  async destroySurface(surfaceId: string): Promise<NativeWorkbenchSurfaceResult> {
    return await this.queueSurfaceOperation(
      surfaceId,
      () => this.destroySurfaceNow(surfaceId),
    )
  }

  private async destroySurfaceNow(surfaceId: string): Promise<NativeWorkbenchSurfaceResult> {
    const record = this.surfaces.get(surfaceId)
    if (!record) return { ok: true }
    await this.destroyRecord(record)
    return { ok: true }
  }

  private async destroyRecord(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    if (record.disposed) return
    const isCurrent = this.surfaces.get(record.id) === record
    if (isCurrent) this.surfaces.delete(record.id)
    if (isCurrent && this.activeSurfaceId === record.id) this.activeSurfaceId = null
    record.disposed = true
    record.visibleRequested = false

    try {
      record.removeZoomShortcuts()
    } catch {}
    try {
      record.view.setVisible(false)
      if (!record.owner.isDestroyed()) record.owner.contentView.removeChildView(record.view)
    } catch {}
    try {
      if (!record.view.webContents.isDestroyed()) {
        record.view.webContents.close({ waitForBeforeUnload: false })
      }
    } catch {}
    try {
      await record.previewSession.protocol.unhandle(NATIVE_WORKBENCH_ARTIFACT_SCHEME)
    } catch {}
    await Promise.allSettled([
      record.previewSession.clearStorageData(),
      record.previewSession.clearCache(),
      record.previewSession.clearAuthCache(),
    ])
  }

  async destroyAll(): Promise<void> {
    // Include queued IDs whose replacement record is temporarily between the
    // old-record cleanup and insertion. Enqueuing the destroy behind each
    // create guarantees a close, navigation or owner crash cannot be lost in
    // that gap and later resurrect a native child view.
    const ids = new Set([
      ...this.surfaces.keys(),
      ...this.surfaceQueues.keys(),
    ])
    await Promise.all([...ids].map(id => this.destroySurface(id)))
  }

  private queueSurfaceOperation<T>(
    surfaceId: string,
    operation: () => Promise<T>,
  ): Promise<T> {
    const previous = this.surfaceQueues.get(surfaceId) ?? Promise.resolve()
    const result = previous
      .catch(() => undefined)
      .then(operation)
    const tail = result.then(() => undefined, () => undefined)
    this.surfaceQueues.set(surfaceId, tail)
    void tail.finally(() => {
      if (this.surfaceQueues.get(surfaceId) === tail) {
        this.surfaceQueues.delete(surfaceId)
      }
    })
    return result
  }

  private async configureSession(
    record: NativeWorkbenchSurfaceRecord,
    bytes: Uint8Array,
    allowRemoteResources: boolean,
  ): Promise<void> {
    const { previewSession } = record
    // Response's DOM type requires an ArrayBuffer-backed body. IPC may deliver
    // a SharedArrayBuffer-backed view, so take one bounded immutable snapshot
    // before installing the protocol handler.
    const documentBytes = Uint8Array.from(bytes).buffer
    previewSession.setPermissionCheckHandler(() => false)
    previewSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
      callback(false)
    })
    previewSession.on('will-download', event => event.preventDefault())
    previewSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
      const isDocument = nativeWorkbenchArtifactRequestIsDocument(
        details.url,
        details.method,
        record.handle,
      )
      if (!isDocument) record.subresourceRequestCount += 1
      callback({
        cancel: !nativeWorkbenchNetworkUrlAllowed(
          details.url,
          allowRemoteResources,
          details.resourceType,
        )
          || record.subresourceRequestCount > NATIVE_WORKBENCH_MAX_SUBRESOURCE_REQUESTS,
      })
    })
    await previewSession.protocol.handle(NATIVE_WORKBENCH_ARTIFACT_SCHEME, request => {
      let target: URL
      try {
        target = new URL(request.url)
      } catch {
        return notFoundResponse()
      }
      const isDocument = nativeWorkbenchArtifactRequestIsDocument(
        request.url,
        request.method,
        record.handle,
      )
      if (!isDocument) {
        const path = `${target.pathname}${target.search}`
        if (!record.missingResourceReported) {
          record.missingResourceReported = true
          this.emit(record, 'missing-resource', { path })
        }
        return notFoundResponse()
      }
      return new Response(documentBytes, {
        status: 200,
        headers: {
          'content-type': 'text/html; charset=utf-8',
          'content-security-policy': artifactHtmlCsp(allowRemoteResources),
          'cache-control': 'no-store',
          'referrer-policy': 'no-referrer',
          'x-content-type-options': 'nosniff',
        },
      })
    })
  }

  private configureWebContents(record: NativeWorkbenchSurfaceRecord): void {
    const contents = record.view.webContents
    contents.setWindowOpenHandler(() => ({ action: 'deny' }))
    contents.on('will-navigate', (event, targetUrl) => {
      // Programmatic loadURL is normally excluded from will-navigate, but keep
      // the initial exact document explicitly admissible for Electron changes.
      // Once that document commits, every renderer-initiated top navigation is
      // denied.
      if (record.initialDocumentCommitted || targetUrl !== record.documentUrl) {
        event.preventDefault()
      }
    })
    contents.on('will-redirect', event => event.preventDefault())
    contents.on(
      'did-frame-navigate',
      (_event, targetUrl, _httpResponseCode, _httpStatusText, isMainFrame) => {
        if (isMainFrame && targetUrl === record.documentUrl) {
          record.initialDocumentCommitted = true
        }
      },
    )
    contents.on('before-input-event', (event, input) => {
      if (input.type === 'keyDown' && input.key === 'Escape') {
        event.preventDefault()
        this.emit(record, 'escape')
      }
    })
    contents.on('did-start-loading', () => this.emit(record, 'loading'))
    contents.on('did-finish-load', () => {
      record.initialDocumentCommitted = true
      this.emit(record, 'ready')
    })
    contents.on('did-fail-load', (_event, errorCode, errorDescription, _url, isMainFrame) => {
      if (!isMainFrame || record.disposed || errorCode === -3) return
      // A failed native document must yield to the DOM error state. Keeping the
      // child view visible would cover the recovery controls rendered by Vue.
      this.failRecord(record, 'error', {
        message: errorDescription || `Load failed (${errorCode})`,
      })
    })
    contents.on('render-process-gone', (_event, detail) => {
      this.failRecord(record, 'crashed', { reason: detail.reason })
    })
    contents.on('unresponsive', () => {
      this.failRecord(record, 'crashed', { reason: 'unresponsive' })
    })
  }

  private activateRecord(record: NativeWorkbenchSurfaceRecord): void {
    if (record.disposed || record.crashed || record.owner.isDestroyed() || !record.rect) return
    for (const other of this.surfaces.values()) {
      if (other !== record) this.hideRecord(other)
    }
    this.activeSurfaceId = record.id
    record.view.setBounds(record.rect)
    this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(record.owner))
  }

  private hideRecord(record: NativeWorkbenchSurfaceRecord): void {
    if (this.activeSurfaceId === record.id) this.activeSurfaceId = null
    this.setPhysicalVisibility(record, false)
  }

  private setPhysicalVisibility(
    record: NativeWorkbenchSurfaceRecord,
    visible: boolean,
  ): void {
    try {
      record.view.setVisible(visible)
    } catch {}
  }

  refreshBounds(owner: BrowserWindow): void {
    this.reapplyActiveBounds(owner)
  }

  private reapplyActiveBounds(owner: BrowserWindow): void {
    if (!this.activeSurfaceId) return
    const record = this.surfaces.get(this.activeSurfaceId)
    if (record?.disposed || record?.crashed) {
      this.hideRecord(record)
      return
    }
    if (!record || record.owner !== owner || !record.requestedRect || !record.visibleRequested) {
      return
    }
    record.rect = this.resolveSurfaceRect(record)
    if (!record.rect) {
      this.setPhysicalVisibility(record, false)
      return
    }
    record.view.setBounds(record.rect)
    this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(owner))
  }

  private ownerCanShowSurfaces(owner: BrowserWindow): boolean {
    return !owner.isDestroyed()
      && !this.unresponsiveWindows.has(owner)
      && owner.isVisible()
      && !owner.isMinimized()
  }

  private hideOwnedViews(owner: BrowserWindow): void {
    for (const record of this.surfaces.values()) {
      if (record.owner === owner) this.setPhysicalVisibility(record, false)
    }
  }

  private failOwnedSurfaces(owner: BrowserWindow, reason: string): void {
    for (const record of this.surfaces.values()) {
      if (record.owner === owner) {
        this.failRecord(record, 'crashed', { reason })
      }
    }
  }

  private failRecord(
    record: NativeWorkbenchSurfaceRecord,
    type: 'error' | 'crashed',
    detail: NonNullable<NativeWorkbenchSurfaceEvent['detail']>,
  ): boolean {
    if (record.disposed || record.crashed) return false
    record.crashed = true
    record.visibleRequested = false
    this.hideRecord(record)
    this.emit(record, type, detail)
    return true
  }

  private hookWindow(owner: BrowserWindow): void {
    if (this.hookedWindows.has(owner)) return
    this.hookedWindows.add(owner)
    owner.on('resize', () => this.reapplyActiveBounds(owner))
    owner.on('hide', () => this.hideOwnedViews(owner))
    owner.on('minimize', () => this.hideOwnedViews(owner))
    owner.on('show', () => this.reapplyActiveBounds(owner))
    owner.on('restore', () => this.reapplyActiveBounds(owner))
    owner.webContents.on('zoom-changed', () => this.reapplyActiveBounds(owner))
    owner.webContents.on('unresponsive', () => {
      this.unresponsiveWindows.add(owner)
      this.failOwnedSurfaces(owner, 'owner-unresponsive')
    })
    owner.webContents.on('responsive', () => {
      this.unresponsiveWindows.delete(owner)
    })
    owner.webContents.on('render-process-gone', () => {
      this.unresponsiveWindows.add(owner)
      void this.destroyAll()
    })
    owner.once('closed', () => {
      void this.destroyAll()
    })
  }

  private emit(
    record: NativeWorkbenchSurfaceRecord,
    type: NativeWorkbenchSurfaceEvent['type'],
    detail?: NativeWorkbenchSurfaceEvent['detail'],
  ): void {
    if (
      record.disposed
      || (record.crashed && type !== 'error' && type !== 'crashed')
    ) return
    this.options.emit({
      version: NATIVE_WORKBENCH_PROTOCOL_VERSION,
      surfaceId: record.id,
      type,
      ...(detail ? { detail } : {}),
    })
  }

  private resolveSurfaceRect(record: NativeWorkbenchSurfaceRecord): NativeWorkbenchSurfaceRect | null {
    if (!record.requestedRect || record.owner.isDestroyed()) return null
    const dipRect = nativeWorkbenchCssRectToDip(
      record.requestedRect,
      record.owner.webContents.getZoomFactor(),
    )
    return clampNativeWorkbenchSurfaceRect(dipRect, record.owner.getContentBounds())
  }
}
