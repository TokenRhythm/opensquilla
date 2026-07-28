import {
  BrowserWindow,
  desktopCapturer,
  dialog,
  session,
  shell,
  type Certificate,
  type Session,
  WebContentsView,
} from 'electron'
import { randomUUID } from 'node:crypto'
import { isIP } from 'node:net'
import {
  clampNativeWorkbenchSurfaceRect,
  NATIVE_WORKBENCH_ARTIFACT_SCHEME,
  NATIVE_WORKBENCH_MAX_SURFACES,
  NATIVE_WORKBENCH_PROTOCOL_VERSION,
  NATIVE_WORKBENCH_PROTOCOL_VERSION_V2,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchDownloadAllowed,
  nativeWorkbenchNetworkUrlAllowed,
  nativeWorkbenchV2NetworkUrlAllowed,
  type NativeWorkbenchCreateRequest,
  type NativeWorkbenchNavigationRequest,
  type NativeWorkbenchPermissionResponse,
  type NativeWorkbenchPreviewMode,
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
  version: 1 | 2
  kind: NativeWorkbenchCreateRequest['kind']
  mode: NativeWorkbenchPreviewMode
  scopeId: string
  handle: string | null
  documentUrl: string
  expectedOrigin: string | null
  owner: BrowserWindow
  previewSession: Session
  view: WebContentsView
  requestedRect: NativeWorkbenchSurfaceRect | null
  rect: NativeWorkbenchSurfaceRect | null
  visibleRequested: boolean
  initialDocumentCommitted: boolean
  disposed: boolean
  crashed: boolean
  cleanupPromise: Promise<void> | null
  missingResourceReported: boolean
  blockedNetworkReported: boolean
  privilegedOriginReported: boolean
  subresourceRequestCount: number
  removeZoomShortcuts: () => void
  lastTrustedGestureAt: number
  permissionGrants: Set<string>
  pendingPermissions: Map<string, NativeWorkbenchPendingPermission>
  pendingAuthentication: NativeWorkbenchPendingAuthentication | null
  authenticationAttempts: Map<string, number>
}

interface NativeWorkbenchPendingPermission {
  requestId: string
  origin: string
  permission: string
  grantPermissions: string[]
  callback(allowed: boolean): void
  timeout: NodeJS.Timeout
}

interface NativeWorkbenchPendingAuthentication {
  challengeKey: string
  callback(username?: string, password?: string): void
  prompt: BrowserWindow
  promptSession: Session
  timeout: NodeJS.Timeout
}

// A single-file preview cannot legitimately need an unbounded number of
// subresources. Keeping this budget in the main process prevents artifact
// scripts from flooding the custom protocol and renderer-to-Control-UI events.
const NATIVE_WORKBENCH_MAX_SUBRESOURCE_REQUESTS = 256
const NATIVE_WORKBENCH_PERMISSION_TIMEOUT_MS = 30_000
const NATIVE_WORKBENCH_AUTH_TIMEOUT_MS = 30_000
const NATIVE_WORKBENCH_MAX_AUTH_ATTEMPTS = 3
const NATIVE_WORKBENCH_USER_GESTURE_WINDOW_MS = 1_500
const NATIVE_WORKBENCH_EXTERNAL_PROTOCOLS = new Set(['mailto:', 'sms:', 'tel:'])
const NATIVE_WORKBENCH_OFFLINE_WEBRTC_CSP = "webrtc 'block'"
const NATIVE_WORKBENCH_OFFLINE_REALM_GUARD = `(() => {
  const blockedConstructors = [
    'RTCPeerConnection',
    'webkitRTCPeerConnection',
    'mozRTCPeerConnection',
    'RTCIceGatherer',
    'RTCIceTransport',
    'RTCDtlsTransport',
    'RTCSctpTransport',
    'RTCQuicTransport',
  ]
  for (const name of blockedConstructors) {
    try {
      Object.defineProperty(globalThis, name, {
        configurable: false,
        enumerable: false,
        value: undefined,
        writable: false,
      })
    } catch {}
  }
})()`
const NATIVE_WORKBENCH_PROMPTABLE_PERMISSIONS = new Set([
  'clipboard-read',
  'clipboard-sanitized-write',
  'display-capture',
  'geolocation',
  'media',
])

export interface NativeWorkbenchSurfaceResult {
  ok: boolean
  message?: string
}

export interface NativeWorkbenchSurfaceManagerOptions {
  authenticationTimeoutMs?: number
  getPrivilegedGatewayUrl?(): string | null
  getWindow(): BrowserWindow | null
  emit(event: NativeWorkbenchSurfaceEvent): void
  forceArtifactPreviewsOffline?: boolean
  permissionTimeoutMs?: number
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

function appendResponseHeader(
  source: Record<string, string[]> | undefined,
  name: string,
  value: string,
): Record<string, string[]> {
  const headers = { ...(source ?? {}) }
  const existingKey = Object.keys(headers).find(key => key.toLowerCase() === name.toLowerCase())
  const key = existingKey ?? name
  headers[key] = [...(headers[key] ?? []), value]
  return headers
}

function replaceResponseHeader(
  source: Record<string, string[]> | undefined,
  name: string,
  value: string,
): Record<string, string[]> {
  const headers = { ...(source ?? {}) }
  for (const key of Object.keys(headers)) {
    if (key.toLowerCase() === name.toLowerCase()) delete headers[key]
  }
  headers[name] = [value]
  return headers
}

function effectiveHttpPort(url: URL): string {
  if (url.port) return url.port
  return url.protocol === 'https:' || url.protocol === 'wss:' ? '443' : '80'
}

function normalizedUrlHostname(value: string): string {
  return value.replace(/^\[|\]$/g, '').replace(/\.$/, '').toLowerCase()
}

function isLoopbackUrlHostname(value: string): boolean {
  const hostname = normalizedUrlHostname(value)
  if (hostname === 'localhost' || hostname.endsWith('.localhost')) return true
  if (hostname === '::1') return true
  if (hostname.startsWith('::ffff:')) {
    return isLoopbackUrlHostname(hostname.slice('::ffff:'.length))
  }
  return isIP(hostname) === 4 && hostname.startsWith('127.')
}

const BASIC_AUTH_PROMPT_HTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta
    http-equiv="Content-Security-Policy"
    content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'none'"
  >
  <meta name="color-scheme" content="light dark">
  <title>Sign in to preview</title>
  <style>
    :root { font: 14px system-ui, sans-serif; color-scheme: light dark; }
    body { margin: 0; padding: 24px; background: Canvas; color: CanvasText; }
    h1 { margin: 0 0 8px; font-size: 18px; }
    p { margin: 0 0 18px; color: GrayText; overflow-wrap: anywhere; }
    label { display: grid; gap: 6px; margin: 12px 0; font-weight: 600; }
    input {
      min-width: 0; padding: 9px 10px; border: 1px solid GrayText;
      border-radius: 6px; background: Field; color: FieldText; font: inherit;
    }
    footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }
    button { padding: 8px 14px; border: 1px solid GrayText; border-radius: 6px; font: inherit; }
    button[type="submit"] { background: Highlight; color: HighlightText; }
  </style>
</head>
<body>
  <main>
    <h1>Sign in to this preview</h1>
    <p id="challenge"></p>
    <form id="credentials" autocomplete="off">
      <label>Username
        <input id="username" name="username" autocomplete="off" maxlength="1024" autofocus>
      </label>
      <label>Password
        <input
          id="password"
          name="password"
          type="password"
          autocomplete="new-password"
          maxlength="4096"
          data-1p-ignore
          data-lpignore="true"
        >
      </label>
      <footer>
        <button id="cancel" type="button">Cancel</button>
        <button type="submit">Sign in</button>
      </footer>
    </form>
  </main>
</body>
</html>`

/**
 * Owns the native content surfaces independently from Vue. Renderer input is
 * already schema-checked before reaching this class; all navigation, network,
 * permission and lifecycle policy is still enforced here in the main process.
 */
export class NativeWorkbenchSurfaceManager {
  private readonly surfaces = new Map<string, NativeWorkbenchSurfaceRecord>()
  private readonly surfaceQueues = new Map<string, Promise<void>>()
  private readonly recordCleanups = new Set<Promise<void>>()
  private readonly hookedWindows = new WeakSet<BrowserWindow>()
  private readonly unresponsiveWindows = new WeakSet<BrowserWindow>()
  private activeSurfaceId: string | null = null

  constructor(private readonly options: NativeWorkbenchSurfaceManagerOptions) {}

  async createSurface(
    request: NativeWorkbenchCreateRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const pending = this.surfaces.get(request.surfaceId)
    if (pending) this.cancelPendingAuthentication(pending)
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
    if (this.surfaces.size >= NATIVE_WORKBENCH_MAX_SURFACES) {
      return {
        ok: false,
        message: `Close a Workbench preview before opening more than ${NATIVE_WORKBENCH_MAX_SURFACES}.`,
      }
    }
    const owner = this.options.getWindow()
    if (!owner || owner.isDestroyed()) {
      return { ok: false, message: 'The OpenSquilla window is unavailable.' }
    }

    this.hookWindow(owner)
    const isLegacyArtifact = request.kind === 'artifact-html'
    const handle = isLegacyArtifact ? randomUUID() : null
    const documentUrl = isLegacyArtifact
      ? nativeWorkbenchArtifactUrl(handle!)
      : request.kind === 'artifact-preview'
        ? request.payload.launchUrl
        : request.payload.url
    const expectedOrigin = request.kind === 'artifact-preview'
      ? request.payload.expectedOrigin
      : null
    const mode = request.kind === 'artifact-preview'
      ? this.options.forceArtifactPreviewsOffline
        ? 'offline'
        : request.payload.mode
      : 'full'
    const previewSession = session.fromPartition(
      `${isLegacyArtifact
        ? 'opensquilla-artifact-preview'
        : 'opensquilla-workbench-preview'}:${randomUUID()}`,
      { cache: false },
    )
    const record: NativeWorkbenchSurfaceRecord = {
      id: request.surfaceId,
      version: request.version,
      kind: request.kind,
      mode,
      scopeId: request.payload.scopeId,
      handle,
      documentUrl,
      expectedOrigin,
      owner,
      previewSession,
      view: new WebContentsView({
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          webSecurity: true,
          webviewTag: false,
          disableDialogs: isLegacyArtifact,
          disableHtmlFullscreenWindowResize: true,
          ...(isLegacyArtifact
            ? {}
            : {
                devTools: false,
                navigateOnDragDrop: false,
                safeDialogs: true,
                safeDialogsMessage: 'Repeated dialogs were blocked in this temporary preview.',
                spellcheck: true,
              }),
          session: previewSession,
        },
      }),
      requestedRect: null,
      rect: null,
      visibleRequested: false,
      initialDocumentCommitted: false,
      disposed: false,
      crashed: false,
      cleanupPromise: null,
      missingResourceReported: false,
      blockedNetworkReported: false,
      privilegedOriginReported: false,
      subresourceRequestCount: 0,
      removeZoomShortcuts: () => {},
      lastTrustedGestureAt: 0,
      permissionGrants: new Set(),
      pendingPermissions: new Map(),
      pendingAuthentication: null,
      authenticationAttempts: new Map(),
    }
    record.removeZoomShortcuts = installDesktopZoomShortcuts(
      record.view.webContents,
      owner.webContents,
      () => this.refreshBounds(owner),
    )
    this.surfaces.set(record.id, record)

    try {
      if (request.kind === 'artifact-html') {
        await this.configureLegacySession(
          record,
          request.payload.data,
          request.payload.allowRemoteResources,
        )
      } else {
        await this.configureV2Session(record)
      }
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

  async navigateSurface(
    request: NativeWorkbenchNavigationRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const record = this.surfaces.get(request.surfaceId)
    if (!record || record.disposed) {
      return { ok: false, message: 'The native Workbench surface no longer exists.' }
    }
    if (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V2) {
      return { ok: false, message: 'This native Workbench surface does not support navigation.' }
    }
    if (record.crashed || record.view.webContents.isDestroyed()) {
      return { ok: false, message: 'The native Workbench surface renderer crashed.' }
    }
    const contents = record.view.webContents
    this.cancelPendingAuthentication(record)
    if (
      request.action === 'navigate'
      || request.action === 'back'
      || request.action === 'forward'
      || request.action === 'reload'
    ) {
      this.rejectPendingPermissions(record)
    }
    if (request.action !== 'stop' && request.action !== 'open-external') {
      record.authenticationAttempts.clear()
    }
    try {
      switch (request.action) {
        case 'navigate':
          if (!this.v2TopLevelNavigationAllowed(record, request.url!)) {
            this.reportPrivilegedGatewayBlock(record, request.url!)
            return {
              ok: false,
              message: 'The OpenSquilla Gateway is unavailable inside isolated previews.',
            }
          }
          await contents.loadURL(request.url!)
          break
        case 'back':
          if (contents.navigationHistory.canGoBack()) contents.navigationHistory.goBack()
          break
        case 'forward':
          if (contents.navigationHistory.canGoForward()) contents.navigationHistory.goForward()
          break
        case 'reload':
          contents.reload()
          break
        case 'stop':
          contents.stop()
          break
        case 'open-external':
          await shell.openExternal(request.url!, { activate: true })
          break
      }
      this.emitNavigationState(record)
      return { ok: true }
    } catch (error) {
      return { ok: false, message: errorMessage(error) }
    }
  }

  respondToPermission(
    response: NativeWorkbenchPermissionResponse,
  ): NativeWorkbenchSurfaceResult {
    const record = this.surfaces.get(response.surfaceId)
    if (!record || record.disposed) {
      return { ok: false, message: 'The native Workbench surface no longer exists.' }
    }
    if (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V2) {
      return { ok: false, message: 'This native Workbench surface has no pending permissions.' }
    }
    const pending = record.pendingPermissions.get(response.requestId)
    if (!pending) {
      return { ok: false, message: 'The native Workbench permission request expired.' }
    }
    record.pendingPermissions.delete(response.requestId)
    clearTimeout(pending.timeout)
    if (response.allow) {
      for (const permission of pending.grantPermissions) {
        record.permissionGrants.add(this.permissionGrantKey(
          pending.origin,
          permission,
        ))
      }
    }
    pending.callback(response.allow)
    return { ok: true }
  }

  async destroySurface(surfaceId: string): Promise<NativeWorkbenchSurfaceResult> {
    const pending = this.surfaces.get(surfaceId)
    if (pending) this.cancelPendingAuthentication(pending)
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

  private destroyRecord(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    if (record.cleanupPromise) return record.cleanupPromise
    const isCurrent = this.surfaces.get(record.id) === record
    if (isCurrent) this.surfaces.delete(record.id)
    if (isCurrent && this.activeSurfaceId === record.id) this.activeSurfaceId = null
    record.disposed = true
    record.visibleRequested = false
    this.rejectPendingPermissions(record)
    this.cancelPendingAuthentication(record)

    try {
      record.removeZoomShortcuts()
    } catch {}
    try {
      record.view.setVisible(false)
      if (!record.owner.isDestroyed()) record.owner.contentView.removeChildView(record.view)
    } catch {}
    try {
      if (!record.view.webContents.isDestroyed()) {
        if (record.view.webContents.debugger.isAttached()) {
          record.view.webContents.debugger.detach()
        }
        record.view.webContents.close({ waitForBeforeUnload: false })
      }
    } catch {}

    const cleanupPromise = this.cleanupDisposedRecord(record)
    record.cleanupPromise = cleanupPromise
    this.recordCleanups.add(cleanupPromise)
    void cleanupPromise.then(
      () => this.recordCleanups.delete(cleanupPromise),
      () => this.recordCleanups.delete(cleanupPromise),
    )
    return cleanupPromise
  }

  private async cleanupDisposedRecord(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    if (record.kind === 'artifact-html') {
      try {
        await record.previewSession.protocol.unhandle(NATIVE_WORKBENCH_ARTIFACT_SCHEME)
      } catch {}
    }
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
    for (const record of this.surfaces.values()) {
      this.cancelPendingAuthentication(record)
    }
    await Promise.all([...ids].map(id => this.destroySurface(id)))
    await Promise.allSettled([...this.recordCleanups])
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

  private async configureLegacySession(
    record: NativeWorkbenchSurfaceRecord,
    bytes: Uint8Array,
    allowRemoteResources: boolean,
  ): Promise<void> {
    const { previewSession } = record
    if (!record.handle) throw new Error('The native Workbench artifact handle is missing.')
    const handle = record.handle
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
        handle,
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
        handle,
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

  private async configureV2Session(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    const { previewSession } = record
    if (record.mode === 'offline') {
      await this.installOfflineRealmGuard(record)
      record.view.webContents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp')
      previewSession.webRequest.onHeadersReceived(
        { urls: ['<all_urls>'] },
        (details, callback) => {
          let responseHeaders = appendResponseHeader(
            details.responseHeaders,
            'Content-Security-Policy',
            NATIVE_WORKBENCH_OFFLINE_WEBRTC_CSP,
          )
          responseHeaders = replaceResponseHeader(
            responseHeaders,
            'X-DNS-Prefetch-Control',
            'off',
          )
          callback({ responseHeaders })
        },
      )
    }
    previewSession.setDevicePermissionHandler(() => false)
    previewSession.on('select-hid-device', (event, _details, callback) => {
      event.preventDefault()
      callback()
      this.emit(record, 'blocked-action', {
        action: 'hid',
        reason: 'unsupported-device-permission',
      })
    })
    previewSession.on('select-usb-device', (event, _details, callback) => {
      event.preventDefault()
      callback()
      this.emit(record, 'blocked-action', {
        action: 'usb',
        reason: 'unsupported-device-permission',
      })
    })
    previewSession.on('select-serial-port', (event, _ports, _contents, callback) => {
      event.preventDefault()
      callback('')
      this.emit(record, 'blocked-action', {
        action: 'serial',
        reason: 'unsupported-device-permission',
      })
    })
    previewSession.setPermissionCheckHandler(
      (webContents, permission, requestingOrigin, details) => (
        webContents === record.view.webContents
        && record.permissionGrants.has(this.permissionGrantKey(
          this.normalizedOrigin(requestingOrigin),
          permission === 'media'
            ? `media:${details.mediaType ?? 'unknown'}`
            : permission,
        ))
      ),
    )
    previewSession.setPermissionRequestHandler(
      (webContents, permission, callback, details) => {
        if (
          webContents !== record.view.webContents
          || !NATIVE_WORKBENCH_PROMPTABLE_PERMISSIONS.has(permission)
        ) {
          callback(false)
          this.emit(record, 'blocked-action', {
            action: 'permission',
            reason: 'unsupported-permission',
          })
          return
        }
        const origin = this.permissionRequestOrigin(details.requestingUrl)
        if (!origin) {
          callback(false)
          return
        }
        const mediaTypes = 'mediaTypes' in details && Array.isArray(details.mediaTypes)
          ? details.mediaTypes
          : undefined
        const grantPermissions = permission === 'media' && mediaTypes
          ? mediaTypes.map(mediaType => `media:${mediaType}`)
          : [permission]
        const permissionLabel = permission === 'media' && mediaTypes
          ? mediaTypes.includes('video') && mediaTypes.includes('audio')
            ? 'camera-and-microphone'
            : mediaTypes.includes('video')
              ? 'camera'
              : mediaTypes.includes('audio')
                ? 'microphone'
                : 'media'
          : permission
        this.requestPermission(record, {
          origin,
          permission: permissionLabel,
          grantPermissions,
          callback,
          ...(mediaTypes ? { mediaTypes } : {}),
        })
      },
    )
    previewSession.setDisplayMediaRequestHandler((request, callback) => {
      const origin = this.permissionRequestOrigin(request.securityOrigin)
      if (!request.userGesture || !origin) {
        callback({})
        this.emit(record, 'blocked-action', {
          action: 'display-capture',
          reason: 'user-gesture-required',
        })
        return
      }
      this.requestPermission(record, {
        origin,
        permission: 'display-capture',
        callback: allowed => {
          if (!allowed) {
            callback({})
            return
          }
          void this.chooseDisplayMedia(record, request, callback)
        },
      })
    }, { useSystemPicker: false })
    previewSession.on('will-download', (event, item, webContents) => {
      if (
        webContents !== record.view.webContents
        || record.disposed
        || !nativeWorkbenchDownloadAllowed(item.hasUserGesture())
      ) {
        event.preventDefault()
        this.emit(record, 'blocked-action', {
          action: 'download',
          targetUrl: item.getURL(),
          reason: 'user-gesture-required',
        })
        return
      }
      // Leaving the save path unset makes Electron show its native confirmation
      // dialog. Supplying options here makes that contract explicit.
      item.setSaveDialogOptions({ title: 'Save preview download' })
    })
    previewSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
      const networkAllowed = nativeWorkbenchV2NetworkUrlAllowed(
        details.url,
        record.mode,
        record.expectedOrigin ?? undefined,
      )
      const privilegedGateway = (
        networkAllowed
        && this.isPrivilegedGatewayTarget(details.url)
      )
      const allowed = networkAllowed && !privilegedGateway
      if (privilegedGateway) {
        this.reportPrivilegedGatewayBlock(record, details.url)
      } else if (!allowed && record.mode === 'offline' && !record.blockedNetworkReported) {
        record.blockedNetworkReported = true
        this.emit(record, 'blocked-action', {
          action: 'network',
          reason: 'offline-policy',
        })
      }
      callback({
        cancel: !allowed,
      })
    })
    previewSession.webRequest.onCompleted({ urls: ['<all_urls>'] }, details => {
      if (
        details.resourceType !== 'mainFrame'
        && details.statusCode >= 400
        && !record.missingResourceReported
      ) {
        record.missingResourceReported = true
        this.emit(record, 'missing-resource', { reason: 'http-error' })
      }
    })
    previewSession.webRequest.onErrorOccurred({ urls: ['<all_urls>'] }, details => {
      if (
        details.resourceType !== 'mainFrame'
        && !record.missingResourceReported
        && !record.blockedNetworkReported
        && !record.privilegedOriginReported
      ) {
        record.missingResourceReported = true
        this.emit(record, 'missing-resource', { reason: 'network-error' })
      }
    })
  }

  private async installOfflineRealmGuard(
    record: NativeWorkbenchSurfaceRecord,
  ): Promise<void> {
    const contents = record.view.webContents
    const command = async (
      method: string,
      params?: Record<string, unknown>,
    ): Promise<unknown> => {
      let timeout: NodeJS.Timeout | undefined
      try {
        return await Promise.race([
          contents.debugger.sendCommand(method, params),
          new Promise<never>((_resolve, reject) => {
            timeout = setTimeout(
              () => reject(new Error(`Offline isolation command timed out: ${method}`)),
              5_000,
            )
            timeout.unref()
          }),
        ])
      } finally {
        if (timeout) clearTimeout(timeout)
      }
    }
    // A newly-created WebContentsView has no renderer target until its first
    // navigation. Materialize a trusted empty document before attaching CDP;
    // the untrusted artifact is loaded only after the guard is registered.
    await contents.loadURL('about:blank')
    contents.debugger.attach('1.3')
    contents.debugger.on('detach', (_event, reason) => {
      if (record.disposed || record.crashed) return
      this.failRecord(record, 'error', {
        message: 'The offline browser isolation guard stopped unexpectedly.',
        reason: reason || 'offline-realm-guard-detached',
      })
    })
    await command('Page.enable')
    await command('Page.addScriptToEvaluateOnNewDocument', {
      source: NATIVE_WORKBENCH_OFFLINE_REALM_GUARD,
      runImmediately: true,
    })
    const verification = await command('Runtime.evaluate', {
      expression: `[
        'RTCPeerConnection',
        'webkitRTCPeerConnection',
        'mozRTCPeerConnection',
        'RTCIceGatherer',
        'RTCIceTransport',
      ].every(name => typeof globalThis[name] === 'undefined')`,
      returnByValue: true,
    }) as {
      result?: {
        value?: unknown
      }
    }
    if (verification.result?.value !== true) {
      throw new Error('The offline browser isolation guard could not disable WebRTC.')
    }
  }

  private requestPermission(
    record: NativeWorkbenchSurfaceRecord,
    request: {
      origin: string
      permission: string
      grantPermissions?: string[]
      mediaTypes?: string[]
      callback(allowed: boolean): void
    },
  ): void {
    const grantPermissions = request.grantPermissions ?? [request.permission]
    if (grantPermissions.every(permission =>
      record.permissionGrants.has(this.permissionGrantKey(request.origin, permission)))) {
      request.callback(true)
      return
    }
    const requestId = randomUUID()
    let settled = false
    const finish = (allowed: boolean) => {
      if (settled) return
      settled = true
      request.callback(allowed)
    }
    const timeout = setTimeout(() => {
      record.pendingPermissions.delete(requestId)
      finish(false)
    }, this.options.permissionTimeoutMs ?? NATIVE_WORKBENCH_PERMISSION_TIMEOUT_MS)
    timeout.unref()
    record.pendingPermissions.set(requestId, {
      requestId,
      origin: request.origin,
      permission: request.permission,
      grantPermissions,
      callback: finish,
      timeout,
    })
    this.emit(record, 'permission-request', {
      requestId,
      permission: request.permission,
      requestingOrigin: request.origin,
      ...(request.mediaTypes ? { mediaTypes: request.mediaTypes } : {}),
    })
  }

  private async chooseDisplayMedia(
    record: NativeWorkbenchSurfaceRecord,
    request: {
      videoRequested: boolean
      audioRequested: boolean
    },
    callback: (streams: Electron.Streams) => void,
  ): Promise<void> {
    if (record.disposed || record.owner.isDestroyed()) {
      callback({})
      return
    }
    try {
      if (!request.videoRequested) {
        callback(request.audioRequested ? { audio: 'loopback' } : {})
        return
      }
      const sources = await desktopCapturer.getSources({
        types: ['screen', 'window'],
        fetchWindowIcons: false,
        thumbnailSize: { width: 0, height: 0 },
      })
      if (record.disposed || record.owner.isDestroyed() || sources.length === 0) {
        callback({})
        return
      }
      const visibleSources = sources.slice(0, 12)
      const cancelId = visibleSources.length
      const choice = await dialog.showMessageBox(record.owner, {
        type: 'question',
        title: 'Share a screen or window',
        message: 'Choose what this temporary preview may capture.',
        detail: sources.length > visibleSources.length
          ? `Showing the first ${visibleSources.length} available sources.`
          : 'Access ends when this Workbench item closes.',
        buttons: [
          ...visibleSources.map(source => source.name.slice(0, 80) || 'Unnamed source'),
          'Cancel',
        ],
        defaultId: 0,
        cancelId,
        noLink: true,
      })
      const source = visibleSources[choice.response]
      if (!source || record.disposed) {
        callback({})
        return
      }
      callback({
        video: source,
        ...(request.audioRequested ? { audio: 'loopback' as const } : {}),
      })
    } catch {
      callback({})
    }
  }

  private async promptForBasicAuthentication(
    record: NativeWorkbenchSurfaceRecord,
    targetUrl: string,
    authInfo: Electron.AuthInfo,
    callback: (username?: string, password?: string) => void,
  ): Promise<void> {
    const target = this.httpUrl(targetUrl)
    if (!target || record.disposed || record.crashed || record.owner.isDestroyed()) {
      callback()
      return
    }
    const realm = authInfo.realm.slice(0, 512)
    const challengeKey = [
      authInfo.isProxy ? 'proxy' : 'origin',
      authInfo.host.toLowerCase(),
      String(authInfo.port),
      realm,
    ].join('\u0000')
    const attempts = (record.authenticationAttempts.get(challengeKey) ?? 0) + 1
    record.authenticationAttempts.set(challengeKey, attempts)
    if (attempts > NATIVE_WORKBENCH_MAX_AUTH_ATTEMPTS) {
      callback()
      this.emit(record, 'blocked-action', {
        action: 'authentication',
        targetUrl: target.origin,
        reason: 'authentication-attempt-limit',
      })
      return
    }

    const promptSession = session.fromPartition(
      `opensquilla-workbench-auth:${randomUUID()}`,
      { cache: false },
    )
    promptSession.setPermissionCheckHandler(() => false)
    promptSession.setPermissionRequestHandler((_contents, _permission, done) => done(false))
    promptSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, done) => {
      done({ cancel: !details.url.startsWith('data:text/html') })
    })
    const prompt = new BrowserWindow({
      parent: record.owner,
      modal: true,
      show: false,
      width: 440,
      height: 390,
      minWidth: 380,
      minHeight: 340,
      maximizable: false,
      minimizable: false,
      resizable: false,
      autoHideMenuBar: true,
      title: 'Sign in to preview',
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        webviewTag: false,
        devTools: false,
        spellcheck: false,
        session: promptSession,
      },
    })
    prompt.setMenu(null)
    prompt.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    prompt.webContents.on('will-navigate', event => event.preventDefault())
    let settled = false
    const finish = (username?: string, password?: string) => {
      if (settled) return
      settled = true
      callback(username, password)
    }
    const timeout = setTimeout(() => {
      this.cancelPendingAuthentication(record)
    }, this.options.authenticationTimeoutMs ?? NATIVE_WORKBENCH_AUTH_TIMEOUT_MS)
    timeout.unref()
    record.pendingAuthentication = {
      challengeKey,
      callback: finish,
      prompt,
      promptSession,
      timeout,
    }
    prompt.once('closed', () => {
      if (record.pendingAuthentication?.prompt === prompt) {
        this.cancelPendingAuthentication(record)
      }
    })

    try {
      await prompt.loadURL(
        `data:text/html;charset=utf-8,${encodeURIComponent(BASIC_AUTH_PROMPT_HTML)}`,
      )
      if (record.pendingAuthentication?.prompt !== prompt) return
      prompt.show()
      const result = await prompt.webContents.executeJavaScript(`(() => {
        const challenge = document.getElementById('challenge')
        challenge.textContent = ${JSON.stringify(
          `${authInfo.isProxy ? 'Proxy' : target.origin}`
          + `${realm ? ` — ${realm}` : ''}`,
        )}
        const form = document.getElementById('credentials')
        const username = document.getElementById('username')
        const password = document.getElementById('password')
        const cancel = document.getElementById('cancel')
        username.focus()
        return new Promise(resolve => {
          form.addEventListener('submit', event => {
            event.preventDefault()
            resolve({
              cancelled: false,
              username: String(username.value),
              password: String(password.value),
            })
          }, { once: true })
          cancel.addEventListener('click', () => resolve({ cancelled: true }), { once: true })
        })
      })()`) as {
        cancelled?: unknown
        username?: unknown
        password?: unknown
      }
      if (record.pendingAuthentication?.prompt !== prompt) return
      const username = typeof result?.username === 'string' ? result.username : ''
      const password = typeof result?.password === 'string' ? result.password : ''
      if (
        result?.cancelled === true
        || username.length > 1024
        || password.length > 4096
        || username.includes('\u0000')
        || password.includes('\u0000')
      ) {
        this.cancelPendingAuthentication(record)
        return
      }
      this.finishPendingAuthentication(record, username, password)
    } catch {
      this.cancelPendingAuthentication(record)
    }
  }

  private finishPendingAuthentication(
    record: NativeWorkbenchSurfaceRecord,
    username?: string,
    password?: string,
  ): void {
    const pending = record.pendingAuthentication
    if (!pending) return
    record.pendingAuthentication = null
    clearTimeout(pending.timeout)
    pending.callback(username, password)
    if (!pending.prompt.isDestroyed()) pending.prompt.destroy()
    void Promise.allSettled([
      pending.promptSession.clearStorageData(),
      pending.promptSession.clearCache(),
      pending.promptSession.clearAuthCache(),
    ])
  }

  private cancelPendingAuthentication(record: NativeWorkbenchSurfaceRecord): void {
    this.finishPendingAuthentication(record)
  }

  private rejectPendingPermissions(record: NativeWorkbenchSurfaceRecord): void {
    for (const pending of record.pendingPermissions.values()) {
      clearTimeout(pending.timeout)
      pending.callback(false)
    }
    record.pendingPermissions.clear()
  }

  private permissionGrantKey(origin: string, permission: string): string {
    return `${origin}\u0000${permission}`
  }

  private permissionRequestOrigin(value: string): string | null {
    try {
      const parsed = new URL(value)
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
      return parsed.origin
    } catch {
      return null
    }
  }

  private normalizedOrigin(value: string): string {
    return this.permissionRequestOrigin(value) ?? ''
  }

  private configureWebContents(record: NativeWorkbenchSurfaceRecord): void {
    const contents = record.view.webContents
    contents.setWindowOpenHandler(details => {
      if (record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V2) {
        if (
          !details.postBody
          && this.hasRecentTrustedGesture(record)
          && this.httpUrl(details.url)
        ) {
          void this.confirmPopup(record, details.url)
        }
        this.emit(record, 'blocked-action', {
          action: 'popup',
          targetUrl: details.url,
          reason: this.hasRecentTrustedGesture(record)
            ? 'host-confirmation-required'
            : 'user-gesture-required',
        })
      }
      return { action: 'deny' }
    })
    contents.on('will-navigate', (event, targetUrl) => {
      if (record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION) {
        // Programmatic loadURL is normally excluded from will-navigate, but keep
        // the initial exact document explicitly admissible for Electron changes.
        // Once that document commits, every renderer-initiated top navigation is
        // denied.
        if (!record.initialDocumentCommitted && targetUrl === record.documentUrl) return
        event.preventDefault()
        return
      }
      if (!this.v2TopLevelNavigationAllowed(record, targetUrl)) {
        event.preventDefault()
        this.reportPrivilegedGatewayBlock(record, targetUrl)
        if (
          this.hasRecentTrustedGesture(record)
          && this.externalProtocolUrl(targetUrl)
        ) {
          void this.confirmExternalProtocol(record, targetUrl)
        }
        this.emit(record, 'blocked-action', {
          action: 'navigation',
          targetUrl,
          reason: 'scheme-or-offline-policy',
        })
      } else {
        this.rejectPendingPermissions(record)
        this.cancelPendingAuthentication(record)
        record.authenticationAttempts.clear()
      }
    })
    contents.on('will-redirect', (event, targetUrl) => {
      if (
        record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION
        || !this.v2TopLevelNavigationAllowed(record, targetUrl)
      ) {
        event.preventDefault()
        if (record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V2) {
          this.reportPrivilegedGatewayBlock(record, targetUrl)
          this.emit(record, 'blocked-action', {
            action: 'redirect',
            targetUrl,
            reason: 'scheme-or-offline-policy',
          })
        }
      } else {
        this.rejectPendingPermissions(record)
        this.cancelPendingAuthentication(record)
        record.authenticationAttempts.clear()
      }
    })
    if (record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V2) {
      contents.on('will-attach-webview', event => event.preventDefault())
      contents.on('devtools-opened', () => {
        if (!contents.isDestroyed()) contents.closeDevTools()
      })
      contents.on(
        'select-client-certificate',
        (event, targetUrl, _certificateList, callback) => {
          event.preventDefault()
          // Electron otherwise selects the first matching certificate from
          // the operating-system store. A preview must never inherit that
          // durable host identity.
          ;(callback as unknown as (certificate?: Certificate) => void)()
          this.emit(record, 'blocked-action', {
            action: 'client-certificate',
            targetUrl: this.httpUrl(targetUrl)?.origin,
            reason: 'host-identity-unavailable',
          })
        },
      )
      contents.on('select-bluetooth-device', (event, _devices, callback) => {
        event.preventDefault()
        callback('')
        this.emit(record, 'blocked-action', {
          action: 'bluetooth',
          reason: 'unsupported-device-permission',
        })
      })
      contents.on(
        'login',
        (event, responseDetails, authInfo, callback) => {
          event.preventDefault()
          if (
            authInfo.scheme.toLowerCase() !== 'basic'
            || !this.httpUrl(responseDetails.url)
            || record.pendingAuthentication
          ) {
            callback()
            this.emit(record, 'blocked-action', {
              action: 'authentication',
              targetUrl: responseDetails.url,
              reason: record.pendingAuthentication
                ? 'authentication-already-pending'
                : 'unsupported-authentication',
            })
            return
          }
          void this.promptForBasicAuthentication(
            record,
            responseDetails.url,
            authInfo,
            callback,
          )
        },
      )
    }
    contents.on(
      'did-frame-navigate',
      (_event, targetUrl, httpResponseCode, _httpStatusText, isMainFrame) => {
        if (isMainFrame && targetUrl === record.documentUrl) {
          record.initialDocumentCommitted = true
        }
        if (isMainFrame && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V2) {
          this.rejectPendingPermissions(record)
          if (httpResponseCode === 410) this.emit(record, 'capability-expired')
          if (
            record.kind === 'artifact-preview'
            && httpResponseCode >= 400
            && httpResponseCode !== 410
          ) {
            this.failRecord(record, 'error', {
              message: httpResponseCode === 409
                ? 'Artifact preview integrity check failed or its bundle version is unsupported.'
                : httpResponseCode === 404
                  ? 'Artifact preview resource was not found.'
                  : `Artifact preview request failed (HTTP ${httpResponseCode}).`,
              reason: 'artifact-http-error',
            })
            return
          }
          this.emitNavigationState(record)
        }
      },
    )
    contents.on('before-input-event', (event, input) => {
      if (input.type === 'keyDown') record.lastTrustedGestureAt = Date.now()
      if (input.type === 'keyDown' && input.key === 'Escape') {
        event.preventDefault()
        this.emit(record, 'escape')
        return
      }
      const devToolsShortcut = record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
        && input.type === 'keyDown' && (
        input.key === 'F12'
        || (
          input.key.toLowerCase() === 'i'
          && input.shift
          && (input.control || input.meta)
        )
      )
      if (devToolsShortcut) {
        event.preventDefault()
        this.emit(record, 'blocked-action', {
          action: 'devtools',
          reason: 'privileged-host-capability',
        })
      }
    })
    contents.on('before-mouse-event', (_event, input) => {
      if (input.type === 'mouseDown') record.lastTrustedGestureAt = Date.now()
    })
    contents.on('did-start-loading', () => {
      this.emit(record, 'loading')
      this.emitNavigationState(record)
    })
    contents.on('did-stop-loading', () => this.emitNavigationState(record))
    contents.on('page-title-updated', () => this.emitNavigationState(record))
    contents.on('did-navigate-in-page', () => this.emitNavigationState(record))
    contents.on('did-finish-load', () => {
      record.initialDocumentCommitted = true
      record.authenticationAttempts.clear()
      this.emit(record, 'ready')
      this.emitNavigationState(record)
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
      this.failRecord(record, 'unresponsive', { reason: 'unresponsive' })
    })
  }

  private hasRecentTrustedGesture(record: NativeWorkbenchSurfaceRecord): boolean {
    return Date.now() - record.lastTrustedGestureAt <= NATIVE_WORKBENCH_USER_GESTURE_WINDOW_MS
  }

  private httpUrl(value: string): URL | null {
    try {
      const parsed = new URL(value)
      return (
        (parsed.protocol === 'http:' || parsed.protocol === 'https:')
        && !parsed.username
        && !parsed.password
      ) ? parsed : null
    } catch {
      return null
    }
  }

  private externalProtocolUrl(value: string): URL | null {
    if (value.length > 8192) return null
    try {
      const parsed = new URL(value)
      return NATIVE_WORKBENCH_EXTERNAL_PROTOCOLS.has(parsed.protocol) ? parsed : null
    } catch {
      return null
    }
  }

  private async confirmPopup(
    record: NativeWorkbenchSurfaceRecord,
    targetUrl: string,
  ): Promise<void> {
    const target = this.httpUrl(targetUrl)
    if (!target || record.disposed || record.owner.isDestroyed()) return
    const result = await dialog.showMessageBox(record.owner, {
      type: 'question',
      title: 'Open preview link',
      message: 'Where should this link open?',
      detail: target.origin,
      buttons: ['Current preview', 'System browser', 'Cancel'],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    })
    if (record.disposed || record.crashed) return
    if (result.response === 0 && this.v2TopLevelNavigationAllowed(record, target.href)) {
      this.rejectPendingPermissions(record)
      this.cancelPendingAuthentication(record)
      record.authenticationAttempts.clear()
      await record.view.webContents.loadURL(target.href).catch(() => undefined)
    } else if (result.response === 1) {
      await shell.openExternal(target.href, { activate: true }).catch(() => undefined)
    }
  }

  private async confirmExternalProtocol(
    record: NativeWorkbenchSurfaceRecord,
    targetUrl: string,
  ): Promise<void> {
    const target = this.externalProtocolUrl(targetUrl)
    if (!target || record.disposed || record.owner.isDestroyed()) return
    const result = await dialog.showMessageBox(record.owner, {
      type: 'question',
      title: 'Open an external application',
      message: `Allow this preview to open ${target.protocol.slice(0, -1)}?`,
      detail: 'This action leaves the isolated Workbench preview.',
      buttons: ['Open', 'Cancel'],
      defaultId: 1,
      cancelId: 1,
      noLink: true,
    })
    if (result.response === 0 && !record.disposed) {
      await shell.openExternal(target.href, { activate: true }).catch(() => undefined)
    }
  }

  private v2TopLevelNavigationAllowed(
    record: NativeWorkbenchSurfaceRecord,
    targetUrl: string,
  ): boolean {
    try {
      const target = new URL(targetUrl)
      if (target.protocol !== 'http:' && target.protocol !== 'https:') return false
      return (
        nativeWorkbenchV2NetworkUrlAllowed(
          target.href,
          record.mode,
          record.expectedOrigin ?? undefined,
        )
        && !this.isPrivilegedGatewayTarget(target.href)
      )
    } catch {
      return false
    }
  }

  private isPrivilegedGatewayTarget(value: string): boolean {
    const configured = this.options.getPrivilegedGatewayUrl?.()
    if (!configured) return false
    try {
      const target = new URL(value)
      const gateway = new URL(configured)
      if (
        !['http:', 'https:', 'ws:', 'wss:'].includes(target.protocol)
        || !['http:', 'https:'].includes(gateway.protocol)
      ) return false
      const targetProtocol = target.protocol === 'ws:'
        ? 'http:'
        : target.protocol === 'wss:'
          ? 'https:'
          : target.protocol
      const samePort = effectiveHttpPort(target) === effectiveHttpPort(gateway)
      if (
        samePort
        && targetProtocol === gateway.protocol
        && normalizedUrlHostname(target.hostname) === normalizedUrlHostname(gateway.hostname)
      ) return true
      return (
        samePort
        && isLoopbackUrlHostname(target.hostname)
        && isLoopbackUrlHostname(gateway.hostname)
      )
    } catch {
      return false
    }
  }

  private reportPrivilegedGatewayBlock(
    record: NativeWorkbenchSurfaceRecord,
    targetUrl: string,
  ): void {
    if (
      !this.isPrivilegedGatewayTarget(targetUrl)
      || record.privilegedOriginReported
    ) return
    record.privilegedOriginReported = true
    this.emit(record, 'blocked-action', {
      action: 'gateway',
      reason: 'privileged-origin-isolated',
    })
  }

  private emitNavigationState(record: NativeWorkbenchSurfaceRecord): void {
    if (
      record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V2
      || record.disposed
      || record.crashed
      || record.view.webContents.isDestroyed()
    ) return
    const contents = record.view.webContents
    this.emit(record, 'navigation-state', {
      url: contents.getURL(),
      title: contents.getTitle(),
      loading: contents.isLoading(),
      canGoBack: contents.navigationHistory.canGoBack(),
      canGoForward: contents.navigationHistory.canGoForward(),
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
      if (!record.view.webContents.isDestroyed()) {
        record.view.webContents.setAudioMuted(!visible)
      }
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
    // Snapshot before dispatching terminal events. A renderer event consumer
    // may synchronously request a replacement item; that new surface must not
    // be swept into the owner failure that preceded it.
    const ownedRecords = [...this.surfaces.values()].filter(record => record.owner === owner)
    for (const record of ownedRecords) {
      this.failRecord(record, 'crashed', { reason })
    }
  }

  private failRecord(
    record: NativeWorkbenchSurfaceRecord,
    type: 'error' | 'crashed' | 'unresponsive',
    detail: NonNullable<NativeWorkbenchSurfaceEvent['detail']>,
  ): boolean {
    if (record.disposed || record.crashed) return false
    record.crashed = true
    record.visibleRequested = false
    // Begin the complete teardown before calling renderer-owned event code.
    // destroyRecord removes the slot and marks the record disposed
    // synchronously, so callback re-entry cannot revive or replace a surface
    // while the failed renderer is still attached to the host window.
    void this.destroyRecord(record)
    this.dispatchEvent(record, type, detail)
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
    this.dispatchEvent(record, type, detail)
  }

  private dispatchEvent(
    record: NativeWorkbenchSurfaceRecord,
    type: NativeWorkbenchSurfaceEvent['type'],
    detail?: NativeWorkbenchSurfaceEvent['detail'],
  ): void {
    this.options.emit({
      version: record.version,
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
