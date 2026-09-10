import {
  app,
  BrowserWindow,
  desktopCapturer,
  dialog,
  MessageChannelMain,
  session,
  shell,
  type Certificate,
  type MessagePortMain,
  type Session,
  WebContentsView,
} from 'electron'
import { randomUUID } from 'node:crypto'
import { isIP } from 'node:net'
import { fileURLToPath } from 'node:url'
import {
  NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HEIGHT,
  NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH,
  parseNativeWorkbenchAnnotationGeometry,
  parseNativeWorkbenchAnnotationOverlayMessage,
  parseNativeWorkbenchAnnotationSelection,
  type NativeWorkbenchAnnotationCapabilities,
  type NativeWorkbenchAnnotationModeRequest,
  type NativeWorkbenchAnnotationOverlayCloseRequest,
  type NativeWorkbenchAnnotationOverlayShowRequest,
  type NativeWorkbenchAnnotationSelection,
  type NativeWorkbenchAnnotationSelectionCandidate,
} from './native-workbench-annotation-contract.js'
import {
  clampNativeWorkbenchSurfaceRect,
  NATIVE_WORKBENCH_ARTIFACT_SCHEME,
  NATIVE_WORKBENCH_MAX_SURFACES,
  NATIVE_WORKBENCH_PROTOCOL_VERSION,
  NATIVE_WORKBENCH_PROTOCOL_VERSION_V3,
  NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchDownloadAllowed,
  nativeWorkbenchMissingResourceIsLocal,
  nativeWorkbenchNetworkUrlAllowed,
  parseNativeWorkbenchNavigationUrl,
  nativeWorkbenchV2NetworkUrlAllowed,
  type NativeWorkbenchCreateRequest,
  type NativeWorkbenchNavigationRequest,
  type NativeWorkbenchPermissionResponse,
  type NativeWorkbenchPreviewMode,
  type NativeWorkbenchSurfaceEvent,
  type NativeWorkbenchSurfaceRect,
  type NativeWorkbenchSurfaceRectRequest,
} from './native-workbench-surface-contract.js'
import { DesktopBrowserError, type DesktopBrowserRequest } from './desktop-browser.js'
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
  surfaceInstanceId: string
  version: NativeWorkbenchCreateRequest['version']
  kind: NativeWorkbenchCreateRequest['kind']
  mode: NativeWorkbenchPreviewMode
  scopeId: string
  handle: string | null
  documentUrl: string
  expectedOrigin: string | null
  targetRef: string
  revisionTimer: NodeJS.Timeout | null
  revisionRequest: AbortController | null
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
  annotationCandidate: NativeWorkbenchAnnotationCandidate | null
  annotationDocumentGeneration: number
  annotationFallbackActive: boolean
  annotationFocusTimer: NodeJS.Timeout | null
  annotationPickerActive: boolean
  annotationPickerEpoch: number
  /** True only after the current preview navigation reaches did-finish-load. */
  browserDocumentReady: boolean
  /** Set by CDP Runtime.exceptionThrown until the next successful navigation. */
  browserRuntimeException: boolean
  /** WebRTC guard for an offline preview. */
  offlineRealmGuardInstalled: boolean
  /** CDP id for the offline WebRTC guard. */
  offlineRealmGuardScriptId: string | null
  browserObjectGroup: string | null
  browserAnchors: Map<string, NativeWorkbenchBrowserAnchor>
  browserAnchorGeneration: number
  cdpQueue: Promise<void>
  cdpReady: boolean
  debuggerExpectedDetach: boolean
}

interface NativeWorkbenchBrowserAnchor {
  objectId: string
  documentGeneration: number
}

interface NativeWorkbenchAnnotationCandidate {
  selection: NativeWorkbenchAnnotationSelection
  viewportWidth: number
  viewportHeight: number
  documentGeneration: number
  objectGroup: string
  objectId: string
  geometryTimer: NodeJS.Timeout | null
  geometryRefreshPending: boolean
}

interface NativeWorkbenchAnnotationOverlayBinding {
  annotationId: string
  port: MessagePortMain
  record: NativeWorkbenchSurfaceRecord
  selectionId: string
}

interface NativeWorkbenchAnnotationOverlayRecord {
  owner: BrowserWindow
  previewSession: Session
  ready: Promise<void>
  view: WebContentsView
  binding: NativeWorkbenchAnnotationOverlayBinding | null
  disposed: boolean
  focusTimer: NodeJS.Timeout | null
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
const NATIVE_WORKBENCH_MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
const NATIVE_WORKBENCH_CDP_TIMEOUT_MS = 5_000
const NATIVE_WORKBENCH_ANNOTATION_OVERLAY_CHANNEL =
  'opensquilla:workbench-annotation-overlay:init'
const NATIVE_WORKBENCH_ANNOTATION_OVERLAY_DEFAULT_COPY = Object.freeze({
  targetLabel: 'Selected area',
  contextLabel: 'Current selection',
  bodyLabel: 'Page annotation',
  placeholder: 'Describe what you want to change…',
  newlineHint: 'Shift + Enter for a new line',
  cancelLabel: 'Cancel',
  submitLabel: 'Add annotation',
  emptyBodyMessage: 'Describe the requested change.',
})
const NATIVE_WORKBENCH_ANNOTATION_OVERLAY_PRELOAD = fileURLToPath(new URL(
  './native-workbench-annotation-overlay-preload.cjs',
  import.meta.url,
))
const NATIVE_WORKBENCH_EXTERNAL_PROTOCOLS = new Set(['mailto:', 'sms:', 'tel:'])
const NATIVE_WORKBENCH_OFFLINE_WEBRTC_CSP = "webrtc 'block'"
const NATIVE_WORKBENCH_OFFLINE_REALM_GUARD = `(() => {
  for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'mozRTCPeerConnection', 'RTCIceGatherer', 'RTCIceTransport', 'RTCDtlsTransport', 'RTCSctpTransport', 'RTCQuicTransport']) {
    try { Object.defineProperty(globalThis, name, { configurable: false, value: undefined, writable: false }) } catch {}
  }
})()`
const NATIVE_WORKBENCH_PROMPTABLE_PERMISSIONS = new Set([
  'clipboard-read',
  'clipboard-sanitized-write',
  'display-capture',
  'geolocation',
  'media',
])

const NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG = Object.freeze({
  showInfo: false,
  showAccessibilityInfo: false,
  showRulers: false,
  showExtensionLines: false,
  contentColor: { r: 25, g: 118, b: 255, a: 0.16 },
  paddingColor: { r: 25, g: 118, b: 255, a: 0.12 },
  borderColor: { r: 25, g: 118, b: 255, a: 0.95 },
  marginColor: { r: 25, g: 118, b: 255, a: 0.08 },
})

// Inspect the selected node in an isolated world; page content remains untrusted context.
const NATIVE_WORKBENCH_ANNOTATION_INSPECT_FUNCTION = `function () {
  const selected = this
  if (window.top !== window || !(selected instanceof Element) || !selected.isConnected || selected.ownerDocument !== document || selected.getRootNode() !== document) return { ok: false }
  const segments = []
  for (let node = selected; node; node = node.parentElement) {
    if (segments.length >= 128) return { ok: false }
    let index = 1
    for (let sibling=node.previousElementSibling; sibling; sibling=sibling.previousElementSibling) if (sibling.localName === node.localName) index++
    segments.unshift(CSS.escape(node.localName) + ':nth-of-type(' + index + ')')
  }
  const locatorHint = segments.join(' > ')
  const rect = selected.getBoundingClientRect()
  const viewport = window.visualViewport
  return { ok: true, tagName: selected.localName, elementPath: locatorHint, locatorHint,
    selectionText: (selected.innerText || selected.textContent || '').slice(0,4096),
    rect: {x:rect.x,y:rect.y,width:rect.width,height:rect.height},
    viewportWidth: viewport ? viewport.width : window.innerWidth,
    viewportHeight: viewport ? viewport.height : window.innerHeight }
}`

const NATIVE_WORKBENCH_ANNOTATION_GEOMETRY_FUNCTION = `function () {
  const selected = this
  if (
    window.top !== window
    || !(selected instanceof Element)
    || !selected.isConnected
    || selected.ownerDocument !== document
    || selected.getRootNode() !== document
  ) return { ok: false, reason: 'unsupported-node' }
  const rect = selected.getBoundingClientRect()
  const viewport = window.visualViewport
  return {
    ok: true,
    rect: {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    },
    viewportWidth: viewport ? viewport.width : window.innerWidth,
    viewportHeight: viewport ? viewport.height : window.innerHeight,
  }
}`

const NATIVE_WORKBENCH_ANNOTATION_SCROLL_FUNCTION = `function () {
  const selected = this
  if (
    window.top !== window
    || !(selected instanceof Element)
    || !selected.isConnected
    || selected.ownerDocument !== document
    || selected.getRootNode() !== document
  ) return { ok: false, reason: 'unsupported-node' }
  selected.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'center' })
  const rect = selected.getBoundingClientRect()
  const viewport = window.visualViewport
  return {
    ok: true,
    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
    viewportWidth: viewport ? viewport.width : window.innerWidth,
    viewportHeight: viewport ? viewport.height : window.innerHeight,
  }
}`

const NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HTML = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; connect-src 'none'; font-src 'none'; frame-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; script-src 'none'; style-src 'unsafe-inline'; form-action 'none'">
  <meta name="color-scheme" content="light dark">
  <title>Artifact annotation</title>
  <style>
    :root {
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
        "Helvetica Neue", "Segoe UI", "PingFang SC", "Hiragino Sans",
        "Microsoft YaHei", "Yu Gothic", sans-serif;
      --bg-surface: #FFFFFF;
      --bg-surface-2: #F0F0F2;
      --bg-hover: #EAEAED;
      --text: #1D1D1F;
      --text-muted: #5F6066;
      --text-dim: #85868D;
      --border: #E6E6E9;
      --border-strong: #D5D5DA;
      --accent: #BA4D0F;
      --accent-hover: #A5440C;
      --accent-foreground: #FFFFFF;
      --focus-ring: rgba(186, 77, 15, 0.34);
      --shadow: 0 8px 30px -16px rgba(16, 20, 26, 0.22);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg-surface: #202022;
        --bg-surface-2: #28282B;
        --bg-hover: #353539;
        --text: #F5F5F7;
        --text-muted: #B0B0B6;
        --text-dim: #87878E;
        --border: #303034;
        --border-strong: #444448;
        --accent: #F26A1B;
        --accent-hover: #FF7A2E;
        --accent-foreground: #160B02;
        --focus-ring: rgba(242, 106, 27, 0.4);
        --shadow: 0 6px 16px -4px rgba(0, 0, 0, 0.5);
      }
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; }
    body {
      margin: 0;
      overflow: hidden;
      background: var(--bg-surface);
      color: var(--text);
      font-size: 13px;
      line-height: 1.4;
    }
    .annotation-card {
      position: relative;
      display: grid;
      grid-template-rows: 22px minmax(0, 1fr) 32px;
      gap: 6px;
      width: 100%;
      height: 100%;
      padding: 10px 10px 9px 13px;
      border: 1px solid var(--border-strong);
      border-radius: 12px;
      background: var(--bg-surface);
      box-shadow: var(--shadow);
    }
    .annotation-card::before {
      position: absolute;
      inset: 10px auto 10px 0;
      width: 3px;
      border-radius: 0 999px 999px 0;
      background: var(--accent);
      content: "";
    }
    .annotation-header {
      display: flex;
      min-width: 0;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .annotation-title {
      display: flex;
      min-width: 0;
      align-items: center;
      gap: 6px;
      margin: 0;
      color: var(--text);
      font-size: 13px;
      font-weight: 500;
      line-height: 22px;
    }
    .annotation-target {
      max-width: 148px;
      overflow: hidden;
      padding: 2px 7px;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: var(--bg-surface-2);
      color: var(--text-muted);
      font-family: "SFMono-Regular", ui-monospace, "Cascadia Code", Menlo, monospace;
      font-size: 11px;
      font-weight: 500;
      line-height: 17px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .annotation-context {
      overflow: hidden;
      color: var(--text-dim);
      font-size: 11px;
      font-weight: 400;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    textarea {
      width: 100%;
      height: 100%;
      resize: none;
      padding: 8px 9px;
      border: 1px solid var(--border-strong);
      border-radius: 8px;
      outline: none;
      background: var(--bg-surface-2);
      color: var(--text);
      caret-color: var(--accent);
      font: inherit;
      line-height: 1.45;
      transition: border-color 120ms cubic-bezier(.2, 0, 0, 1),
        box-shadow 120ms cubic-bezier(.2, 0, 0, 1),
        background 120ms cubic-bezier(.2, 0, 0, 1);
    }
    textarea::placeholder { color: var(--text-dim); opacity: 1; }
    textarea:hover { border-color: var(--border-strong); background: var(--bg-hover); }
    textarea:focus-visible {
      border-color: var(--accent);
      background: var(--bg-surface);
      box-shadow: 0 0 0 3px var(--focus-ring);
    }
    footer {
      display: flex;
      min-width: 0;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
    }
    .annotation-shortcut-hint {
      min-width: 0;
      overflow: hidden;
      margin: 0 auto 0 0;
      color: var(--text-dim);
      font-size: 10px;
      line-height: 1;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    button {
      flex: 0 0 auto;
      min-width: 56px;
      max-width: 124px;
      height: 32px;
      overflow: hidden;
      padding: 0 11px;
      border: 1px solid transparent;
      border-radius: 8px;
      outline: none;
      font: inherit;
      font-weight: 600;
      line-height: 1;
      text-overflow: ellipsis;
      white-space: nowrap;
      cursor: pointer;
      transition: background 120ms cubic-bezier(.2, 0, 0, 1),
        border-color 120ms cubic-bezier(.2, 0, 0, 1),
        box-shadow 120ms cubic-bezier(.2, 0, 0, 1);
    }
    button:focus-visible { box-shadow: 0 0 0 3px var(--focus-ring); }
    #annotation-cancel {
      max-width: 88px;
      border-color: var(--border);
      background: transparent;
      color: var(--text-muted);
    }
    #annotation-cancel:hover { border-color: var(--border-strong); background: var(--bg-hover); color: var(--text); }
    button[type="submit"] { background: var(--accent); color: var(--accent-foreground); }
    button[type="submit"]:hover { background: var(--accent-hover); }
    button[type="submit"]:disabled { cursor: default; opacity: 0.5; }
    @media (prefers-reduced-motion: reduce) {
      textarea, button { transition: none; }
    }
  </style>
</head>
<body>
  <form
    id="annotation-form"
    class="annotation-card"
    role="dialog"
    aria-modal="false"
    aria-labelledby="annotation-title"
  >
    <header class="annotation-header">
      <h1 id="annotation-title" class="annotation-title">
        <span id="annotation-target" class="annotation-target" aria-label="Selected area">Selected area</span>
        <span id="annotation-context" class="annotation-context">Current selection</span>
      </h1>
    </header>
    <textarea id="annotation-body" maxlength="16384" required aria-label="Page annotation" placeholder="Describe what you want to change…"></textarea>
    <footer>
      <span id="annotation-newline-hint" class="annotation-shortcut-hint"></span>
      <button id="annotation-cancel" type="button">Cancel</button>
      <button id="annotation-submit" type="submit">Add annotation</button>
    </footer>
  </form>
</body>
</html>`

export interface NativeWorkbenchSurfaceResult {
  ok: boolean
  code?: string
  retryable?: boolean
  message?: string
  surfaceInstanceId?: string
}

export interface NativeWorkbenchAnnotationLifecycleDiagnostic {
  phase: 'close-start' | 'arm-start' | 'armed' | 'cancelled' | 'failed' | 'selection-emitted' | 'selection-rejected'
  outcome: 'started' | 'succeeded' | 'cancelled' | 'failed'
  reason: NativeWorkbenchAnnotationLifecycleReason
  elapsedMs: number
  pickerEpoch: number
  generation: number
  webContentsId: number
  current: boolean
  visible: boolean
  cdpReady: boolean
}

type NativeWorkbenchAnnotationLifecycleReason =
  | 'requested'
  | 'completed'
  | 'superseded'
  | 'reset-failed'
  | 'activate-failed'
  | 'preview-hide-failed'
  | 'picker-cancelled'
  | 'selection-stale'
  | 'selection-rejected'
  | 'debugger-detached'
  | 'surface-reloaded'
  | 'surface-navigation'
  | 'surface-redirect'
  | 'surface-hidden'
  | 'surface-closed'
  | 'surface-failed'
  | 'lifecycle-cancelled'

const NATIVE_WORKBENCH_ANNOTATION_LIFECYCLE_REASONS = new Set<string>([
  'requested',
  'completed',
  'superseded',
  'reset-failed',
  'activate-failed',
  'preview-hide-failed',
  'picker-cancelled',
  'selection-stale',
  'selection-rejected',
  'debugger-detached',
  'surface-reloaded',
  'surface-navigation',
  'surface-redirect',
  'surface-hidden',
  'surface-closed',
  'surface-failed',
  'lifecycle-cancelled',
])

function nativeWorkbenchAnnotationLifecycleReason(
  reason: string,
): NativeWorkbenchAnnotationLifecycleReason {
  return NATIVE_WORKBENCH_ANNOTATION_LIFECYCLE_REASONS.has(reason)
    ? reason as NativeWorkbenchAnnotationLifecycleReason
    : 'lifecycle-cancelled'
}

export interface NativeWorkbenchSurfaceManagerOptions {
  annotationAudit?(entry: NativeWorkbenchAnnotationLifecycleDiagnostic): void
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

function boundedAnnotationCdpError(error: unknown): string {
  const raw = errorMessage(error).replace(/[\r\n\t]+/g, ' ').trim()
  const protocolPayload = raw.match(/\{\s*"code"\s*:\s*-?\d+[\s\S]{0,512}\}$/)?.[0]
  let code: number | null = null
  let detail = raw
  if (protocolPayload) {
    try {
      const parsed = JSON.parse(protocolPayload) as { code?: unknown; message?: unknown }
      if (Number.isSafeInteger(parsed.code)) code = parsed.code as number
      if (typeof parsed.message === 'string') detail = parsed.message
    } catch {}
  }
  detail = detail
    .replace(/(?:https?|file):\/\/\S+/gi, '[redacted-url]')
    .replace(/(?:\/[^/\s:]+){2,}/g, '[redacted-path]')
    .replace(/[^\x20-\x7e]/g, '?')
    .slice(0, 160)
  if (!detail) detail = 'Unknown inspector protocol error.'
  return code === null ? detail : `CDP ${code}: ${detail}`
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
  private readonly annotationOverlays = new Map<BrowserWindow, NativeWorkbenchAnnotationOverlayRecord>()
  private readonly hookedWindows = new WeakSet<BrowserWindow>()
  private readonly unresponsiveWindows = new WeakSet<BrowserWindow>()
  private activeSurfaceId: string | null = null

  constructor(private readonly options: NativeWorkbenchSurfaceManagerOptions) {}

  async createSurface(
    request: NativeWorkbenchCreateRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const pending = this.surfaces.get(request.surfaceId)
    if (pending) {
      // Fence an in-flight atomic picker arm before waiting behind any queued
      // surface work. The eventual destroy still performs authoritative CDP
      // cleanup; this synchronous epoch bump only prevents a late arm result
      // from becoming current during replacement.
      pending.annotationPickerEpoch += 1
      this.cancelPendingAuthentication(pending)
    }
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
      surfaceInstanceId: randomUUID(),
      version: request.version,
      kind: request.kind,
      mode,
      scopeId: request.payload.scopeId,
      handle,
      documentUrl,
      expectedOrigin,
      targetRef: `page-${randomUUID()}`,
      revisionTimer: null,
      revisionRequest: null,
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
      annotationCandidate: null,
      annotationDocumentGeneration: 0,
      annotationFallbackActive: false,
      annotationFocusTimer: null,
      annotationPickerActive: false,
      annotationPickerEpoch: 0,
      browserDocumentReady: false,
      browserRuntimeException: false,
      offlineRealmGuardInstalled: false,
      offlineRealmGuardScriptId: null,
      browserObjectGroup: null,
      browserAnchors: new Map(),
      browserAnchorGeneration: 0,
      cdpQueue: Promise.resolve(),
      cdpReady: false,
      debuggerExpectedDetach: false,
    }
    record.view.setBounds({ x: 0, y: 0, width: 960, height: 720 })
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
      if (request.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION) {
        try {
          await this.initializeAnnotationCdp(record)
        } catch (error) {
          // DOM annotations are an additive capability. If Overlay or the
          // isolated-world inspector is unavailable, keep the ordinary
          // preview usable and advertise the annotation capability as off.
          record.cdpReady = false
          this.emit(record, 'blocked-action', {
            action: 'annotation-picker',
            reason: errorMessage(error).slice(0, 200),
          })
        }
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
      if (record.kind === 'artifact-preview') await this.watchWorkingPreview(record)
      if (this.surfaces.get(record.id) !== record) return { ok: false, message: 'The browser page was closed.' }
      return { ok: true, surfaceInstanceId: record.surfaceInstanceId }
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

  async getArtifactAnnotationCapabilities(): Promise<NativeWorkbenchAnnotationCapabilities> {
    const record = this.activeAnnotationRecord()
    const annotationVersion = record?.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      ? NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      : NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
    if (!record) {
      return {
        version: NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
        available: false,
        picker: false,
        trustedOverlay: false,
        overlayCopyVersion: 1,
        atomicCloseRearm: true,
        reason: 'No active browser page is available.',
      }
    }
    if (!record.cdpReady || !record.view.webContents.debugger.isAttached()) {
      return {
        version: annotationVersion,
        available: false,
        picker: false,
        trustedOverlay: false,
        overlayCopyVersion: 1,
        atomicCloseRearm: true,
        reason: 'The isolated DOM inspector is unavailable.',
      }
    }
    try {
      const overlay = await this.annotationOverlayForOwner(record.owner)
      await overlay.ready
      if (
        overlay.disposed
        || overlay.view.webContents.isDestroyed()
        || !this.isActiveAnnotationRecord(record)
      ) throw new Error('The trusted annotation editor is unavailable.')
    } catch {
      return {
        version: annotationVersion,
        available: false,
        picker: true,
        trustedOverlay: false,
        overlayCopyVersion: 1,
        atomicCloseRearm: true,
        reason: 'The trusted annotation editor is unavailable.',
      }
    }
    return {
      version: annotationVersion,
      available: true,
      picker: true,
      trustedOverlay: true,
      overlayCopyVersion: 1,
      atomicCloseRearm: true,
    }
  }

  private annotationPickerTransitionIsCurrent(
    record: NativeWorkbenchSurfaceRecord,
    pickerEpoch: number,
    documentGeneration: number,
    binding: NativeWorkbenchAnnotationOverlayBinding | null,
  ): boolean {
    if (
      record.annotationPickerEpoch !== pickerEpoch
      || record.annotationDocumentGeneration !== documentGeneration
      || this.surfaces.get(record.id) !== record
      || this.activeSurfaceId !== record.id
      || record.kind === 'artifact-html'
      || record.disposed
      || record.crashed
      || !record.visibleRequested
      || !record.rect
      || record.owner.isDestroyed()
      || !this.ownerCanShowSurfaces(record.owner)
      || record.view.webContents.isDestroyed()
      || !record.cdpReady
      || !record.view.webContents.debugger.isAttached()
    ) return false
    return binding === null || this.activeAnnotationOverlayBinding(record) === binding
  }

  private auditAnnotationPicker(
    record: NativeWorkbenchSurfaceRecord,
    phase: NativeWorkbenchAnnotationLifecycleDiagnostic['phase'],
    outcome: NativeWorkbenchAnnotationLifecycleDiagnostic['outcome'],
    reason: string,
    startedAt = Date.now(),
  ): void {
    try {
      this.options.annotationAudit?.({
        phase,
        outcome,
        reason: nativeWorkbenchAnnotationLifecycleReason(reason),
        elapsedMs: Math.max(0, Date.now() - startedAt),
        pickerEpoch: record.annotationPickerEpoch,
        generation: record.annotationDocumentGeneration,
        webContentsId: record.view.webContents.id,
        current: this.surfaces.get(record.id) === record && !record.disposed && !record.crashed,
        visible: record.view.getVisible(),
        cdpReady: record.cdpReady && record.view.webContents.debugger.isAttached(),
      })
    } catch {
      // Diagnostics must never change picker lifecycle semantics.
    }
  }

  private async armAnnotationPicker(
    record: NativeWorkbenchSurfaceRecord,
    pickerEpoch: number,
    documentGeneration: number,
    binding: NativeWorkbenchAnnotationOverlayBinding | null,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const startedAt = Date.now()
    this.auditAnnotationPicker(record, 'arm-start', 'started', 'requested', startedAt)
    record.annotationPickerActive = false
    const resetFailure = await this.clearAnnotationInspectState(record, true)
    if (!this.annotationPickerTransitionIsCurrent(
      record,
      pickerEpoch,
      documentGeneration,
      binding,
    )) {
      this.auditAnnotationPicker(record, 'cancelled', 'cancelled', 'superseded', startedAt)
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
        message: 'The annotation picker was cancelled before it became active.',
      }
    }
    if (resetFailure) {
      this.auditAnnotationPicker(record, 'failed', 'failed', 'reset-failed', startedAt)
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
        message: resetFailure,
      }
    }
    record.annotationPickerActive = true
    try {
      await this.cdpCommand(record, 'Overlay.setInspectMode', {
        mode: 'searchForNode',
        highlightConfig: NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG,
      })
      const transitionCurrent = this.annotationPickerTransitionIsCurrent(
        record,
        pickerEpoch,
        documentGeneration,
        binding,
      )
      if (
        !record.annotationPickerActive
        || !transitionCurrent
      ) {
        // A postcondition failure on this exact epoch means Chromium may have
        // accepted searchForNode while the manager cannot advertise it as
        // armed. Roll that inspect mode back. A superseded epoch must not run
        // this cleanup because it could disable the replacement picker.
        if (transitionCurrent && record.annotationPickerEpoch === pickerEpoch) {
          await this.clearAnnotationInspectState(record, true)
        }
        this.auditAnnotationPicker(record, 'cancelled', 'cancelled', 'superseded', startedAt)
        return {
          ok: false,
          code: 'ANNOTATION_UNAVAILABLE',
          retryable: true,
          message: 'The annotation picker was cancelled before it became active.',
        }
      }
      this.auditAnnotationPicker(record, 'armed', 'succeeded', 'completed', startedAt)
      return { ok: true }
    } catch (error) {
      if (record.annotationPickerEpoch === pickerEpoch) {
        record.annotationPickerActive = false
      }
      const cleanupFailure = record.annotationPickerEpoch === pickerEpoch
        ? await this.clearAnnotationInspectState(record, true)
        : null
      this.auditAnnotationPicker(record, 'failed', 'failed', 'activate-failed', startedAt)
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
        message: cleanupFailure
          ? `${errorMessage(error)} ${cleanupFailure}`
          : errorMessage(error),
      }
    }
  }

  async setArtifactAnnotationMode(
    request: NativeWorkbenchAnnotationModeRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    // Enabling is only valid for the active, visible preview. Disabling must
    // also accept the exact live v3/v4 surface while its trusted annotation
    // overlay is visible: presenting that overlay intentionally hides the
    // preview, so the stricter active-record predicate cannot be used to
    // acknowledge Stop and clean up the binding.
    const record = request.enabled
      ? this.annotationRecordForUiRequest(request.surfaceId)
      : this.annotationRecordForCleanupRequest(request.surfaceId)
    if (!record) {
      return {
        ok: false,
        // The renderer may still hold a scoped capability for a surface that
        // Desktop has already replaced. Give it a stable, retryable signal so
        // it can rebuild the preview once instead of surfacing IPC details.
        code: 'PREVIEW_CAPABILITY_EXPIRED',
        retryable: true,
        message: 'Open the browser page before annotating it.',
      }
    }
    if (!request.enabled) {
      const cleanupFailure = await this.cancelAnnotationInteraction(
        record,
        'picker-cancelled',
        true,
      )
      if (cleanupFailure) {
        return {
          ok: false,
          code: 'ANNOTATION_BUSY',
          retryable: true,
          message: cleanupFailure,
        }
      }
      return { ok: true }
    }
    if (!record.cdpReady || !record.view.webContents.debugger.isAttached()) {
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
        message: 'The isolated DOM inspector is unavailable.',
      }
    }
    if (this.activeAnnotationOverlayBinding(record)) {
      return {
        ok: false,
        code: 'ANNOTATION_BUSY',
        retryable: true,
        message: 'Finish the current annotation before choosing another element.',
      }
    }
    this.clearAnnotationCandidate(record)
    // Chromium normally exits inspect mode before dispatching
    // Overlay.inspectNodeRequested, but that transition is not a reliable
    // re-arm boundary on Windows. A later searchForNode command can resolve
    // successfully while ordinary page clicks still pass through to the
    // document. Make every enable an idempotent clean-arm transaction so the
    // UI never advertises an active picker that Chromium did not install.
    const pickerEpoch = ++record.annotationPickerEpoch
    return await this.armAnnotationPicker(
      record,
      pickerEpoch,
      record.annotationDocumentGeneration,
      null,
    )
  }

  async showArtifactAnnotationOverlay(
    request: NativeWorkbenchAnnotationOverlayShowRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const record = this.annotationRecordForUiRequest(request.surfaceId)
    const candidate = record?.annotationCandidate
    if (
      !record
      || !candidate
      || candidate.selection.selectionId !== request.selectionId
      || candidate.documentGeneration !== record.annotationDocumentGeneration
    ) {
      if (record) {
        this.clearAnnotationCandidate(record)
        this.failAnnotationOverlay(record, request.annotationId, 'selection-stale')
      }
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
        message: 'The selected preview element is stale or unavailable.',
      }
    }
    try {
      await this.refreshAnnotationCandidateIntegrity(record, candidate)
    } catch (error) {
      this.clearAnnotationCandidate(record)
      this.failAnnotationOverlay(record, request.annotationId, 'selection-stale')
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
        message: errorMessage(error),
      }
    }
    try {
      const overlay = await this.annotationOverlayForOwner(record.owner)
      await overlay.ready
      if (!this.isActiveAnnotationRecord(record) || record.annotationCandidate !== candidate) {
        throw new Error('The selected preview element changed before the editor opened.')
      }
      this.closeAnnotationOverlayBinding(overlay, false)
      const channel = new MessageChannelMain()
      const binding: NativeWorkbenchAnnotationOverlayBinding = {
        annotationId: request.annotationId,
        port: channel.port1,
        record,
        selectionId: request.selectionId,
      }
      overlay.binding = binding
      channel.port1.on('message', event => {
        this.handleAnnotationOverlayMessage(overlay, binding, event.data)
      })
      channel.port1.on('close', () => {
        if (overlay.binding === binding) {
          this.failAnnotationOverlay(record, request.annotationId, 'trusted-overlay-channel-closed')
        }
      })
      channel.port1.start()
      overlay.view.webContents.postMessage(
        NATIVE_WORKBENCH_ANNOTATION_OVERLAY_CHANNEL,
        {
          version: 1,
          initialBody: request.initialBody,
          copy: request.copy || NATIVE_WORKBENCH_ANNOTATION_OVERLAY_DEFAULT_COPY,
        },
        [channel.port2],
      )
      const bounds = this.annotationOverlayBounds(record, candidate)
      this.presentAnnotationOverlay(
        overlay,
        bounds,
        this.ownerCanShowSurfaces(record.owner),
        false,
      )
      this.focusAnnotationOverlay(overlay)
      record.annotationFallbackActive = false
      this.startAnnotationGeometryWatcher(record, candidate)
      return { ok: true }
    } catch (error) {
      // Recreate only the trusted editor view on the caller's single bounded
      // replay. Keep the opaque selection bound to the active preview so a
      // transient renderer failure does not force the user to select again.
      const failedOverlay = this.annotationOverlays.get(record.owner)
      if (record.annotationCandidate) {
        this.stopAnnotationGeometryWatcher(record.annotationCandidate)
      }
      if (failedOverlay) await this.disposeAnnotationOverlay(failedOverlay)
      record.annotationFallbackActive = false
      this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(record.owner))
      return {
        ok: false,
        code: 'PREVIEW_RENDERER_FAILED',
        retryable: true,
        message: errorMessage(error),
      }
    }
  }

  async closeArtifactAnnotationOverlay(
    request: NativeWorkbenchAnnotationOverlayCloseRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const record = this.surfaces.get(request.surfaceId)
    if (!record || record.disposed) {
      return {
        ok: false,
        code: 'PREVIEW_CAPABILITY_EXPIRED',
        retryable: true,
        message: 'The native Workbench surface no longer exists.',
      }
    }
    const overlay = this.annotationOverlays.get(record.owner)
    const binding = overlay?.binding
    if (
      request.annotationId
      && binding
      && binding.annotationId !== request.annotationId
    ) {
      return {
        ok: false,
        code: 'ANNOTATION_BUSY',
        retryable: true,
        message: 'The trusted annotation editor changed.',
      }
    }
    if (request.rearm === true) {
      const candidate = record.annotationCandidate
      if (
        !overlay
        || !binding
        || !candidate
        || binding.record !== record
        || candidate.documentGeneration !== record.annotationDocumentGeneration
        || !record.cdpReady
        || !record.view.webContents.debugger.isAttached()
        || this.activeSurfaceId !== record.id
        || !record.visibleRequested
        || !record.rect
      ) {
        return {
          ok: false,
          code: 'ANNOTATION_UNAVAILABLE',
          retryable: true,
          message: 'The trusted annotation editor cannot rearm the picker.',
        }
      }
      const startedAt = Date.now()
      const documentGeneration = record.annotationDocumentGeneration
      const pickerEpoch = ++record.annotationPickerEpoch
      this.auditAnnotationPicker(record, 'close-start', 'started', 'requested', startedAt)
      this.stopAnnotationGeometryWatcher(candidate)
      try {
        record.view.webContents.setAudioMuted(true)
        record.view.setVisible(false)
        overlay.view.setVisible(this.ownerCanShowSurfaces(record.owner))
      } catch {
        this.startAnnotationGeometryWatcher(record, candidate)
        this.auditAnnotationPicker(record, 'failed', 'failed', 'preview-hide-failed', startedAt)
        return {
          ok: false,
          code: 'ANNOTATION_UNAVAILABLE',
          retryable: true,
          message: 'The preview could not enter the annotation handoff state.',
        }
      }
      const armed = await this.armAnnotationPicker(
        record,
        pickerEpoch,
        documentGeneration,
        binding,
      )
      if (!armed.ok) {
        if (
          this.surfaces.get(record.id) === record
          && !record.disposed
          && !record.crashed
          && this.activeAnnotationOverlayBinding(record) === binding
          && record.annotationCandidate === candidate
        ) {
          this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(record.owner))
          this.startAnnotationGeometryWatcher(record, candidate)
        }
        return armed
      }
      if (!this.annotationPickerTransitionIsCurrent(
        record,
        pickerEpoch,
        documentGeneration,
        binding,
      )) {
        this.auditAnnotationPicker(record, 'cancelled', 'cancelled', 'superseded', startedAt)
        return {
          ok: false,
          code: 'ANNOTATION_UNAVAILABLE',
          retryable: true,
          message: 'The annotation picker handoff was superseded.',
        }
      }
      this.closeAnnotationOverlayBinding(overlay, false)
      record.annotationFallbackActive = false
      this.clearAnnotationCandidate(record)
      this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(record.owner))
      return { ok: true }
    }
    if (overlay) this.closeAnnotationOverlayBinding(overlay, false)
    record.annotationFallbackActive = false
    this.clearAnnotationCandidate(record)
    if (
      this.activeSurfaceId === record.id
      && this.surfaces.get(record.id) === record
      && (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION)
      && record.kind === 'artifact-preview'
      && !record.disposed
      && !record.crashed
      && record.visibleRequested
    ) {
      this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(record.owner))
    }
    return { ok: true }
  }

  private async watchWorkingPreview(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    let lastRevision: string | null = null
    const check = async (): Promise<void> => {
      if (record.disposed || record.crashed || record.view.webContents.isDestroyed()) return
      let keepWatching = true
      try {
        if (record.view.webContents.isLoading() && lastRevision !== null) return
        const controller = new AbortController()
        record.revisionRequest = controller
        const timeout = setTimeout(() => controller.abort(), 2000)
        timeout.unref()
        try {
          const response = await record.previewSession.fetch(record.documentUrl, {
            method: 'HEAD', cache: 'no-store', redirect: 'error', signal: controller.signal,
          })
          if (record.disposed || record.crashed) return
          if (!response.ok) return
          if (response.headers.get('x-opensquilla-working-preview') !== '1') {
            keepWatching = false
            return
          }
          const revision = response.headers.get('etag')
          if (revision && lastRevision && revision !== lastRevision && !record.view.webContents.isLoading()) {
            record.view.webContents.reload()
          }
          if (revision) lastRevision = revision
        } finally {
          clearTimeout(timeout)
          if (record.revisionRequest === controller) record.revisionRequest = null
        }
      } catch {
        // A lease may be renewing; the normal preview lifecycle reports its own errors.
      } finally {
        if (keepWatching && !record.disposed && !record.crashed) {
          record.revisionTimer = setTimeout(() => { void check() }, 1000)
          record.revisionTimer.unref()
        }
      }
    }
    await check()
  }

  private browserRecord(sessionKey: string, targetRef: string | undefined): NativeWorkbenchSurfaceRecord {
    const record = [...this.surfaces.values()].find(value => value.targetRef === targetRef)
    if (!record || record.scopeId !== sessionKey || record.disposed || record.crashed
      || record.owner.isDestroyed() || record.view.webContents.isDestroyed()) {
      throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The browser target no longer exists in this session.', 404)
    }
    return record
  }

  private describeBrowserRecord(record: NativeWorkbenchSurfaceRecord) {
    return {
      targetRef: record.targetRef, surfaceId: record.id, sessionKey: record.scopeId,
      url: record.view.webContents.getURL(), title: record.view.webContents.getTitle(), kind: record.kind,
      active: this.activeSurfaceId === record.id,
    }
  }

  getBrowserTarget(surfaceId: string) {
    const record = this.surfaces.get(surfaceId)
    if (!record) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The browser target no longer exists.', 404)
    return this.describeBrowserRecord(this.browserRecord(record.scopeId, record.targetRef))
  }

  async focusAnnotation(surfaceId: string, targetRef: string, locatorHint: string) {
    const target = this.getBrowserTarget(surfaceId)
    if (target.targetRef !== targetRef) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The annotation page was replaced.', 404)
    const record = this.browserRecord(target.sessionKey, targetRef)
    if (!this.isActiveAnnotationRecord(record)) throw new DesktopBrowserError('TARGET_NOT_ACTIVE', 'Open the annotation page before focusing it.')
    const group = `opensquilla-focus-${randomUUID()}`
    try {
      const { rootObjectId } = await this.browserRoot(record, group)
      const found = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: rootObjectId, objectGroup: group,
        functionDeclaration: 'function (selector) { const nodes = document.querySelectorAll(selector); return nodes.length === 1 ? nodes[0] : null }',
        arguments: [{ value: locatorHint }], returnByValue: false, silent: true,
      }) as { result?: { objectId?: string } }
      if (!found.result?.objectId) throw new DesktopBrowserError('ELEMENT_NOT_FOUND', 'The annotated element is unavailable or ambiguous.')
      const node = await this.cdpCommand(record, 'DOM.describeNode', { objectId: found.result.objectId }) as { node?: { backendNodeId?: number } }
      await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: found.result.objectId, functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_SCROLL_FUNCTION,
        returnByValue: true, silent: true,
      })
      this.browserRecord(target.sessionKey, targetRef)
      await this.clearAnnotationFocusHighlight(record)
      await this.cdpCommand(record, 'Overlay.highlightNode', {
        backendNodeId: node.node?.backendNodeId, highlightConfig: NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG,
      })
      record.annotationFocusTimer = setTimeout(() => { void this.clearAnnotationFocusHighlight(record) }, 2500)
      record.annotationFocusTimer.unref()
      return { ok: true, targetRef }
    } finally {
      await this.cdpCommand(record, 'Runtime.releaseObjectGroup', { objectGroup: group }).catch(() => undefined)
    }
  }

  async executeBrowser(request: DesktopBrowserRequest, signal: AbortSignal): Promise<unknown> {
    const check = () => {
      if (signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'The browser request ended.', 504)
    }
    check()
    if (request.operation === 'list') {
      return { targets: [...this.surfaces.values()].filter(record => record.scopeId === request.sessionKey
        && !record.disposed && !record.crashed && !record.view.webContents.isDestroyed()
        && record.kind !== 'artifact-html').map(record => this.describeBrowserRecord(record)) }
    }
    if (request.operation === 'open' && !request.targetRef) {
      const url = parseNativeWorkbenchNavigationUrl(request.url)
      if (this.isPrivilegedGatewayTarget(url)) throw new DesktopBrowserError('NAVIGATION_BLOCKED', 'This URL is unavailable inside isolated previews.')
      const surfaceId = `browser-${randomUUID()}`
      const result = await this.createSurface({ version: NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
        surfaceId, kind: 'url-preview', payload: { url, scopeId: request.sessionKey } })
      if (!result.ok) throw new DesktopBrowserError('OPEN_FAILED', result.message || 'The browser page could not open.')
      const record = this.surfaces.get(surfaceId)
      if (!record) throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The browser page was closed.', 404)
      const assertOpeningRecord = () => {
        if (this.surfaces.get(surfaceId) !== record || record.disposed || record.crashed || record.owner.isDestroyed()
          || record.view.webContents.isDestroyed()) {
          throw new DesktopBrowserError('TARGET_NOT_FOUND', 'The browser page was closed.', 404)
        }
      }
      const assertOpening = () => { check(); assertOpeningRecord() }
      try {
        await this.queueSurfaceOperation(`operation:${record.targetRef}`, async () => {
          assertOpening()
          // A never-shown child view can have a zero-sized renderer despite native
          // bounds. Initialize that same renderer, then immediately remove emulation
          // so later UI layout and device scale remain native.
          let initializationFailed = false
          try {
            await this.cdpCommand(record, 'Emulation.setDeviceMetricsOverride', {
              width: 960, height: 720, deviceScaleFactor: 0, mobile: false,
            }, assertOpening)
          } catch (error) {
            initializationFailed = true
            throw error
          } finally {
            try {
              await this.cdpCommand(record, 'Emulation.clearDeviceMetricsOverride', undefined, assertOpeningRecord)
            } catch (error) {
              if (!initializationFailed) throw error
            }
          }
          assertOpening()
        })
        assertOpening()
        // The current session's UI adopts the hidden page and supplies its visible layout.
        const target = this.getBrowserTarget(surfaceId)
        this.emit(record, 'browser-opened', { url: target.url, title: target.title,
          sessionKey: request.sessionKey, targetRef: target.targetRef })
        return target
      } catch (error) {
        if (this.surfaces.get(surfaceId) === record) await this.destroyRecord(record)
        throw error
      }
    }
    const record = this.browserRecord(request.sessionKey, request.targetRef)
    if (record.kind === 'artifact-html') throw new DesktopBrowserError('BROWSER_UNAVAILABLE', 'Reopen this legacy preview to enable browser control.')
    const assertCurrent = () => { check(); this.browserRecord(request.sessionKey, request.targetRef) }
    if (request.operation === 'open' || request.operation === 'reload') {
      const navigation = await this.navigateSurface({ version: record.version as 2 | 3 | 4,
        surfaceId: record.id, action: request.operation === 'open' ? 'navigate' : 'reload',
        ...(request.operation === 'open' ? { url: parseNativeWorkbenchNavigationUrl(request.url) } : {}) })
      assertCurrent()
      if (!navigation.ok) throw new DesktopBrowserError('NAVIGATION_BLOCKED', navigation.message || 'Browser navigation failed.')
      return { ...this.describeBrowserRecord(record), loading: record.view.webContents.isLoading() }
    }
    if (!record.browserDocumentReady || !record.cdpReady) {
      throw new DesktopBrowserError('PAGE_NOT_READY', 'The browser page is still loading or unavailable.')
    }
    if (request.operation === 'screenshot') {
      const generation = record.annotationDocumentGeneration
      const image = await record.view.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true })
      let png = image.toPNG()
      let { width, height } = image.getSize()
      if (!png.length) {
        // Hidden child views may have no compositor surface. CDP captures the same
        // WebContents renderer without activating another tab or opening a new page.
        const captured = await this.cdpCommand(record, 'Page.captureScreenshot', {
          format: 'png', captureBeyondViewport: false,
        }, assertCurrent) as { data?: string }
        if (typeof captured.data === 'string' && captured.data.length <= 12 * 1024 * 1024) {
          png = Buffer.from(captured.data, 'base64')
          if (png.length >= 24 && png.subarray(1, 4).toString() === 'PNG') {
            width = png.readUInt32BE(16)
            height = png.readUInt32BE(20)
          }
        }
      }
      assertCurrent()
      if (generation !== record.annotationDocumentGeneration) throw new DesktopBrowserError('PAGE_CHANGED', 'The page navigated during capture.')
      if (!png.length || png.length > 8 * 1024 * 1024 || width < 1 || height < 1) throw new DesktopBrowserError('SCREENSHOT_UNAVAILABLE', 'The screenshot is empty or exceeds 8 MiB.')
      return { targetRef: record.targetRef, mimeType: 'image/png', dataBase64: png.toString('base64'), width, height }
    }
    // Serialize short browser operations only. No turn lease prevents the user from closing or replacing a page.
    return await this.queueSurfaceOperation(`operation:${record.targetRef}`, async () => {
      assertCurrent()
      const result = request.operation === 'snapshot'
        ? await this.snapshotBrowser(record, assertCurrent)
        : await this.performBrowserAction(record, request, assertCurrent)
      assertCurrent()
      return result
    })
  }

  private async snapshotBrowser(record: NativeWorkbenchSurfaceRecord, assertCurrent: () => void) {
    this.invalidateBrowserAnchors(record)
    const generation = record.annotationDocumentGeneration
    const group = `opensquilla-browser-${randomUUID()}`
    record.browserObjectGroup = group
    const { rootObjectId } = await this.browserRoot(record, group)
    const snapshot = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
      objectId: rootObjectId, objectGroup: group,
      functionDeclaration: `function () {
        return Array.from(document.querySelectorAll('a,button,input,textarea,select,summary,[role],[contenteditable],h1,h2,h3,p,label')).slice(0,2400).filter(node => {
          const rect = node.getBoundingClientRect(); const style = getComputedStyle(node);
          return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }).slice(0,160)
      }`, returnByValue: false, silent: true,
    }) as { result?: { objectId?: string } }
    if (!snapshot.result?.objectId) throw new DesktopBrowserError('SNAPSHOT_FAILED', 'The browser snapshot is unavailable.')
    const objects = await this.cdpCommand(record, 'Runtime.getProperties', {
      objectId: snapshot.result.objectId, ownProperties: true,
    }) as { result?: { name: string; value?: { objectId?: string } }[] }
    const description = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
      objectId: snapshot.result.objectId,
      functionDeclaration: `function () { return {
        text: (document.body?.innerText || '').slice(0,24000),
        nodes: this.map(node => ({tagName: node.localName, role: node.getAttribute('role') || node.localName,
          name: (node.getAttribute('aria-label') || node.innerText || node.getAttribute('placeholder') || '').slice(0,256),
          disabled: node.matches(':disabled,[aria-disabled="true"]')}))
      } }`, returnByValue: true, silent: true,
    }) as { result?: { value?: { text: string; nodes: Record<string, unknown>[] } } }
    assertCurrent()
    if (generation !== record.annotationDocumentGeneration) throw new DesktopBrowserError('PAGE_CHANGED', 'The page navigated during inspection.')
    const refs: Record<string, unknown>[] = []
    for (const property of objects.result ?? []) {
      if (!/^\d+$/.test(property.name) || !property.value?.objectId) continue
      const ref = `e-${randomUUID()}`
      record.browserAnchors.set(ref, { objectId: property.value.objectId, documentGeneration: generation })
      refs.push({ ref, ...description.result?.value?.nodes[Number(property.name)] })
    }
    return { ...this.describeBrowserRecord(record), text: description.result?.value?.text || '', refs,
      truncated: refs.length === 160, diagnostics: { runtimeError: record.browserRuntimeException,
        missingResource: record.missingResourceReported, blockedNetwork: record.blockedNetworkReported } }
  }

  private async performBrowserAction(record: NativeWorkbenchSurfaceRecord, request: DesktopBrowserRequest, assertCurrent: () => void) {
    const generation = record.annotationDocumentGeneration
    const anchor = request.ref ? record.browserAnchors.get(request.ref) : undefined
    if (request.ref && (!anchor || anchor.documentGeneration !== generation)) {
      throw new DesktopBrowserError('STALE_ELEMENT', 'The element reference expired. Request a new snapshot.')
    }
    const send = async (method: string, params: Record<string, unknown>) => {
      assertCurrent()
      if (generation !== record.annotationDocumentGeneration) throw new DesktopBrowserError('PAGE_CHANGED', 'The page navigated before the action completed.')
      return await this.cdpCommand(record, method, params, () => {
        assertCurrent()
        if (generation !== record.annotationDocumentGeneration) throw new DesktopBrowserError('PAGE_CHANGED', 'The page navigated before the action was sent.')
      })
    }
    if (anchor) {
      const geometry = await send('Runtime.callFunctionOn', {
        objectId: anchor.objectId,
        functionDeclaration: `async function () {
          if (!this.isConnected || this.ownerDocument !== document) return null;
          if (this.matches(':disabled,[aria-disabled="true"]')) return {unavailable:true};
          this.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
          let previous=this.getBoundingClientRect();
          for (let attempt=0;attempt<8;attempt++) {
            await new Promise(resolve=>setTimeout(resolve,16));
            if (!this.isConnected || this.ownerDocument !== document) return null;
            const r=this.getBoundingClientRect();
            if (['x','y','width','height'].some(key=>Math.abs(r[key]-previous[key])>0.5)) { previous=r; continue; }
            for (let node=this;node instanceof Element;node=node.parentElement) {
              const style=getComputedStyle(node);
              if (style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse'||Number(style.opacity)<=0) return {unavailable:true};
            }
            const left=Math.max(0,r.left),right=Math.min(innerWidth,r.right),top=Math.max(0,r.top),bottom=Math.min(innerHeight,r.bottom);
            if (right<=left||bottom<=top) return {unavailable:true};
            const x=(left+right)/2,y=(top+bottom)/2,hit=document.elementFromPoint(x,y);
            if (!hit || (hit!==this && !this.contains(hit))) return {unavailable:true};
            return {x,y,width:r.width,height:r.height};
          }
          return {unavailable:true};
        }`, returnByValue: true, awaitPromise: true, silent: true,
      }) as { result?: { value?: { x: number; y: number; width: number; height: number; unavailable?: boolean } } }
      const rect = geometry.result?.value
      if (!rect) throw new DesktopBrowserError('STALE_ELEMENT', 'The selected element is no longer available.')
      if (rect.unavailable || rect.width <= 0 || rect.height <= 0) throw new DesktopBrowserError('ACTION_UNAVAILABLE', 'The element is hidden, disabled, moving or covered.')
      if (request.action === 'click' || request.action === 'hover') {
        await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: rect.x, y: rect.y })
        if (request.action === 'click') {
          const hit = await send('Runtime.callFunctionOn', {
            objectId: anchor.objectId,
            functionDeclaration: `function (x,y) {
              if (!this.isConnected || this.matches(':disabled,[aria-disabled="true"]')) return false;
              const node=document.elementFromPoint(x,y);
              return node===this || Boolean(node && this.contains(node));
            }`, arguments: [{value:rect.x},{value:rect.y}], returnByValue: true, silent: true,
          }) as { result?: { value?: boolean } }
          if (hit.result?.value !== true) throw new DesktopBrowserError('ACTION_UNAVAILABLE', 'The element moved or became covered before the click.')
          await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: rect.x, y: rect.y, button: 'left', clickCount: 1 })
          await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: rect.x, y: rect.y, button: 'left', clickCount: 1 })
        }
      } else {
        const prepared = await send('Runtime.callFunctionOn', {
          objectId: anchor.objectId,
          functionDeclaration: `function (action, text) {
            if (!this.isConnected || this.matches(':disabled,[aria-disabled="true"]')) return false;
            this.focus();
            if (action === 'fill') {
              if (this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement) {
                if (this.readOnly || (this instanceof HTMLInputElement && ['file','hidden','checkbox','radio','submit','button'].includes(this.type))) return false;
                this.select();
              } else if (this.isContentEditable) { const range=document.createRange(); range.selectNodeContents(this); const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range); }
              else return false;
            }
            if (action === 'select') {
              if (!(this instanceof HTMLSelectElement) || !Array.from(this.options).some(option=>option.value===text)) return false;
              this.value=text; this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true}));
            }
            return true;
          }`, arguments: [{ value: request.action }, { value: request.text ?? '' }], returnByValue: true, silent: true,
        }) as { result?: { value?: boolean } }
        if (prepared.result?.value !== true) throw new DesktopBrowserError('ACTION_UNAVAILABLE', 'The element does not support this action.')
        if (request.action === 'fill') await send('Input.insertText', { text: request.text ?? '' })
      }
    }
    if (request.action === 'press') {
      const key = request.key!
      const codes: Record<string, number> = { Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46,
        ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34, Space: 32 }
      if (!(key in codes) && [...key].length !== 1) throw new DesktopBrowserError('INVALID_REQUEST', 'Unsupported browser key.', 400)
      const text = key === 'Enter' ? '\r' : key === 'Space' ? ' ' : key.length === 1 ? key : undefined
      const params = { key: key === 'Space' ? ' ' : key, windowsVirtualKeyCode: codes[key] ?? key.toUpperCase().charCodeAt(0), ...(text ? { text, unmodifiedText: text } : {}) }
      await send('Input.dispatchKeyEvent', { type: 'keyDown', ...params })
      await send('Input.dispatchKeyEvent', { type: 'keyUp', ...params })
    }
    if (request.action === 'scroll') {
      const bounds = record.view.getBounds()
      const amount = request.amount ?? 600
      await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: Math.max(1, bounds.width / 2), y: Math.max(1, bounds.height / 2),
        deltaX: request.direction === 'left' ? -amount : request.direction === 'right' ? amount : 0,
        deltaY: request.direction === 'up' ? -amount : request.direction === 'down' ? amount : 0 })
    }
    return { targetRef: record.targetRef, action: request.action, performed: true }
  }

  private activeAnnotationRecord(): NativeWorkbenchSurfaceRecord | null {
    if (!this.activeSurfaceId) return null
    const record = this.surfaces.get(this.activeSurfaceId)
    return record && this.isActiveAnnotationRecord(record) ? record : null
  }

  private annotationRecordForUiRequest(
    surfaceId: string,
  ): NativeWorkbenchSurfaceRecord | null {
    const record = this.surfaces.get(surfaceId)
    return record && this.isActiveAnnotationRecord(record) ? record : null
  }

  private annotationRecordForCleanupRequest(
    surfaceId: string,
  ): NativeWorkbenchSurfaceRecord | null {
    const record = this.surfaces.get(surfaceId)
    return record
      && record.kind !== 'artifact-html'
      && (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION)
      && !record.disposed
      ? record
      : null
  }

  private isActiveAnnotationRecord(record: NativeWorkbenchSurfaceRecord): boolean {
    return record.kind !== 'artifact-html' && this.surfaces.get(record.id) === record
      && !record.disposed && !record.crashed && !record.view.webContents.isDestroyed()
      && this.activeSurfaceId === record.id && record.visibleRequested
  }

  private activeAnnotationOverlayBinding(
    record: NativeWorkbenchSurfaceRecord,
  ): NativeWorkbenchAnnotationOverlayBinding | null {
    const binding = this.annotationOverlays.get(record.owner)?.binding
    return binding?.record === record ? binding : null
  }

  private stopAnnotationGeometryWatcher(candidate: NativeWorkbenchAnnotationCandidate): void {
    if (candidate.geometryTimer) clearInterval(candidate.geometryTimer)
    candidate.geometryTimer = null
  }

  private clearAnnotationCandidate(record: NativeWorkbenchSurfaceRecord): void {
    const candidate = record.annotationCandidate
    record.annotationCandidate = null
    if (!candidate) return
    this.stopAnnotationGeometryWatcher(candidate)
    if (
      !record.view.webContents.isDestroyed()
      && record.view.webContents.debugger.isAttached()
    ) {
      void this.cdpCommand(record, 'Runtime.releaseObjectGroup', {
        objectGroup: candidate.objectGroup,
      }).catch(() => undefined)
    }
  }

  private applyAnnotationGeometry(
    record: NativeWorkbenchSurfaceRecord,
    candidate: NativeWorkbenchAnnotationCandidate,
    geometry: {
      rect: NativeWorkbenchAnnotationSelection['rect']
      viewportWidth: number
      viewportHeight: number
    },
  ): void {
    if (record.annotationCandidate !== candidate) {
      throw new Error('The selected preview element changed during inspection.')
    }
    candidate.selection = {
      ...candidate.selection,
      rect: geometry.rect,
    }
    candidate.viewportWidth = geometry.viewportWidth
    candidate.viewportHeight = geometry.viewportHeight
    const overlay = this.annotationOverlays.get(record.owner)
    if (
      overlay?.binding?.record === record
      && !record.annotationFallbackActive
      && record.rect
    ) {
      this.presentAnnotationOverlay(
        overlay,
        this.annotationOverlayBounds(record, candidate),
        this.ownerCanShowSurfaces(record.owner),
        true,
      )
    }
  }

  private async refreshAnnotationCandidateIntegrity(
    record: NativeWorkbenchSurfaceRecord,
    candidate: NativeWorkbenchAnnotationCandidate,
  ): Promise<void> {
    try {
      if (
        record.annotationCandidate !== candidate
        || candidate.documentGeneration !== record.annotationDocumentGeneration
        || !this.isActiveAnnotationRecord(record)
      ) throw new Error('The selected preview element is stale or unavailable.')
      const inspected = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: candidate.objectId,
        objectGroup: candidate.objectGroup,
        functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_INSPECT_FUNCTION,
        awaitPromise: true,
        returnByValue: true,
        silent: true,
      }) as {
        exceptionDetails?: unknown
        result?: { value?: unknown }
      }
      if (inspected.exceptionDetails) {
        throw new Error('The selected preview element could not be inspected safely.')
      }
      const raw = inspected.result?.value
      if (raw && typeof raw === 'object' && (raw as Record<string, unknown>).ok === false) {
        throw new Error('The selected preview element is no longer editable.')
      }
      const current = parseNativeWorkbenchAnnotationSelection(raw)
      if (
        current.tagName !== candidate.selection.tagName
        || current.elementPath !== candidate.selection.elementPath
      ) throw new Error('The preview DOM changed after the element was selected.')
      this.applyAnnotationGeometry(record, candidate, current)
    } catch (error) {
      if (record.annotationCandidate === candidate) this.clearAnnotationCandidate(record)
      throw error
    }
  }

  private async refreshAnnotationGeometry(
    record: NativeWorkbenchSurfaceRecord,
    candidate: NativeWorkbenchAnnotationCandidate,
  ): Promise<void> {
    if (
      record.annotationCandidate !== candidate
      || candidate.documentGeneration !== record.annotationDocumentGeneration
      || !this.isActiveAnnotationRecord(record)
    ) throw new Error('The selected preview element is stale or unavailable.')
    const inspected = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
      objectId: candidate.objectId,
      objectGroup: candidate.objectGroup,
      functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_GEOMETRY_FUNCTION,
      returnByValue: true,
      silent: true,
    }) as {
      exceptionDetails?: unknown
      result?: { value?: unknown }
    }
    if (inspected.exceptionDetails) {
      throw new Error('The selected preview element geometry is unavailable.')
    }
    const raw = inspected.result?.value
    if (raw && typeof raw === 'object' && (raw as Record<string, unknown>).ok === false) {
      throw new Error('The selected preview element is no longer editable.')
    }
    this.applyAnnotationGeometry(
      record,
      candidate,
      parseNativeWorkbenchAnnotationGeometry(raw),
    )
  }

  private startAnnotationGeometryWatcher(
    record: NativeWorkbenchSurfaceRecord,
    candidate: NativeWorkbenchAnnotationCandidate,
  ): void {
    this.stopAnnotationGeometryWatcher(candidate)
    candidate.geometryTimer = setInterval(() => {
      if (
        candidate.geometryRefreshPending
        || record.annotationCandidate !== candidate
        || !this.activeAnnotationOverlayBinding(record)
      ) return
      candidate.geometryRefreshPending = true
      void this.refreshAnnotationGeometry(record, candidate).catch(() => {
        // Closing an editor can release its candidate while this CDP read is
        // still in flight. Never let that retired read cancel a picker that a
        // newer close/rearm transaction has already installed.
        if (record.annotationCandidate === candidate) {
          void this.cancelAnnotationInteraction(record, 'selection-stale', true)
        }
      }).finally(() => {
        candidate.geometryRefreshPending = false
      })
    }, 100)
    candidate.geometryTimer.unref()
  }

  private async clearAnnotationFocusHighlight(
    record: NativeWorkbenchSurfaceRecord,
  ): Promise<void> {
    if (record.annotationFocusTimer) clearTimeout(record.annotationFocusTimer)
    record.annotationFocusTimer = null
    if (
      record.cdpReady
      && !record.view.webContents.isDestroyed()
      && record.view.webContents.debugger.isAttached()
    ) {
      await this.cdpCommand(record, 'Overlay.hideHighlight').catch(() => undefined)
    }
  }

  private invalidateBrowserAnchors(record: NativeWorkbenchSurfaceRecord): void {
    const objectGroup = record.browserObjectGroup
    record.browserObjectGroup = null
    if (objectGroup) void this.cdpCommand(record, 'Runtime.releaseObjectGroup', { objectGroup }).catch(() => undefined)
    record.browserAnchors.clear()
    record.browserAnchorGeneration += 1
  }

  private async browserRoot(
    record: NativeWorkbenchSurfaceRecord,
    objectGroup: string,
  ): Promise<{ rootObjectId: string; executionContextId: number }> {
    const frameTree = await this.cdpCommand(record, 'Page.getFrameTree') as {
      frameTree?: { frame?: { id?: unknown } }
    }
    const frameId = frameTree.frameTree?.frame?.id
    if (typeof frameId !== 'string' || frameId.length === 0) {
      throw new Error('The top-level preview frame is unavailable.')
    }
    const world = await this.cdpCommand(record, 'Page.createIsolatedWorld', {
      frameId,
      worldName: 'opensquilla-artifact-browser',
      grantUniveralAccess: false,
    }) as { executionContextId?: unknown }
    if (!Number.isSafeInteger(world.executionContextId)) {
      throw new Error('The isolated browser inspector context is unavailable.')
    }
    const root = await this.cdpCommand(record, 'Runtime.evaluate', {
      expression: 'document.documentElement',
      contextId: world.executionContextId,
      objectGroup,
      returnByValue: false,
      silent: true,
    }) as {
      exceptionDetails?: unknown
      result?: { objectId?: unknown }
    }
    const rootObjectId = root.result?.objectId
    if (
      root.exceptionDetails
      || typeof rootObjectId !== 'string'
      || !rootObjectId
    ) throw new Error('The browser page root is unavailable.')
    return {
      rootObjectId,
      executionContextId: world.executionContextId as number,
    }
  }

  private async initializeAnnotationCdp(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    await this.ensureDebuggerAttached(record)
    await this.cdpCommand(record, 'Page.enable')
    await this.cdpCommand(record, 'Runtime.enable')
    await this.cdpCommand(record, 'DOM.enable')
    await this.cdpCommand(record, 'Overlay.enable')
    record.cdpReady = true
  }

  private async ensureDebuggerAttached(record: NativeWorkbenchSurfaceRecord): Promise<void> {
    const contents = record.view.webContents
    if (contents.debugger.isAttached()) return
    if (!contents.getURL()) await contents.loadURL('about:blank')
    contents.debugger.attach('1.3')
    contents.debugger.on('message', (_event, method, params) => {
      if (
        (method === 'Runtime.exceptionThrown' || method === 'Runtime.consoleAPICalled')
      ) {
        const payload = params && typeof params === 'object'
          ? params as Record<string, unknown>
          : null
        const consoleType = payload?.type
        if (
          method === 'Runtime.exceptionThrown'
          || consoleType === 'error'
          || consoleType === 'assert'
        ) {
          record.browserRuntimeException = true
          this.invalidateBrowserAnchors(record)
        }
      }
      if (method !== 'Overlay.inspectNodeRequested') return
      const payload = params && typeof params === 'object'
        ? params as Record<string, unknown>
        : null
      const backendNodeId = payload?.backendNodeId
      if (!Number.isSafeInteger(backendNodeId) || (backendNodeId as number) <= 0) return
      void this.handleAnnotationNodeSelected(record, backendNodeId as number)
    })
    contents.debugger.on('detach', (_event, reason) => {
      record.cdpReady = false
      this.invalidateBrowserAnchors(record)
      record.annotationPickerActive = false
      void this.cancelAnnotationInteraction(record, 'debugger-detached', true)
      if (record.debuggerExpectedDetach || record.disposed || record.crashed) return
      if (record.mode === 'offline') {
        this.failRecord(record, 'error', {
          message: 'The offline browser isolation guard stopped unexpectedly.',
          reason: reason || 'offline-realm-guard-detached',
        })
      } else {
        this.emit(record, 'blocked-action', {
          action: 'annotation-picker',
          reason: reason || 'annotation-debugger-detached',
        })
      }
    })
  }

  private cdpCommand(
    record: NativeWorkbenchSurfaceRecord,
    method: string,
    params?: Record<string, unknown>,
    beforeSend?: () => void,
  ): Promise<unknown> {
    const operation = record.cdpQueue.then(async () => {
      beforeSend?.()
      if (
        record.disposed
        || record.crashed
        || record.view.webContents.isDestroyed()
        || !record.view.webContents.debugger.isAttached()
      ) throw new Error('The isolated DOM inspector is unavailable.')
      let timeout: NodeJS.Timeout | undefined
      try {
        return await Promise.race([
          record.view.webContents.debugger.sendCommand(method, params),
          new Promise<never>((_resolve, reject) => {
            timeout = setTimeout(
              () => reject(new Error(`DOM inspector command timed out: ${method}`)),
              NATIVE_WORKBENCH_CDP_TIMEOUT_MS,
            )
            timeout.unref()
          }),
        ])
      } finally {
        if (timeout) clearTimeout(timeout)
      }
    })
    record.cdpQueue = operation.then(() => undefined, () => undefined)
    return operation
  }

  private async handleAnnotationNodeSelected(
    record: NativeWorkbenchSurfaceRecord,
    backendNodeId: number,
  ): Promise<void> {
    if (!record.annotationPickerActive || !this.isActiveAnnotationRecord(record)) return
    const pickerEpoch = ++record.annotationPickerEpoch
    record.annotationPickerActive = false
    const generation = record.annotationDocumentGeneration
    // Chromium exits inspect mode as part of dispatching inspectNodeRequested.
    // Reassert the clean state best-effort, but do not reject a valid selected
    // node merely because that redundant command races the automatic exit.
    await this.clearAnnotationInspectState(record, false)
    const objectGroup = `opensquilla-annotation-${randomUUID()}`
    let retainedObjectGroup = false
    try {
      const frameTree = await this.cdpCommand(record, 'Page.getFrameTree') as {
        frameTree?: { frame?: { id?: unknown } }
      }
      const frameId = frameTree.frameTree?.frame?.id
      if (typeof frameId !== 'string' || frameId.length === 0) {
        throw new Error('The top-level preview frame is unavailable.')
      }
      const world = await this.cdpCommand(record, 'Page.createIsolatedWorld', {
        frameId,
        worldName: 'opensquilla-artifact-annotation',
        grantUniveralAccess: false,
      }) as { executionContextId?: unknown }
      if (!Number.isSafeInteger(world.executionContextId)) {
        throw new Error('The isolated DOM inspector context is unavailable.')
      }
      const resolved = await this.cdpCommand(record, 'DOM.resolveNode', {
        backendNodeId,
        executionContextId: world.executionContextId,
        objectGroup,
      }) as { object?: { objectId?: unknown } }
      const objectId = resolved.object?.objectId
      if (typeof objectId !== 'string' || objectId.length === 0) {
        throw new Error('The selected preview node is unavailable.')
      }
      const inspected = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId,
        objectGroup,
        functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_INSPECT_FUNCTION,
        awaitPromise: true,
        returnByValue: true,
        silent: true,
      }) as {
        exceptionDetails?: unknown
        result?: { value?: unknown }
      }
      if (inspected.exceptionDetails) {
        throw new Error('The selected preview node could not be inspected safely.')
      }
      const raw = inspected.result?.value
      if (
        raw
        && typeof raw === 'object'
        && (raw as Record<string, unknown>).ok === false
      ) {
        const reason = (raw as Record<string, unknown>).reason
        throw new Error(typeof reason === 'string' ? reason : 'Unsupported preview node.')
      }
      const candidate = parseNativeWorkbenchAnnotationSelection(raw)
      if (
        generation !== record.annotationDocumentGeneration
        || !this.isActiveAnnotationRecord(record)
      ) throw new Error('The selected preview element changed during inspection.')
      const selection: NativeWorkbenchAnnotationSelection = {
        selectionId: randomUUID(),
        tagName: candidate.tagName,
        elementPath: candidate.elementPath,
        targetRef: record.targetRef,
        locatorHint: candidate.locatorHint,
        selectionText: candidate.selectionText,
        rect: candidate.rect,
      }
      record.annotationCandidate = {
        selection,
        viewportWidth: candidate.viewportWidth,
        viewportHeight: candidate.viewportHeight,
        documentGeneration: generation,
        objectGroup,
        objectId,
        geometryTimer: null,
        geometryRefreshPending: false,
      }
      retainedObjectGroup = true
      this.auditAnnotationPicker(
        record,
        'selection-emitted',
        'succeeded',
        'completed',
      )
      this.emit(record, 'annotation-selected', { selection })
    } catch (error) {
      const selectionIsCurrent = this.annotationPickerTransitionIsCurrent(
        record,
        pickerEpoch,
        generation,
        null,
      )
      if (!selectionIsCurrent) return
      this.clearAnnotationCandidate(record)
      this.auditAnnotationPicker(
        record,
        'selection-rejected',
        'failed',
        'selection-rejected',
      )
      this.emit(record, 'blocked-action', {
        action: 'annotation-picker',
        reason: errorMessage(error).slice(0, 200),
      })
      // Chromium inspect mode is one-shot: even an unsupported node (for
      // example a page-wide CSS pseudo-element) consumes searchForNode before
      // the isolated inspector can reject it. Keep the user's annotation
      // intent alive by installing a fresh picker, fenced to this exact
      // surface generation. A concurrent Stop, navigation, hide, or replace
      // advances the epoch and prevents this recovery from reactivating it.
      const rearmEpoch = ++record.annotationPickerEpoch
      const rearmed = await this.armAnnotationPicker(record, rearmEpoch, generation, null)
      if (
        !rearmed.ok
        && this.annotationPickerTransitionIsCurrent(record, rearmEpoch, generation, null)
      ) {
        // Do not expose the raw CDP failure. WebUI uses this stable signal to
        // clear its optimistic armed state and invoke the existing one-shot
        // bounded recovery instead of leaving a pressed but inert toolbar.
        this.emit(record, 'blocked-action', {
          action: 'annotation-picker',
          code: 'ANNOTATION_REARM_FAILED',
          reason: 'annotation-picker-rearm-failed',
          surfaceInstanceId: record.surfaceInstanceId,
        })
      }
    } finally {
      if (!retainedObjectGroup) {
        await this.cdpCommand(record, 'Runtime.releaseObjectGroup', { objectGroup })
          .catch(() => undefined)
      }
    }
  }

  private async cancelAnnotationInteraction(
    record: NativeWorkbenchSurfaceRecord,
    reason: string,
    emitCancel: boolean,
  ): Promise<string | null> {
    // Fence delayed focus cleanup synchronously. Navigation and destruction
    // intentionally do not wait for this async routine before tearing down the
    // child renderer, so a prior timer must not outlive the surface generation.
    const startedAt = Date.now()
    record.annotationPickerEpoch += 1
    const inspectModeMayBeActive = record.annotationPickerActive
    if (record.annotationFocusTimer) clearTimeout(record.annotationFocusTimer)
    record.annotationFocusTimer = null
    record.annotationPickerActive = false
    this.clearAnnotationCandidate(record)
    const overlay = this.annotationOverlays.get(record.owner)
    const binding = overlay?.binding
    if (overlay && binding?.record === record) {
      if (emitCancel) {
        this.emit(record, 'annotation-cancel', {
          annotationId: binding.annotationId,
          reason,
        })
      }
      this.closeAnnotationOverlayBinding(overlay, false)
    }
    record.annotationFallbackActive = false
    const cleanupFailure = await this.clearAnnotationInspectState(
      record,
      inspectModeMayBeActive,
    )
    if (
      cleanupFailure
      && !record.disposed
      && !record.crashed
      && !record.view.webContents.isDestroyed()
      && record.view.webContents.debugger.isAttached()
    ) record.annotationPickerActive = true
    this.auditAnnotationPicker(
      record,
      cleanupFailure ? 'failed' : 'cancelled',
      cleanupFailure ? 'failed' : 'cancelled',
      cleanupFailure ? 'reset-failed' : reason,
      startedAt,
    )
    return cleanupFailure
  }

  private async clearAnnotationInspectState(
    record: NativeWorkbenchSurfaceRecord,
    inspectModeMayBeActive: boolean,
  ): Promise<string | null> {
    if (record.annotationFocusTimer) clearTimeout(record.annotationFocusTimer)
    record.annotationFocusTimer = null
    if (
      record.view.webContents.isDestroyed()
      || !record.view.webContents.debugger.isAttached()
    ) return null

    let inspectModeDisableError: unknown = null
    if (inspectModeMayBeActive) {
      try {
        await this.cdpCommand(record, 'Overlay.setInspectMode', {
          mode: 'none',
          highlightConfig: NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG,
        })
      } catch (error) {
        inspectModeDisableError = error
      }
    }
    // setInspectMode(none) is the authoritative picker state transition and
    // clears its hover decoration in Chromium. hideHighlight is retained as a
    // compatibility cleanup for explicit focus highlights; its failure cannot
    // reactivate inspect mode and must not turn a confirmed stop into an error.
    try {
      await this.cdpCommand(record, 'Overlay.hideHighlight')
    } catch {}
    return inspectModeDisableError
      ? `The annotation picker could not be fully disabled: ${boundedAnnotationCdpError(
        inspectModeDisableError,
      )}`
      : null
  }

  private async annotationOverlayForOwner(
    owner: BrowserWindow,
  ): Promise<NativeWorkbenchAnnotationOverlayRecord> {
    const current = this.annotationOverlays.get(owner)
    if (current && !current.disposed && !current.view.webContents.isDestroyed()) {
      return current
    }
    const previewSession = session.fromPartition(
      `opensquilla-annotation-overlay:${randomUUID()}`,
      { cache: false },
    )
    const documentUrl = `data:text/html;charset=utf-8,${encodeURIComponent(
      NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HTML,
    )}`
    previewSession.setPermissionCheckHandler(() => false)
    previewSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
    previewSession.on('will-download', event => event.preventDefault())
    previewSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
      callback({
        cancel: details.resourceType !== 'mainFrame' || details.url !== documentUrl,
      })
    })
    const view = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        webviewTag: false,
        devTools: false,
        navigateOnDragDrop: false,
        safeDialogs: true,
        spellcheck: true,
        preload: NATIVE_WORKBENCH_ANNOTATION_OVERLAY_PRELOAD,
        session: previewSession,
      },
    })
    // The trusted editor is a compact product surface, not a rectangular
    // browser debug view. Clip the native child view as well as its HTML card
    // so the rounded edge remains correct above light and dark previews.
    view.setBorderRadius(14)
    const overlay: NativeWorkbenchAnnotationOverlayRecord = {
      owner,
      previewSession,
      view,
      binding: null,
      disposed: false,
      focusTimer: null,
      ready: Promise.resolve(),
    }
    view.setVisible(false)
    view.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    view.webContents.on('will-navigate', event => event.preventDefault())
    view.webContents.on('devtools-opened', () => {
      if (!view.webContents.isDestroyed()) view.webContents.closeDevTools()
    })
    view.webContents.on('render-process-gone', () => {
      const binding = overlay.binding
      if (binding) {
        this.failAnnotationOverlay(
          binding.record,
          binding.annotationId,
          'trusted-overlay-renderer-gone',
        )
      }
      void this.disposeAnnotationOverlay(overlay)
    })
    owner.contentView.addChildView(view)
    overlay.ready = view.webContents.loadURL(documentUrl).then(() => undefined)
    this.annotationOverlays.set(owner, overlay)
    return overlay
  }

  private annotationOverlayBounds(
    record: NativeWorkbenchSurfaceRecord,
    candidate: NativeWorkbenchAnnotationCandidate,
  ): NativeWorkbenchSurfaceRect {
    const surface = record.rect!
    const scaleX = surface.width / candidate.viewportWidth
    const scaleY = surface.height / candidate.viewportHeight
    const selected = {
      x: surface.x + candidate.selection.rect.x * scaleX,
      y: surface.y + candidate.selection.rect.y * scaleY,
      width: candidate.selection.rect.width * scaleX,
      height: candidate.selection.rect.height * scaleY,
    }
    const gap = 8
    let x = selected.x + selected.width + gap
    let y = selected.y
    if (x + NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH > surface.x + surface.width) {
      x = selected.x - NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH - gap
    }
    if (x < surface.x) x = surface.x + surface.width - NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH
    if (y + NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HEIGHT > surface.y + surface.height) {
      y = selected.y + selected.height - NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HEIGHT
    }
    return {
      x: Math.round(Math.max(surface.x, Math.min(x, surface.x + surface.width
        - NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH))),
      y: Math.round(Math.max(surface.y, Math.min(y, surface.y + surface.height
        - NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HEIGHT))),
      width: Math.min(NATIVE_WORKBENCH_ANNOTATION_OVERLAY_WIDTH, surface.width),
      height: Math.min(NATIVE_WORKBENCH_ANNOTATION_OVERLAY_HEIGHT, surface.height),
    }
  }

  private raiseAnnotationOverlay(overlay: NativeWorkbenchAnnotationOverlayRecord): void {
    if (overlay.owner.isDestroyed() || overlay.view.webContents.isDestroyed()) return
    // Removing a focused WebContentsView from the native view hierarchy drops
    // its OS keyboard/IME focus. Geometry refreshes run frequently while an
    // annotation editor is open, so reparent only when another view has
    // actually moved above it.
    if (overlay.owner.contentView.children.at(-1) === overlay.view) return
    try {
      overlay.owner.contentView.removeChildView(overlay.view)
    } catch {}
    overlay.owner.contentView.addChildView(overlay.view)
  }

  private presentAnnotationOverlay(
    overlay: NativeWorkbenchAnnotationOverlayRecord,
    bounds: NativeWorkbenchSurfaceRect,
    visible: boolean,
    preserveFocus: boolean,
  ): void {
    if (overlay.owner.isDestroyed() || overlay.view.webContents.isDestroyed()) return
    const binding = overlay.binding
    const wasFocused = preserveFocus && visible && overlay.view.webContents.isFocused()
    const currentBounds = overlay.view.getBounds()
    const boundsChanged = currentBounds.x !== bounds.x
      || currentBounds.y !== bounds.y
      || currentBounds.width !== bounds.width
      || currentBounds.height !== bounds.height
    const needsRaise = overlay.owner.contentView.children.at(-1) !== overlay.view
    const visibilityChanged = overlay.view.getVisible() !== visible
    if (boundsChanged) overlay.view.setBounds(bounds)
    if (needsRaise) this.raiseAnnotationOverlay(overlay)
    if (visibilityChanged) overlay.view.setVisible(visible)
    // Electron may asynchronously move native focus away from a child
    // WebContentsView after setBounds/reparenting. Only restore focus when the
    // editor owned it before this layout mutation; ordinary user focus changes
    // must not be stolen by the geometry watcher.
    if (wasFocused && binding && (boundsChanged || needsRaise || visibilityChanged)) {
      this.focusAnnotationOverlay(overlay, binding, false)
    }
  }

  private focusAnnotationOverlay(
    overlay: NativeWorkbenchAnnotationOverlayRecord,
    binding: NativeWorkbenchAnnotationOverlayBinding | null = overlay.binding,
    activateOwner = true,
  ): void {
    if (overlay.focusTimer) {
      clearTimeout(overlay.focusTimer)
      overlay.focusTimer = null
    }
    let attempts = 0
    const focus = (): void => {
      overlay.focusTimer = null
      if (
        overlay.disposed
        || overlay.binding !== binding
        || !binding
        || overlay.owner.isDestroyed()
        || overlay.view.webContents.isDestroyed()
        || !overlay.view.getVisible()
      ) return
      if (!overlay.owner.isFocused()) {
        if (!activateOwner) return
        if (process.platform === 'darwin') app.focus({ steal: true })
        if (overlay.owner.isMinimized()) overlay.owner.restore()
        overlay.owner.show()
        overlay.owner.focus()
      }
      overlay.view.webContents.focus()
      attempts += 1
      // Native owner/view focus settles asynchronously on macOS and Windows.
      // Retry across settling event-loop turns, fenced to the same annotation
      // binding, so a close/rearm can never focus a stale editor.
      if (attempts < 4) {
        const retryDelay = [0, 32, 96][attempts - 1] ?? 96
        overlay.focusTimer = setTimeout(focus, retryDelay)
        overlay.focusTimer.unref()
      }
    }
    focus()
  }

  private clearAnnotationOverlayFocusTimer(
    overlay: NativeWorkbenchAnnotationOverlayRecord,
  ): void {
    if (!overlay.focusTimer) return
    clearTimeout(overlay.focusTimer)
    overlay.focusTimer = null
  }

  private handleAnnotationOverlayMessage(
    overlay: NativeWorkbenchAnnotationOverlayRecord,
    binding: NativeWorkbenchAnnotationOverlayBinding,
    value: unknown,
  ): void {
    if (
      overlay.binding !== binding
      || !this.isActiveAnnotationRecord(binding.record)
      || binding.record.annotationCandidate?.selection.selectionId !== binding.selectionId
    ) return
    let message
    try {
      message = parseNativeWorkbenchAnnotationOverlayMessage(value)
    } catch {
      this.failAnnotationOverlay(binding.record, binding.annotationId, 'invalid-overlay-message')
      return
    }
    if (message.type === 'draft-changed') {
      this.emit(binding.record, 'annotation-draft-change', {
        annotationId: binding.annotationId,
        body: message.body,
      })
      return
    }
    if (message.type === 'submit') {
      this.emit(binding.record, 'annotation-submit', {
        annotationId: binding.annotationId,
        body: message.body,
      })
    } else {
      this.emit(binding.record, 'annotation-cancel', {
        annotationId: binding.annotationId,
        reason: 'user-cancelled',
      })
    }
    // Submit/cancel are intents, not acknowledgements that Gateway state was
    // updated. Keep the trusted editor and its opaque selection binding alive
    // until the Control UI explicitly closes this exact annotation after the
    // corresponding update/discard RPC succeeds. This also leaves an empty
    // submit or a failed RPC recoverable in the same trusted editor.
  }

  private failAnnotationOverlay(
    record: NativeWorkbenchSurfaceRecord,
    annotationId: string,
    reason: string,
  ): void {
    if (record.annotationCandidate) {
      this.stopAnnotationGeometryWatcher(record.annotationCandidate)
    }
    const overlay = this.annotationOverlays.get(record.owner)
    if (overlay?.binding?.record === record) {
      this.closeAnnotationOverlayBinding(overlay, false)
    }
    record.annotationFallbackActive = true
    this.setPhysicalVisibility(record, false)
    this.emit(record, 'annotation-overlay-fallback', { annotationId, reason })
  }

  private closeAnnotationOverlayBinding(
    overlay: NativeWorkbenchAnnotationOverlayRecord,
    destroy: boolean,
  ): void {
    this.clearAnnotationOverlayFocusTimer(overlay)
    const binding = overlay.binding
    overlay.binding = null
    if (binding) {
      try {
        binding.port.close()
      } catch {}
      try {
        if (
          !binding.record.owner.isDestroyed()
          && !binding.record.owner.webContents.isDestroyed()
        ) binding.record.owner.webContents.focus()
      } catch {}
    }
    try {
      overlay.view.setVisible(false)
    } catch {}
    if (destroy) void this.disposeAnnotationOverlay(overlay)
  }

  private async disposeAnnotationOverlay(
    overlay: NativeWorkbenchAnnotationOverlayRecord,
  ): Promise<void> {
    if (overlay.disposed) return
    overlay.disposed = true
    this.closeAnnotationOverlayBinding(overlay, false)
    if (this.annotationOverlays.get(overlay.owner) === overlay) {
      this.annotationOverlays.delete(overlay.owner)
    }
    try {
      if (!overlay.owner.isDestroyed()) overlay.owner.contentView.removeChildView(overlay.view)
    } catch {}
    try {
      if (!overlay.view.webContents.isDestroyed()) {
        overlay.view.webContents.close({ waitForBeforeUnload: false })
      }
    } catch {}
    await Promise.allSettled([
      overlay.previewSession.clearStorageData(),
      overlay.previewSession.clearCache(),
      overlay.previewSession.clearAuthCache(),
    ])
  }

  async navigateSurface(
    request: NativeWorkbenchNavigationRequest,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const record = this.surfaces.get(request.surfaceId)
    if (!record || record.disposed) {
      return { ok: false, message: 'The native Workbench surface no longer exists.' }
    }
    if (record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION) {
      return { ok: false, message: 'This native Workbench surface does not support navigation.' }
    }
    if (record.crashed || record.view.webContents.isDestroyed()) {
      return { ok: false, message: 'The native Workbench surface renderer crashed.' }
    }
    const contents = record.view.webContents
    if (
      record.kind === 'artifact-preview'
      && request.action !== 'stop'
      && request.action !== 'open-external'
    ) {
      await this.cancelAnnotationInteraction(record, 'surface-navigation', true)
    }
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
    if (record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION) {
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
    if (pending) {
      // destroyAll and ordinary close share this queue-external fence so a
      // blocked searchForNode cannot complete successfully while disposal is
      // waiting for earlier work on the same surface.
      pending.annotationPickerEpoch += 1
      this.cancelPendingAuthentication(pending)
    }
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

  private destroyRecord(
    record: NativeWorkbenchSurfaceRecord,
  ): Promise<void> {
    if (record.cleanupPromise) return record.cleanupPromise
    const isCurrent = this.surfaces.get(record.id) === record
    if (isCurrent) this.surfaces.delete(record.id)
    if (isCurrent && this.activeSurfaceId === record.id) this.activeSurfaceId = null
    void this.cancelAnnotationInteraction(record, 'surface-closed', true)
    record.disposed = true
    if (record.revisionTimer) clearTimeout(record.revisionTimer)
    record.revisionTimer = null
    record.revisionRequest?.abort()
    record.revisionRequest = null
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
          record.debuggerExpectedDetach = true
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

  private async cleanupDisposedRecord(
    record: NativeWorkbenchSurfaceRecord,
  ): Promise<void> {
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
    await Promise.allSettled(
      [...this.annotationOverlays.values()].map(overlay => this.disposeAnnotationOverlay(overlay)),
    )
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
    }
    previewSession.webRequest.onHeadersReceived(
      { urls: ['<all_urls>'] },
      (details, callback) => {
        if (record.mode !== 'offline') {
          // Electron rejects an explicit `undefined` responseHeaders value on
          // some opaque/data responses. Preserve the response untouched while
          // using the API's empty-details form when no header map exists.
          callback(
            details.responseHeaders === undefined
              ? {}
              : { responseHeaders: details.responseHeaders },
          )
          return
        }
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
        && nativeWorkbenchMissingResourceIsLocal(
          details.url,
          record.expectedOrigin ?? undefined,
        )
        && !record.missingResourceReported
      ) {
        record.missingResourceReported = true
        this.emit(record, 'missing-resource', { reason: 'http-error' })
      }
    })
    previewSession.webRequest.onErrorOccurred({ urls: ['<all_urls>'] }, details => {
      if (
        details.resourceType !== 'mainFrame'
        && nativeWorkbenchMissingResourceIsLocal(
          details.url,
          record.expectedOrigin ?? undefined,
        )
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
    if (record.offlineRealmGuardInstalled) return
    const setState = (installed: boolean, scriptId: string | null): void => {
      record.offlineRealmGuardInstalled = installed
      record.offlineRealmGuardScriptId = scriptId
    }
    const contents = record.view.webContents
    // A newly-created WebContentsView has no renderer target until its first
    // navigation. Materialize a trusted empty document before attaching CDP;
    // the untrusted artifact is loaded only after the guard is registered.
    if (!contents.getURL()) await contents.loadURL('about:blank')
    await this.ensureDebuggerAttached(record)
    await this.cdpCommand(record, 'Page.enable')
    let scriptId: string | null = null
    try {
      const installed = await this.cdpCommand(record, 'Page.addScriptToEvaluateOnNewDocument', {
        source: NATIVE_WORKBENCH_OFFLINE_REALM_GUARD,
        runImmediately: true,
      }) as { identifier?: unknown }
      scriptId = (
        typeof installed.identifier === 'string' && installed.identifier
          ? installed.identifier
          : null
      )
      const verification = await this.cdpCommand(record, 'Runtime.evaluate', {
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
      setState(true, scriptId)
    } catch (error) {
      // The script is installed before the verification query runs.  Remove
      // it on a failed setup so a later preview cannot inherit a
      // half-installed guard.  Keep the record marker if removal itself
      // fails; the caller's bind rollback and the normal surface teardown can
      // retry the cleanup.
      if (scriptId) {
        setState(true, scriptId)
        try {
          await this.cdpCommand(record, 'Page.removeScriptToEvaluateOnNewDocument', {
            identifier: scriptId,
          })
          setState(false, null)
        } catch {
          // Preserve the identifier for a subsequent cleanup attempt.
        }
      }
      throw error
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
      if (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION) {
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
      if ((record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION)) {
        void this.cancelAnnotationInteraction(record, 'surface-navigation', true)
      }
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
      if ((record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION)) {
        void this.cancelAnnotationInteraction(record, 'surface-redirect', true)
      }
      if (
        record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION
        || !this.v2TopLevelNavigationAllowed(record, targetUrl)
      ) {
        event.preventDefault()
        if (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION) {
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
    if (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION) {
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
      'did-start-navigation',
      (_event, _targetUrl, _isInPlace, isMainFrame) => {
        if (!isMainFrame || !(record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION)) return
        if (
          record.kind !== 'artifact-html'
        ) {
          record.browserDocumentReady = false
          record.browserRuntimeException = false
          record.missingResourceReported = false
          record.blockedNetworkReported = false
          record.privilegedOriginReported = false
        }
        record.annotationDocumentGeneration += 1
        this.invalidateBrowserAnchors(record)
        void this.cancelAnnotationInteraction(record, 'surface-navigation', true)
      },
    )
    contents.on(
      'did-frame-navigate',
      (_event, targetUrl, httpResponseCode, _httpStatusText, isMainFrame) => {
        if (isMainFrame && targetUrl === record.documentUrl) {
          record.initialDocumentCommitted = true
        }
        if (isMainFrame && record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION) {
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
      const devToolsShortcut = record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION
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
      if (
        record.kind !== 'artifact-html'
      ) {
        record.browserDocumentReady = false
        record.browserRuntimeException = false
        record.missingResourceReported = false
        record.blockedNetworkReported = false
        record.privilegedOriginReported = false
        this.invalidateBrowserAnchors(record)
      }
      this.emit(record, 'loading')
      this.emitNavigationState(record)
    })
    contents.on('did-stop-loading', () => this.emitNavigationState(record))
    contents.on('page-title-updated', () => this.emitNavigationState(record))
    contents.on('did-navigate-in-page', () => {
      if ((record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION)) {
        record.annotationDocumentGeneration += 1
        this.invalidateBrowserAnchors(record)
        void this.cancelAnnotationInteraction(record, 'surface-navigation', true)
      }
      this.emitNavigationState(record)
    })
    contents.on('did-finish-load', () => {
      if (
        record.kind !== 'artifact-html'
      ) {
        record.browserDocumentReady = true
      }
      record.initialDocumentCommitted = true
      record.authenticationAttempts.clear()
      this.emit(record, 'ready')
      this.emitNavigationState(record)
    })
    contents.on('did-fail-load', (_event, errorCode, errorDescription, _url, isMainFrame) => {
      if (!isMainFrame || record.disposed || errorCode === -3) return
      if (
        record.kind !== 'artifact-html'
      ) {
        record.browserDocumentReady = false
        record.browserRuntimeException = true
        this.invalidateBrowserAnchors(record)
      }
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
      record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION
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
    const overlay = this.annotationOverlays.get(record.owner)
    if (
      overlay?.binding?.record === record
      && record.annotationCandidate
      && !record.annotationFallbackActive
    ) {
      this.presentAnnotationOverlay(
        overlay,
        this.annotationOverlayBounds(record, record.annotationCandidate),
        this.ownerCanShowSurfaces(record.owner),
        true,
      )
    }
  }

  private hideRecord(record: NativeWorkbenchSurfaceRecord): void {
    void this.cancelAnnotationInteraction(record, 'surface-hidden', true)
    // A queued hide can arrive after a replacement has already installed a
    // new record under the same surface id. Never let the stale record clear
    // the replacement's active binding; hiding the stale record itself is safe.
    const isCurrentRecord = this.surfaces.get(record.id) === record
    if (isCurrentRecord && this.activeSurfaceId === record.id) {
      this.activeSurfaceId = null
    }
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
      record.view.setVisible(visible && !record.annotationFallbackActive)
      const overlay = this.annotationOverlays.get(record.owner)
      if (
        overlay?.binding?.record === record
        && overlay.view.getVisible() !== visible
      ) overlay.view.setVisible(visible)
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
    const overlay = this.annotationOverlays.get(owner)
    if (
      overlay?.binding?.record === record
      && record.annotationCandidate
      && !record.annotationFallbackActive
    ) {
      this.presentAnnotationOverlay(
        overlay,
        this.annotationOverlayBounds(record, record.annotationCandidate),
        this.ownerCanShowSurfaces(owner),
        true,
      )
    }
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
    try {
      this.annotationOverlays.get(owner)?.view.setVisible(false)
    } catch {}
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
    this.setPhysicalVisibility(record, false)
    this.invalidateBrowserAnchors(record)
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
