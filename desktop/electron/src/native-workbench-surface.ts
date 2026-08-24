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
  nativeWorkbenchV2NetworkUrlAllowed,
  type NativeWorkbenchCreateRequest,
  type NativeWorkbenchNavigationRequest,
  type NativeWorkbenchPermissionResponse,
  type NativeWorkbenchPreviewMode,
  type NativeWorkbenchSurfaceEvent,
  type NativeWorkbenchSurfaceRect,
  type NativeWorkbenchSurfaceRectRequest,
} from './native-workbench-surface-contract.js'
import type {
  DesktopArtifactBrowserActRequest,
  DesktopArtifactBrowserActResult,
  DesktopArtifactBrowserInspectRequest,
  DesktopArtifactBrowserSnapshot,
  DesktopArtifactFocusAnnotationRequest,
} from './desktop-artifact-bridge-contract.js'
import type {
  DesktopArtifactBridgeTarget,
  DesktopArtifactBridgeTargetBinding,
} from './desktop-artifact-bridge.js'
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
  version: NativeWorkbenchCreateRequest['version']
  kind: NativeWorkbenchCreateRequest['kind']
  mode: NativeWorkbenchPreviewMode
  canonicalMode: NativeWorkbenchPreviewMode
  scopeId: string
  activePreviewArtifactId: string | null
  handle: string | null
  documentUrl: string
  expectedOrigin: string | null
  canonicalDocumentUrl: string
  canonicalExpectedOrigin: string | null
  canonicalPreviewArtifactId: string | null
  candidatePreview: {
    handle: string
    leaseId: string
    artifactId: string
  } | null
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
  artifactBridgePins: number
  uiReleaseRequested: boolean
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
  /** True only after the current v4 preview navigation reaches did-finish-load. */
  browserDocumentReady: boolean
  /** Set by CDP Runtime.exceptionThrown until the next successful navigation. */
  browserRuntimeException: boolean
  /** Legacy WebRTC-only guard for an offline canonical preview. */
  offlineRealmGuardInstalled: boolean
  /** CDP id for the legacy WebRTC-only guard. */
  offlineRealmGuardScriptId: string | null
  /** Candidate-only network side-effect guard layered over the WebRTC guard. */
  candidateOfflineRealmGuardInstalled: boolean
  /** CDP id for the candidate-only network side-effect guard. */
  candidateOfflineRealmGuardScriptId: string | null
  /**
   * Anchors issued by the last browserInspect call.  They are intentionally
   * held in the main process and are invalidated on every top-level or
   * in-page navigation and whenever a fresh snapshot is requested.
   */
  browserAnchors: Map<string, NativeWorkbenchBrowserAnchor>
  browserAnchorGeneration: number
  cdpQueue: Promise<void>
  cdpReady: boolean
  debuggerExpectedDetach: boolean
}

interface NativeWorkbenchArtifactBridgeBindingState {
  record: NativeWorkbenchSurfaceRecord
  target: DesktopArtifactBridgeTarget
  previewPin: NativeWorkbenchArtifactPreviewPin
  generation: number
  recoveryAttempted: boolean
  terminal: boolean
  released: boolean
  candidateHandle: string | null
}

interface NativeWorkbenchArtifactPreviewGrant {
  launchUrl: string
  expectedOrigin: string
  scopeId: string
  mode: NativeWorkbenchPreviewMode
}

interface NativeWorkbenchArtifactPreviewPin {
  currentGrant(): NativeWorkbenchArtifactPreviewGrant
  ensureCurrent(): Promise<NativeWorkbenchArtifactPreviewGrant | null>
  release(): Promise<void>
}

interface NativeWorkbenchBrowserAnchor {
  elementPath: string
  documentGeneration: number
  anchorGeneration: number
  surfaceId: string
  scopeId: string
  activePreviewArtifactId: string | null
  /**
   * The opaque Gateway handle transitively binds this anchor to the
   * candidate SHA/epoch.  The native process deliberately does not receive
   * those source-level values, but it must still compare the handle when an
   * anchor is consumed.
   */
  candidateHandle: string | null
  expiresAt: number
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
const nativeWorkbenchOfflineRealmGuardSource = (blockNetworkSideEffects: boolean): string => `(() => {
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
  ${blockNetworkSideEffects ? `
  const deny = () => { throw new Error('offline preview side effect denied') }
  for (const name of ['fetch', 'WebSocket', 'EventSource', 'XMLHttpRequest']) {
    try {
      Object.defineProperty(globalThis, name, {
        configurable: false,
        enumerable: false,
        value: deny,
        writable: false,
      })
    } catch {}
  }
  try { Object.defineProperty(navigator, 'sendBeacon', { value: deny, configurable: false }) } catch {}
  try {
    Object.defineProperty(globalThis, 'open', { value: deny, configurable: false })
    Object.defineProperty(HTMLFormElement.prototype, 'submit', { value: deny, configurable: false })
    Object.defineProperty(HTMLFormElement.prototype, 'requestSubmit', { value: deny, configurable: false })
  } catch {}
  ` : ''}
})()`
const NATIVE_WORKBENCH_OFFLINE_REALM_GUARD = nativeWorkbenchOfflineRealmGuardSource(false)
const NATIVE_WORKBENCH_CANDIDATE_OFFLINE_REALM_GUARD =
  nativeWorkbenchOfflineRealmGuardSource(true)
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

// This is the only JavaScript the annotation picker may execute. No caller
// value is interpolated into it. It runs in a main-frame isolated world and
// returns only a bounded structural fingerprint and geometry candidate.
const NATIVE_WORKBENCH_ANNOTATION_INSPECT_FUNCTION = `function () {
  const selected = this
  if (
    window.top !== window
    || !(selected instanceof Element)
    || !selected.isConnected
    || selected.ownerDocument !== document
    || selected.getRootNode() !== document
  ) return { ok: false, reason: 'unsupported-node' }

  const htmlNamespace = 'http://www.w3.org/1999/xhtml'
  const normalizedNamespace = node => node.namespaceURI === htmlNamespace
    ? ''
    : (node.namespaceURI || '')
  const compareJsonKeysByCodePoint = (left, right) => {
    const leftPoints = Array.from(JSON.stringify(left), value => value.codePointAt(0))
    const rightPoints = Array.from(JSON.stringify(right), value => value.codePointAt(0))
    const length = Math.min(leftPoints.length, rightPoints.length)
    for (let index = 0; index < length; index += 1) {
      if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index]
    }
    return leftPoints.length - rightPoints.length
  }
  const segments = []
  const proofTokens = []
  let current = selected
  while (current) {
    if (segments.length >= 128) return { ok: false, reason: 'path-too-deep' }
    const namespace = normalizedNamespace(current)
    const tagName = (current.localName || current.tagName || '').toLowerCase()
    let index = 1
    for (let sibling = current.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
      if (
        normalizedNamespace(sibling) === namespace
        && (sibling.localName || sibling.tagName || '').toLowerCase() === tagName
      ) index += 1
    }
    segments.unshift([namespace, tagName, index])
    const attributes = Array.from(current.attributes, attribute => [
      attribute.namespaceURI || '',
      attribute.localName || attribute.name,
      attribute.value,
    ]).sort(compareJsonKeysByCodePoint)
    proofTokens.unshift([namespace, tagName, index, attributes])
    current = current.parentElement
  }
  const elementPath = JSON.stringify(segments)
  if (!elementPath || elementPath.length > 4096) {
    return { ok: false, reason: 'path-too-large' }
  }
  const proofEncoded = new TextEncoder().encode(
    proofTokens.map(token => JSON.stringify(token)).join('\\n'),
  )
  if (proofEncoded.byteLength > 4194304) {
    return { ok: false, reason: 'element-proof-too-large' }
  }

  const rect = selected.getBoundingClientRect()
  const viewport = window.visualViewport
  return crypto.subtle.digest('SHA-256', proofEncoded).then(elementProofBuffer => ({
    ok: true,
    tagName: (selected.localName || selected.tagName || '').toLowerCase(),
    elementPath,
    elementProofSha256: Array.from(
      new Uint8Array(elementProofBuffer),
      byte => byte.toString(16).padStart(2, '0'),
    ).join(''),
    rect: {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
    },
    viewportWidth: viewport ? viewport.width : window.innerWidth,
    viewportHeight: viewport ? viewport.height : window.innerHeight,
  }))
}`

// Geometry refresh deliberately avoids re-running the element proof. The full
// fixed inspector is re-run before opening the editor and again when Gateway
// resolves the opaque selection.
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

// The authenticated Gateway supplies a server-revalidated canonical element
// path. This fixed function has no selector or JavaScript input surface: it
// walks element children by namespace, local name and 1-based sibling index.
const NATIVE_WORKBENCH_ANNOTATION_FIND_BY_PATH_FUNCTION = `function (elementPath) {
  if (window.top !== window || this !== document.documentElement) return null
  let segments
  try { segments = JSON.parse(elementPath) } catch { return null }
  if (!Array.isArray(segments) || segments.length < 1 || segments.length > 128) return null
  const htmlNamespace = 'http://www.w3.org/1999/xhtml'
  const normalizedNamespace = node => node.namespaceURI === htmlNamespace
    ? ''
    : (node.namespaceURI || '')
  const matches = (node, segment) => Array.isArray(segment)
    && segment.length === 3
    && typeof segment[0] === 'string'
    && typeof segment[1] === 'string'
    && Number.isSafeInteger(segment[2])
    && segment[2] >= 1
    && normalizedNamespace(node) === segment[0]
    && (node.localName || node.tagName || '').toLowerCase() === segment[1]
  let current = this
  if (!matches(current, segments[0]) || segments[0][2] !== 1) return null
  for (let depth = 1; depth < segments.length; depth += 1) {
    const segment = segments[depth]
    let index = 0
    let matched = null
    for (const child of current.children) {
      if (!matches(child, segment)) continue
      index += 1
      if (index === segment[2]) {
        matched = child
        break
      }
    }
    if (!matched) return null
    current = matched
  }
  return current
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

// Browser-agent inspection/actions deliberately use fixed function bodies.
// The model may supply only an opaque anchor and bounded scalar arguments;
// there is no selector, URL, JavaScript expression or CDP method surface.
const NATIVE_WORKBENCH_BROWSER_SNAPSHOT_FUNCTION = `function (scope, maxNodes) {
  if (window.top !== window || !document.documentElement) {
    return { ok: false, reason: 'unsupported-preview' }
  }
  const htmlNamespace = 'http://www.w3.org/1999/xhtml'
  const normalizedNamespace = node => node.namespaceURI === htmlNamespace
    ? ''
    : (node.namespaceURI || '')
  const pathFor = selected => {
    const segments = []
    let current = selected
    while (current) {
      if (segments.length >= 128) return null
      const namespace = normalizedNamespace(current)
      const tagName = (current.localName || current.tagName || '').toLowerCase()
      let index = 1
      for (let sibling = current.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (
          normalizedNamespace(sibling) === namespace
          && (sibling.localName || sibling.tagName || '').toLowerCase() === tagName
        ) index += 1
      }
      segments.unshift([namespace, tagName, index])
      current = current.parentElement
    }
    return JSON.stringify(segments)
  }
  const textFor = node => {
    const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim()
    return text ? text.slice(0, 512) : undefined
  }
  const nameFor = node => {
    const aria = node.getAttribute('aria-label')
    if (aria) return aria.slice(0, 256)
    const title = node.getAttribute('title')
    if (title) return title.slice(0, 256)
    return textFor(node)
  }
  const interactiveFor = node => {
    const tag = (node.localName || '').toLowerCase()
    return ['a', 'button', 'input', 'select', 'textarea', 'option', 'summary'].includes(tag)
      || node.hasAttribute('contenteditable')
      || node.hasAttribute('tabindex')
      || node.getAttribute('role') === 'button'
  }
  const visible = node => {
    const style = getComputedStyle(node)
    if (style.display === 'none' || style.visibility === 'hidden') return false
    const rect = node.getBoundingClientRect()
    return rect.width > 0 && rect.height > 0
  }
  const viewport = window.visualViewport
  const viewportWidth = viewport ? viewport.width : window.innerWidth
  const viewportHeight = viewport ? viewport.height : window.innerHeight
  const active = document.activeElement instanceof Element ? document.activeElement : null
  const elements = scope === 'selection' && active
    ? [active]
    : document.querySelectorAll('*')
  // Do not allocate/inspect an unbounded DOM just because a generated page
  // contains a large hidden tree.  The snapshot is intentionally a bounded
  // diagnostic surface; callers can request another viewport/selection slice
  // when the first bounded result is truncated.
  const scanLimit = scope === 'selection'
    ? elements.length
    : Math.min(elements.length, Math.max(maxNodes * 20, 1_000))
  const nodes = []
  let truncated = false
  for (let index = 0; index < scanLimit; index += 1) {
    const node = elements[index]
    if (!(node instanceof Element) || !visible(node)) continue
    const rect = node.getBoundingClientRect()
    if (scope === 'viewport' && (
      rect.bottom <= 0 || rect.right <= 0
      || rect.top >= viewportHeight || rect.left >= viewportWidth
    )) continue
    const elementPath = pathFor(node)
    if (!elementPath || elementPath.length > 4096) continue
    const anchor = 'a' + String(nodes.length + 1)
    const roleValue = node.getAttribute('role')
    const role = roleValue
      ? roleValue.slice(0, 256)
      : ((node.localName || '').toLowerCase() === 'button' ? 'button' : undefined)
    nodes.push({
      anchor,
      elementPath,
      role,
      name: nameFor(node),
      text: textFor(node),
      interactive: interactiveFor(node),
      disabled: node.hasAttribute('disabled') || node.getAttribute('aria-disabled') === 'true',
      selected: node.getAttribute('aria-selected') === 'true' || node.matches(':checked'),
    })
    if (nodes.length >= maxNodes) {
      truncated = true
      break
    }
  }
  if (elements.length > scanLimit) truncated = true
  return { ok: true, scope, nodes, truncated }
}`

const NATIVE_WORKBENCH_BROWSER_CLICK_FUNCTION = `function (focusOnly) {
  if (
    window.top !== window
    || !(this instanceof Element)
    || !this.isConnected
    || this.ownerDocument !== document
    || this.getRootNode() !== document
  ) return { ok: false, reason: 'unsupported-node' }
  if (this.matches(':disabled,[aria-disabled="true"]')) return { ok: false, reason: 'disabled-node' }
  const tag = (this.localName || '').toLowerCase()
  const inputType = (this.getAttribute('type') || '').toLowerCase()
  // Do not let an agent click a navigation link or submit a form.  The
  // browser surface is for local preview verification; network/form side
  // effects must remain user-driven.
  if (!focusOnly && (
      this.closest('form')
      || this.hasAttribute('formaction')
      || this.hasAttribute('download')
      || tag === 'a'
      || (tag === 'button' && inputType !== 'button')
      || (tag === 'input' && ['submit', 'image', 'file'].includes(inputType))
      || Array.from(this.attributes).some(attribute => attribute.name.toLowerCase().startsWith('on'))
  )) return { ok: false, reason: 'side-effect-node' }
  this.focus({ preventScroll: true })
  if (!focusOnly) this.click()
  return { ok: true, changed: !focusOnly }
}`

const NATIVE_WORKBENCH_BROWSER_TYPE_FUNCTION = `function (text, replace) {
  if (
    window.top !== window
    || !(this instanceof Element)
    || !this.isConnected
    || this.ownerDocument !== document
    || this.getRootNode() !== document
  ) return { ok: false, reason: 'unsupported-node' }
  if (this.matches(':disabled,[aria-disabled="true"]')) return { ok: false, reason: 'disabled-node' }
  if (Array.from(this.attributes).some(attribute => attribute.name.toLowerCase().startsWith('on'))) {
    return { ok: false, reason: 'side-effect-node' }
  }
  this.focus({ preventScroll: true })
  const tag = (this.localName || '').toLowerCase()
  if (tag === 'input' || tag === 'textarea') {
    const control = this
    const inputType = (control.getAttribute('type') || 'text').toLowerCase()
    if (tag === 'input' && ['file', 'button', 'submit', 'image', 'reset', 'checkbox', 'radio', 'hidden'].includes(inputType)) {
      return { ok: false, reason: 'not-text-input' }
    }
    const before = String(control.value || '')
    if (replace) control.select()
    const start = replace ? 0 : (control.selectionStart ?? before.length)
    const end = replace ? before.length : (control.selectionEnd ?? start)
    control.setRangeText(text, start, end, 'end')
    control.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }))
    control.dispatchEvent(new Event('change', { bubbles: true }))
    return { ok: true, changed: before !== String(control.value || '') }
  }
  if (this.isContentEditable) {
    const selection = window.getSelection()
    if (replace && selection) {
      const range = document.createRange()
      range.selectNodeContents(this)
      selection.removeAllRanges()
      selection.addRange(range)
    }
    if (document.execCommand) document.execCommand('insertText', false, text)
    else this.textContent = replace ? text : String(this.textContent || '') + text
    this.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }))
    return { ok: true, changed: true }
  }
  return { ok: false, reason: 'not-text-input' }
}`

const NATIVE_WORKBENCH_BROWSER_PRESS_FUNCTION = `function (key) {
  if (window.top !== window || !document) return { ok: false, reason: 'unsupported-preview' }
  const target = document.activeElement || document.body
  const allowed = ['Enter','Tab','Escape','Backspace','Delete','Space','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown']
  if (!allowed.includes(key)) return { ok: false, reason: 'unsupported-key' }
  if (key === 'Enter' && target instanceof Element && (
    target.closest('form')
      || target.matches('button,a,[role="button"],input[type="submit"],input[type="image"]')
      || Array.from(target.attributes).some(attribute => attribute.name.toLowerCase().startsWith('on'))
  )) {
    return { ok: false, reason: 'side-effect-node' }
  }
  const normalized = key === 'Space' ? ' ' : key
  const eventInit = { key: normalized, code: normalized === ' ' ? 'Space' : normalized, bubbles: true, cancelable: true }
  const down = target.dispatchEvent(new KeyboardEvent('keydown', eventInit))
  target.dispatchEvent(new KeyboardEvent('keypress', eventInit))
  target.dispatchEvent(new KeyboardEvent('keyup', eventInit))
  return { ok: true, changed: down }
}`

const NATIVE_WORKBENCH_BROWSER_SCROLL_FUNCTION = `function (direction, amount) {
  if (window.top !== window || !document.scrollingElement) return { ok: false, reason: 'unsupported-preview' }
  const distance = amount === 'page' ? Math.max(1, window.innerHeight * 0.8) : 160
  const dx = direction === 'left' ? -distance : direction === 'right' ? distance : 0
  const dy = direction === 'up' ? -distance : direction === 'down' ? distance : 0
  document.scrollingElement.scrollBy({ left: dx, top: dy, behavior: 'auto' })
  return { ok: true, changed: dx !== 0 || dy !== 0 }
}`

const NATIVE_WORKBENCH_BROWSER_ANCHOR_TTL_MS = 60_000

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
}

export interface NativeWorkbenchSurfaceManagerOptions {
  authenticationTimeoutMs?: number
  getPrivilegedGatewayUrl?(): string | null
  getWindow(): BrowserWindow | null
  emit(event: NativeWorkbenchSurfaceEvent): void
  forceArtifactPreviewsOffline?: boolean
  permissionTimeoutMs?: number
  resolveCandidatePreview?: (
    candidateHandle: string,
    signal: AbortSignal,
  ) => Promise<NativeWorkbenchCandidatePreviewBinding>
  releaseCandidatePreview?: (
    candidateHandle: string,
    signal: AbortSignal,
  ) => Promise<void>
  pinArtifactPreview?: (
    grant: NativeWorkbenchArtifactPreviewGrant,
  ) => NativeWorkbenchArtifactPreviewPin | null
}

export interface NativeWorkbenchCandidatePreviewBinding {
  candidateHandle: string
  candidateArtifactId: string
  leaseId: string
  launchUrl: string
  expectedOrigin: string
  scopeId: string
  mode: NativeWorkbenchPreviewMode
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function isArtifactBridgeProtocolVersion(
  version: NativeWorkbenchSurfaceRecord['version'],
): boolean {
  return version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
    || version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
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
  private readonly artifactBridgeBindings = new Map<
    string,
    NativeWorkbenchArtifactBridgeBindingState
  >()
  private activeSurfaceId: string | null = null

  constructor(private readonly options: NativeWorkbenchSurfaceManagerOptions) {}

  async createSurface(
    request: NativeWorkbenchCreateRequest,
    activePreviewArtifactId: string | null = null,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const pending = this.surfaces.get(request.surfaceId)
    if (pending) this.cancelPendingAuthentication(pending)
    return await this.queueSurfaceOperation(
      request.surfaceId,
      () => this.createSurfaceNow(request, activePreviewArtifactId),
    )
  }

  private async createSurfaceNow(
    request: NativeWorkbenchCreateRequest,
    activePreviewArtifactId: string | null,
  ): Promise<NativeWorkbenchSurfaceResult> {
    const previous = this.surfaces.get(request.surfaceId)
    if (previous?.artifactBridgePins) {
      return {
        ok: false,
        retryable: true,
        code: 'AGENT_EDIT_IN_PROGRESS',
        message: 'Agent editing is continuing in the background.',
      }
    }
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
      canonicalMode: mode,
      scopeId: request.payload.scopeId,
      activePreviewArtifactId: (
        request.kind === 'artifact-preview'
        && /^art-[A-Za-z0-9_-]{1,200}$/.test(activePreviewArtifactId || '')
      ) ? activePreviewArtifactId : null,
      handle,
      documentUrl,
      expectedOrigin,
      canonicalDocumentUrl: documentUrl,
      canonicalExpectedOrigin: expectedOrigin,
      canonicalPreviewArtifactId: (
        request.kind === 'artifact-preview'
        && /^art-[A-Za-z0-9_-]{1,200}$/.test(activePreviewArtifactId || '')
      ) ? activePreviewArtifactId : null,
      candidatePreview: null,
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
      artifactBridgePins: 0,
      uiReleaseRequested: false,
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
      browserDocumentReady: false,
      browserRuntimeException: false,
      offlineRealmGuardInstalled: false,
      offlineRealmGuardScriptId: null,
      candidateOfflineRealmGuardInstalled: false,
      candidateOfflineRealmGuardScriptId: null,
      browserAnchors: new Map(),
      browserAnchorGeneration: 0,
      cdpQueue: Promise.resolve(),
      cdpReady: false,
      debuggerExpectedDetach: false,
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
      if (
        isArtifactBridgeProtocolVersion(request.version)
        && request.kind === 'artifact-preview'
      ) {
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
        reason: 'No active protocol-v4 HTML artifact preview is available.',
      }
    }
    if (!record.cdpReady || !record.view.webContents.debugger.isAttached()) {
      return {
        version: annotationVersion,
        available: false,
        picker: false,
        trustedOverlay: false,
        overlayCopyVersion: 1,
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
        reason: 'The trusted annotation editor is unavailable.',
      }
    }
    return {
      version: annotationVersion,
      available: true,
      picker: true,
      trustedOverlay: true,
      overlayCopyVersion: 1,
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
        message: 'Only the active protocol-v4 HTML artifact preview supports annotations.',
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
    record.annotationPickerActive = true
    try {
      await this.cdpCommand(record, 'Overlay.setInspectMode', {
        mode: 'searchForNode',
        highlightConfig: NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG,
      })
      if (!this.isActiveAnnotationRecord(record) || !record.annotationPickerActive) {
        throw new Error('The annotation picker was cancelled before it became active.')
      }
      return { ok: true }
    } catch (error) {
      record.annotationPickerActive = false
      const cleanupFailure = await this.clearAnnotationInspectState(record, true)
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

  closeArtifactAnnotationOverlay(
    request: NativeWorkbenchAnnotationOverlayCloseRequest,
  ): NativeWorkbenchSurfaceResult {
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
    if (overlay) this.closeAnnotationOverlayBinding(overlay, false)
    record.annotationFallbackActive = false
    this.clearAnnotationCandidate(record)
    if (
      this.activeSurfaceId === record.id
      && this.surfaces.get(record.id) === record
      && isArtifactBridgeProtocolVersion(record.version)
      && record.kind === 'artifact-preview'
      && !record.disposed
      && !record.crashed
      && record.visibleRequested
    ) {
      this.setPhysicalVisibility(record, this.ownerCanShowSurfaces(record.owner))
    }
    return { ok: true }
  }

  /**
   * Returns a capability-scoped binding to the UI-selected active surface.
   * The binding intentionally carries no public surface identifier. Protocol
   * v1/v2 surfaces cannot be upgraded into agent-control surfaces implicitly.
   */
  getActiveArtifactBridgeTarget(): DesktopArtifactBridgeTarget | null {
    if (!this.activeSurfaceId) return null
    const record = this.surfaces.get(this.activeSurfaceId)
    if (
      !record
      || !isArtifactBridgeProtocolVersion(record.version)
      || record.disposed
      || record.crashed
      || !record.visibleRequested
      || !record.view.getVisible()
      || record.view.webContents.isDestroyed()
    ) return null
    return this.artifactBridgeTargetForRecord(record)
  }

  private artifactBridgeTargetForRecord(
    record: NativeWorkbenchSurfaceRecord,
  ): DesktopArtifactBridgeTarget {
    return {
      protocolVersion: record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
        ? NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
        : NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
      isCurrent: () => this.isActiveArtifactBridgeRecord(record),
      capabilities: {
        captureSelection: false,
        resolveAnnotationSelection: (
          record.kind === 'artifact-preview'
          && record.activePreviewArtifactId !== null
          && record.cdpReady
          && record.view.webContents.debugger.isAttached()
        ),
        focusAnnotation: (
          record.kind === 'artifact-preview'
          && record.activePreviewArtifactId !== null
          && record.cdpReady
          && record.view.webContents.debugger.isAttached()
        ),
        browserInspect: (
          record.kind === 'artifact-preview'
          && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
          && record.activePreviewArtifactId !== null
          && record.browserDocumentReady
          && record.cdpReady
          && record.view.webContents.debugger.isAttached()
        ),
        browserAct: (
          record.kind === 'artifact-preview'
          && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
          // Agent actions are only safe against an opaque, turn-local
          // candidate rendered in the forced-offline realm.  A canonical
          // preview may be user-selected full mode and its event handlers can
          // carry application side effects; exposing click/type/press there
          // would bypass the candidate-loop safety boundary.
          && record.candidatePreview !== null
          && record.activePreviewArtifactId === record.candidatePreview.artifactId
          && record.mode === 'offline'
          && record.activePreviewArtifactId !== null
          && record.browserDocumentReady
          && record.cdpReady
          && record.view.webContents.debugger.isAttached()
        ),
        // Screenshot/reload remain available to the v3 annotation path for
        // compatibility.  The autonomous browser inspection/action surface
        // above is still v4-only.
        screenshot: (
          record.kind === 'artifact-preview'
          && record.activePreviewArtifactId !== null
          && (
            record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
            || (
              record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
              && record.browserDocumentReady
              && record.cdpReady
              && record.view.webContents.debugger.isAttached()
            )
          )
        ),
        officeFlush: false,
        reloadSurface: (
          record.kind === 'artifact-preview'
          && record.activePreviewArtifactId !== null
          && (
            record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V3
            || (
              record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
              && record.browserDocumentReady
              && record.cdpReady
              && record.view.webContents.debugger.isAttached()
            )
          )
        ),
        bindCandidatePreview: (
          record.kind === 'artifact-preview'
          && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
          && record.canonicalPreviewArtifactId !== null
          && record.browserDocumentReady
          && Boolean(this.options.resolveCandidatePreview)
          && record.cdpReady
          && record.view.webContents.debugger.isAttached()
        ),
        restoreCanonicalPreview: (
          record.kind === 'artifact-preview'
          && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
          && record.canonicalPreviewArtifactId !== null
          && Boolean(this.options.releaseCandidatePreview)
          && record.cdpReady
          && record.view.webContents.debugger.isAttached()
        ),
      },
      resolveAnnotationSelection: async (request, signal) => {
        this.assertActiveArtifactBridgeRecord(record, signal)
        const candidate = record.annotationCandidate
        if (
          record.kind !== 'artifact-preview'
          || !record.activePreviewArtifactId
          || request.activePreviewArtifactId !== record.activePreviewArtifactId
          || !record.cdpReady
          || !candidate
          || candidate.documentGeneration !== record.annotationDocumentGeneration
          || candidate.selection.selectionId !== request.selectionId
          || candidate.selection.tagName !== request.tagName
          || candidate.selection.elementPath !== request.elementPath
          || candidate.selection.elementProofSha256 !== request.elementProofSha256
        ) throw new Error('The Desktop artifact annotation selection is stale or mismatched.')
        await this.refreshAnnotationCandidateIntegrity(record, candidate)
        this.assertActiveArtifactBridgeRecord(record, signal)
        if (record.annotationCandidate !== candidate) {
          throw new Error('The Desktop artifact annotation selection changed.')
        }
        return {
          activePreviewArtifactId: record.activePreviewArtifactId,
          selectionId: candidate.selection.selectionId,
          tagName: candidate.selection.tagName,
          elementPath: candidate.selection.elementPath,
          ...(request.domSha256 === undefined ? {} : { domSha256: request.domSha256 }),
          elementProofSha256: candidate.selection.elementProofSha256,
          scopeId: record.scopeId,
          rect: { ...candidate.selection.rect },
        }
      },
      focusAnnotation: (request, signal) => (
        this.focusTrustedAnnotation(record, request, signal)
      ),
      browserInspect: (request, signal) => (
        this.inspectBrowser(record, request, signal)
      ),
      browserAct: (request, signal) => (
        this.actBrowser(record, request, signal)
      ),
      bindCandidatePreview: (request, signal) => (
        this.bindCandidatePreview(record, request.candidateHandle, signal)
      ),
      restoreCanonicalPreview: (request, signal) => (
        this.restoreCanonicalPreview(record, request.candidateHandle, signal)
      ),
      screenshot: async (request, signal) => {
        this.assertActiveArtifactBridgeRecord(record, signal)
        this.assertCandidateRequestBinding(record, request.candidateHandle)
        const image = await record.view.webContents.capturePage()
        this.assertActiveArtifactBridgeRecord(record, signal)
        this.assertCandidateRequestBinding(record, request.candidateHandle)
        const size = image.getSize()
        const bytes = image.toPNG()
        if (
          size.width <= 0
          || size.height <= 0
          || bytes.byteLength === 0
          || bytes.byteLength > NATIVE_WORKBENCH_MAX_SCREENSHOT_BYTES
        ) throw new Error('The active Desktop artifact screenshot is unavailable.')
        return {
          mime: 'image/png',
          data: Uint8Array.from(bytes),
          width: size.width,
          height: size.height,
        }
      },
      reloadSurface: (request, signal) => {
        this.assertActiveArtifactBridgeRecord(record, signal)
        this.assertCandidateRequestBinding(record, request.candidateHandle)
        record.browserDocumentReady = false
        this.invalidateBrowserAnchors(record)
        void this.cancelAnnotationInteraction(record, 'surface-reloaded', true)
        this.rejectPendingPermissions(record)
        this.cancelPendingAuthentication(record)
        record.authenticationAttempts.clear()
        record.view.webContents.reload()
        this.assertCandidateRequestBinding(record, request.candidateHandle)
        return { reloaded: true }
      },
    }
  }

  private async bindCandidatePreview(
    record: NativeWorkbenchSurfaceRecord,
    candidateHandle: string,
    signal: AbortSignal,
  ): Promise<{ bound: true; candidateHandle: string }> {
    this.assertActiveArtifactBridgeRecord(record, signal)
    if (
      record.kind !== 'artifact-preview'
      || record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      || record.canonicalPreviewArtifactId === null
      || !record.browserDocumentReady
      || !this.options.resolveCandidatePreview
      || !record.cdpReady
      || !record.view.webContents.debugger.isAttached()
    ) throw new Error('The active Desktop artifact preview cannot bind a candidate.')
    let binding: NativeWorkbenchCandidatePreviewBinding | null = null
    const previousCandidate = record.candidatePreview
    const previousState = {
      activePreviewArtifactId: record.activePreviewArtifactId,
      documentUrl: record.documentUrl,
      expectedOrigin: record.expectedOrigin,
      mode: record.mode,
      browserDocumentReady: record.browserDocumentReady,
    }
    const previousCandidateOfflineRealmGuardInstalled =
      record.candidateOfflineRealmGuardInstalled
    const previousCandidateOfflineRealmGuardScriptId =
      record.candidateOfflineRealmGuardScriptId
    const previousWebRtcPolicy = record.view.webContents.getWebRTCIPHandlingPolicy()
    let candidateOfflineRealmGuardAttempted = false
    let rollbackLoaded = false
    let candidateGuardCleanupFailed = false
    let previousCandidateReplaced = false
    try {
      binding = await this.options.resolveCandidatePreview(candidateHandle, signal)
      // Resolving a handle materializes a fresh Gateway lease.  For a
      // repeated turn-local handle this supersedes the previous lease even
      // before the new URL is loaded; the old surface must not be restored if
      // that load later fails.
      previousCandidateReplaced = (
        previousCandidate !== null
        && previousCandidate.handle === candidateHandle
      )
      this.assertActiveArtifactBridgeRecord(record, signal)
      if (
        binding.candidateHandle !== candidateHandle
        || binding.scopeId !== record.scopeId
        // Candidate previews are deliberately an offline-only realm.  Do not
        // trust a Gateway response that attempts to widen this to the
        // canonical/full-network mode, even when the process-level override
        // is disabled (the override is only a defence-in-depth ceiling).
        || binding.mode !== 'offline'
        || !/^art-[A-Za-z0-9_-]{1,200}$/.test(binding.candidateArtifactId)
        || !/^apl-[A-Za-z0-9_-]{1,240}$/.test(binding.leaseId)
        || !this.trustedCandidatePreviewUrl(binding.launchUrl, binding.expectedOrigin)
        || !this.samePreviewListener(record, binding.expectedOrigin)
      ) throw new Error('The candidate preview binding is invalid.')
      // The process-level offline switch is a hard ceiling, not merely the
      // default used while creating the canonical surface.  A compromised or
      // stale Gateway response must not escalate a candidate bind back to the
      // full-network preview realm.
      const effectiveCandidateMode = 'offline'
      // A turn normally reuses one opaque handle, in which case the Gateway
      // atomically rotates its lease.  If a new turn arrives before the old
      // one was cleaned up, release the old handle before replacing the
      // surface record so its lease cannot be orphaned indefinitely.  Keep
      // the old state intact if cleanup is rejected; the caller can retry
      // without losing the currently visible candidate.
      if (
        previousCandidate
        && previousCandidate.handle !== candidateHandle
      ) {
        if (!this.options.releaseCandidatePreview) {
          throw new Error('The previous candidate preview cannot be released safely.')
        }
        await this.options.releaseCandidatePreview(previousCandidate.handle, signal)
        previousCandidateReplaced = true
      }
      await this.cancelAnnotationInteraction(record, 'candidate-preview-bound', true)
      this.rejectPendingPermissions(record)
      this.cancelPendingAuthentication(record)
      record.authenticationAttempts.clear()
      record.browserDocumentReady = false
      this.invalidateBrowserAnchors(record)
      record.annotationDocumentGeneration += 1
      record.candidatePreview = {
        handle: candidateHandle,
        leaseId: binding.leaseId,
        artifactId: binding.candidateArtifactId,
      }
      record.activePreviewArtifactId = binding.candidateArtifactId
      record.documentUrl = binding.launchUrl
      record.expectedOrigin = binding.expectedOrigin
      record.mode = effectiveCandidateMode
      if (effectiveCandidateMode === 'offline') {
        // Apply the restrictive ICE policy before loading any candidate bytes.
        // If the bind rolls back, the exact policy from the canonical surface
        // is restored below.
        record.view.webContents.setWebRTCIPHandlingPolicy('disable_non_proxied_udp')
        candidateOfflineRealmGuardAttempted = !previousCandidateOfflineRealmGuardInstalled
        await this.installOfflineRealmGuard(record, true)
      }
      await record.view.webContents.loadURL(binding.launchUrl)
      this.assertActiveArtifactBridgeRecord(record, signal)
    } catch (error) {
      if (previousCandidate && !previousCandidateReplaced) {
        // The old candidate is still owned by the Gateway and remains the
        // authoritative visible state.  Restore only the fields changed by
        // an in-progress bind attempt; no new candidate was committed.
        record.candidatePreview = previousCandidate
        record.activePreviewArtifactId = previousState.activePreviewArtifactId
        record.documentUrl = previousState.documentUrl
        record.expectedOrigin = previousState.expectedOrigin
        record.mode = previousState.mode
        record.browserDocumentReady = previousState.browserDocumentReady
      } else {
        record.candidatePreview = null
        record.activePreviewArtifactId = record.canonicalPreviewArtifactId
        record.documentUrl = record.canonicalDocumentUrl
        record.expectedOrigin = record.canonicalExpectedOrigin
        record.mode = record.canonicalMode
        record.browserDocumentReady = false
      }
      this.invalidateBrowserAnchors(record)
      // `runImmediately` applies the offline guard to the currently loaded
      // realm as soon as it is installed.  If the candidate navigation fails
      // before replacing that realm, restoring only the record fields would
      // leave a canonical/full preview with WebRTC and network globals
      // permanently disabled.  Remove the newly-installed guard and reload
      // the exact previous trusted destination before exposing the surface
      // again.  A guard that pre-dated this bind belongs to the previous
      // surface and is intentionally preserved.
      if (
        candidateOfflineRealmGuardAttempted
        || previousCandidateReplaced
        || (
          !previousCandidateOfflineRealmGuardInstalled
          && (
            record.candidateOfflineRealmGuardInstalled
            || record.candidateOfflineRealmGuardScriptId
              !== previousCandidateOfflineRealmGuardScriptId
          )
        )
      ) {
        try {
          await this.removeOfflineRealmGuard(record, true)
        } catch {
          // Loading a canonical page while the candidate-only document-start
          // guard is still registered would leave a full preview with its
          // network APIs permanently disabled.  Fail closed instead of
          // exposing a misleadingly restored surface; destroyRecord will
          // retry the normal debugger/session cleanup.
          candidateGuardCleanupFailed = true
        }
        if (!candidateGuardCleanupFailed) {
          const rollbackUrl = previousCandidate && !previousCandidateReplaced
            ? previousState.documentUrl
            : record.canonicalDocumentUrl
          if (!record.disposed && !record.view.webContents.isDestroyed()) {
            rollbackLoaded = true
            await record.view.webContents.loadURL(rollbackUrl).catch(() => undefined)
          }
        }
      }
      if (
        !candidateGuardCleanupFailed
        &&
        (!previousCandidate || previousCandidateReplaced)
        && !rollbackLoaded
      ) {
        // The old candidate was released (or there was none), so do not leave
        // its bytes visible after a failed replacement.  This is best effort;
        // browserDocumentReady remains false until a successful load event.
        if (!record.disposed && !record.view.webContents.isDestroyed()) {
          await record.view.webContents.loadURL(record.canonicalDocumentUrl).catch(() => undefined)
        }
      }
      // Never leave a failed candidate bind with the candidate's restrictive
      // policy applied to a canonical/full surface.
      record.view.webContents.setWebRTCIPHandlingPolicy(previousWebRtcPolicy as
        'default'
        | 'default_public_interface_only'
        | 'default_public_and_private_interfaces'
        | 'disable_non_proxied_udp')
      if (candidateGuardCleanupFailed && !record.disposed) {
        this.failRecord(record, 'error', {
          message: 'The candidate preview isolation guard could not be removed safely.',
          reason: 'candidate-guard-cleanup-failed',
        })
      }
      if (this.options.releaseCandidatePreview) {
        await this.options.releaseCandidatePreview(
          candidateHandle,
          new AbortController().signal,
        ).catch(() => undefined)
      }
      throw error
    }
    return { bound: true, candidateHandle }
  }

  private async restoreCanonicalPreview(
    record: NativeWorkbenchSurfaceRecord,
    candidateHandle: string,
    signal: AbortSignal,
  ): Promise<{ restored: true }> {
    this.assertActiveArtifactBridgeRecord(record, signal)
    if (
      record.kind !== 'artifact-preview'
      || record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      || !this.options.releaseCandidatePreview
    ) throw new Error('The active Desktop artifact preview cannot restore a candidate.')
    const candidate = record.candidatePreview
    if (!candidate) return { restored: true }
    // A stale turn must never restore the canonical page underneath a newer
    // candidate that has replaced the active surface.  The bridge request is
    // scoped to the opaque handle that performed the bind; mismatches are a
    // harmless no-op/error and leave the current candidate untouched.
    if (candidate.handle !== candidateHandle) {
      throw new Error('The active candidate preview belongs to another turn.')
    }
    // A candidate may be rendered in an offline realm even when the
    // canonical preview was created in full mode.  Remove the temporary
    // candidate-only network guard before loading the canonical page,
    // otherwise the guard would silently persist for the rest of this
    // preview session.
    await this.removeOfflineRealmGuard(record, true)
    if (record.canonicalMode !== 'offline') {
      await this.removeOfflineRealmGuard(record)
      record.view.webContents.setWebRTCIPHandlingPolicy('default')
    }
    await this.cancelAnnotationInteraction(record, 'candidate-preview-restored', true)
    this.rejectPendingPermissions(record)
    this.cancelPendingAuthentication(record)
    record.authenticationAttempts.clear()
    record.browserDocumentReady = false
    this.invalidateBrowserAnchors(record)
    record.annotationDocumentGeneration += 1
    // Publish the trusted destination before starting the programmatic load.
    // v4 navigation policy admits only this exact URL, so a redirect or
    // renderer-initiated navigation away from the canonical preview remains
    // blocked while the candidate lease is being released.
    record.activePreviewArtifactId = record.canonicalPreviewArtifactId
    record.documentUrl = record.canonicalDocumentUrl
    record.expectedOrigin = record.canonicalExpectedOrigin
    record.mode = record.canonicalMode
    await record.view.webContents.loadURL(record.canonicalDocumentUrl)
    this.assertActiveArtifactBridgeRecord(record, signal)
    try {
      await this.options.releaseCandidatePreview(candidate.handle, signal)
    } catch (error) {
      // Keep the opaque handle for a retry, but do not expose the canonical
      // page as verified until cleanup has been acknowledged by the Gateway.
      record.browserDocumentReady = false
      throw error
    }
    record.candidatePreview = null
    return { restored: true }
  }

  private artifactBridgeBindingError(code: string, message: string): Error {
    const error = new Error(message) as Error & { code?: string }
    error.code = code
    return error
  }

  private bindingRecordNeedsRecovery(
    state: NativeWorkbenchArtifactBridgeBindingState,
  ): boolean {
    const record = state.record
    try {
      return record.disposed
        || record.crashed
        || record.view.webContents.isDestroyed()
        || !record.cdpReady
        || !record.view.webContents.debugger.isAttached()
        || this.surfaces.get(record.id) !== record
    } catch {
      return true
    }
  }

  private async refreshArtifactBridgeCanonicalGrant(
    state: NativeWorkbenchArtifactBridgeBindingState,
  ): Promise<boolean> {
    const grant = await state.previewPin.ensureCurrent()
    if (!grant || grant.scopeId !== state.record.scopeId) {
      state.terminal = true
      throw this.artifactBridgeBindingError(
        'binding-terminal-unavailable',
        'The bound Desktop artifact preview lease is unavailable.',
      )
    }
    const record = state.record
    const changed = record.canonicalDocumentUrl !== grant.launchUrl
      || record.canonicalExpectedOrigin !== grant.expectedOrigin
      || record.canonicalMode !== grant.mode
    if (!changed) return false
    record.canonicalDocumentUrl = grant.launchUrl
    record.canonicalExpectedOrigin = grant.expectedOrigin
    record.canonicalMode = grant.mode
    state.generation += 1
    return true
  }

  private async recoverArtifactBridgeBinding(
    state: NativeWorkbenchArtifactBridgeBindingState,
    signal: AbortSignal,
  ): Promise<void> {
    if (state.released || state.terminal || state.recoveryAttempted || signal.aborted) {
      state.terminal = true
      throw this.artifactBridgeBindingError(
        'binding-terminal-unavailable',
        'The bound Desktop artifact surface is unavailable.',
      )
    }
    state.recoveryAttempted = true
    const failed = state.record
    await this.refreshArtifactBridgeCanonicalGrant(state)
    const candidateHandle = state.candidateHandle ?? failed.candidatePreview?.handle ?? null
    const canonicalArtifactId = failed.canonicalPreviewArtifactId
    const uiReleaseRequested = failed.uiReleaseRequested
    const request: NativeWorkbenchCreateRequest = {
      version: NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
      surfaceId: failed.id,
      kind: 'artifact-preview',
      payload: {
        launchUrl: failed.canonicalDocumentUrl,
        expectedOrigin: failed.canonicalExpectedOrigin || '',
        scopeId: failed.scopeId,
        mode: failed.canonicalMode,
      },
    }
    failed.artifactBridgePins = 0
    try {
      await this.destroyRecord(failed, { preserveCandidatePreview: true })
      const created = await this.createSurfaceNow(request, canonicalArtifactId)
      if (!created.ok) throw new Error(created.message || 'Surface recovery failed.')
      const replacement = this.surfaces.get(failed.id)
      if (!replacement || replacement.disposed || replacement.crashed) {
        throw new Error('The replacement surface is unavailable.')
      }
      replacement.artifactBridgePins = 1
      replacement.uiReleaseRequested = uiReleaseRequested
      replacement.visibleRequested = false
      this.setPhysicalVisibility(replacement, false)
      state.record = replacement
      state.target = this.artifactBridgeTargetForRecord(replacement)
      state.generation += 1
      if (candidateHandle) {
        await this.bindCandidatePreview(replacement, candidateHandle, signal)
        state.candidateHandle = candidateHandle
      }
    } catch (error) {
      state.terminal = true
      const replacement = this.surfaces.get(failed.id)
      if (replacement && replacement !== failed) {
        replacement.artifactBridgePins = 0
        await this.destroyRecord(replacement).catch(() => undefined)
      }
      if (candidateHandle && this.options.releaseCandidatePreview) {
        await this.options.releaseCandidatePreview(
          candidateHandle,
          new AbortController().signal,
        ).catch(() => undefined)
      }
      throw this.artifactBridgeBindingError(
        'binding-terminal-unavailable',
        'The bound Desktop artifact surface could not be recovered.',
      )
    }
  }

  private async invokeArtifactBridgeBinding<T>(
    state: NativeWorkbenchArtifactBridgeBindingState,
    method: keyof DesktopArtifactBridgeTarget,
    request: unknown,
    signal: AbortSignal,
    recoverable: boolean,
  ): Promise<T> {
    if (state.released || state.terminal) {
      throw this.artifactBridgeBindingError(
        'binding-terminal-unavailable',
        'The bound Desktop artifact surface is unavailable.',
      )
    }
    const canonicalGrantChanged = await this.refreshArtifactBridgeCanonicalGrant(state)
    if (canonicalGrantChanged || this.bindingRecordNeedsRecovery(state)) {
      await this.recoverArtifactBridgeBinding(state, signal)
    }

    const run = async (): Promise<T> => {
      const handler = state.target[method]
      if (typeof handler !== 'function') {
        throw new Error(`The Desktop artifact surface does not support ${String(method)}.`)
      }
      return await (handler as (
        value: unknown,
        operationSignal: AbortSignal,
      ) => T | Promise<T>)(request, signal)
    }

    const recordSuccess = (value: T): T => {
      if (method === 'bindCandidatePreview') {
        state.candidateHandle = (request as { candidateHandle?: string }).candidateHandle || null
      } else if (method === 'restoreCanonicalPreview') {
        state.candidateHandle = null
      }
      return value
    }

    try {
      return recordSuccess(await run())
    } catch (error) {
      const needsRecovery = this.bindingRecordNeedsRecovery(state)
      if (method === 'browserAct' && needsRecovery) {
        throw this.artifactBridgeBindingError(
          'action-result-unknown',
          'The Desktop artifact action result is unknown; inspect again.',
        )
      }
      if (!recoverable || !needsRecovery) throw error
      await this.recoverArtifactBridgeBinding(state, signal)
      try {
        return recordSuccess(await run())
      } catch {
        state.terminal = true
        throw this.artifactBridgeBindingError(
          'binding-terminal-unavailable',
          'The bound Desktop artifact surface is unavailable after recovery.',
        )
      }
    }
  }

  private artifactBridgeBindingTarget(
    state: NativeWorkbenchArtifactBridgeBindingState,
  ): DesktopArtifactBridgeTarget {
    const initial = state.target
    return {
      ...initial,
      isCurrent: () => !state.released && !state.terminal,
      capabilities: {
        ...initial.capabilities,
        browserAct: initial.capabilities.bindCandidatePreview === true,
      },
      resolveAnnotationSelection: initial.resolveAnnotationSelection
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'resolveAnnotationSelection', request, signal, true,
          )
        : undefined,
      focusAnnotation: initial.focusAnnotation
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'focusAnnotation', request, signal, true,
          )
        : undefined,
      browserInspect: initial.browserInspect
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'browserInspect', request, signal, true,
          )
        : undefined,
      browserAct: initial.browserAct
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'browserAct', request, signal, false,
          )
        : undefined,
      bindCandidatePreview: initial.bindCandidatePreview
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'bindCandidatePreview', request, signal, true,
          )
        : undefined,
      restoreCanonicalPreview: initial.restoreCanonicalPreview
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'restoreCanonicalPreview', request, signal, true,
          )
        : undefined,
      screenshot: initial.screenshot
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'screenshot', request, signal, true,
          )
        : undefined,
      reloadSurface: initial.reloadSurface
        ? (request, signal) => this.invokeArtifactBridgeBinding(
            state, 'reloadSurface', request, signal, true,
          )
        : undefined,
    }
  }

  async acquireArtifactBridgeTargetBinding(): Promise<DesktopArtifactBridgeTargetBinding | null> {
    if (!this.activeSurfaceId) return null
    const record = this.surfaces.get(this.activeSurfaceId)
    if (
      !record
      || record.artifactBridgePins > 0
      || this.artifactBridgeBindings.has(record.id)
    ) return null
    const target = this.getActiveArtifactBridgeTarget()
    if (!target || record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4) return null
    const previewPin = record.kind === 'artifact-preview'
      && record.canonicalExpectedOrigin
      && this.options.pinArtifactPreview
      ? this.options.pinArtifactPreview({
          launchUrl: record.canonicalDocumentUrl,
          expectedOrigin: record.canonicalExpectedOrigin,
          scopeId: record.scopeId,
          mode: record.canonicalMode,
        })
      : null
    if (record.kind === 'artifact-preview' && !previewPin) return null
    if (!previewPin) return null
    record.artifactBridgePins += 1
    let state: NativeWorkbenchArtifactBridgeBindingState | null = null
    try {
      state = {
        record,
        target,
        previewPin,
        generation: 1,
        recoveryAttempted: false,
        terminal: false,
        released: false,
        candidateHandle: record.candidatePreview?.handle ?? null,
      }
      this.artifactBridgeBindings.set(record.id, state)
      const bindingState = state
      const bindingTarget = this.artifactBridgeBindingTarget(bindingState)
      let released = false
      return {
        target: bindingTarget,
        release: async () => {
          if (released) return
          released = true
          bindingState.released = true
          if (this.artifactBridgeBindings.get(record.id) === bindingState) {
            this.artifactBridgeBindings.delete(record.id)
          }
          const current = bindingState.record
          if (
            bindingState.candidateHandle
            && current.candidatePreview?.handle === bindingState.candidateHandle
            && !current.disposed
            && !current.crashed
          ) {
            try {
              await this.restoreCanonicalPreview(
                current,
                bindingState.candidateHandle,
                new AbortController().signal,
              )
              bindingState.candidateHandle = null
            } catch {
              bindingState.terminal = true
            }
          }
          current.artifactBridgePins = Math.max(0, current.artifactBridgePins - 1)
          if (
            current.artifactBridgePins === 0
            && (current.uiReleaseRequested || current.crashed || bindingState.terminal)
          ) {
            await this.destroyRecord(current)
          } else if (
            bindingState.candidateHandle
            && this.options.releaseCandidatePreview
          ) {
            await this.options.releaseCandidatePreview(
              bindingState.candidateHandle,
              new AbortController().signal,
            ).catch(() => undefined)
          }
          await previewPin.release()
          this.options.emit({
            version: NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
            surfaceId: record.id,
            type: 'agent-edit-released',
          })
        },
      }
    } catch (error) {
      if (state && this.artifactBridgeBindings.get(record.id) === state) {
        this.artifactBridgeBindings.delete(record.id)
      }
      record.artifactBridgePins = Math.max(0, record.artifactBridgePins - 1)
      await previewPin.release()
      throw error
    }
  }

  private trustedCandidatePreviewUrl(launchUrl: string, expectedOrigin: string): boolean {
    try {
      const parsed = new URL(launchUrl)
      const origin = new URL(expectedOrigin)
      return (
        parsed.protocol === 'http:'
        && origin.protocol === 'http:'
        && isLoopbackUrlHostname(parsed.hostname)
        && /^p-[a-f0-9]{32}\.localhost$/i.test(parsed.hostname)
        && parsed.port.length > 0
        && !parsed.username
        && !parsed.password
        && !parsed.search
        && !parsed.hash
        && parsed.origin === origin.origin
      )
    } catch {
      return false
    }
  }

  private samePreviewListener(
    record: NativeWorkbenchSurfaceRecord,
    candidateOrigin: string,
  ): boolean {
    if (!record.canonicalExpectedOrigin) return false
    try {
      const canonical = new URL(record.canonicalExpectedOrigin)
      const candidate = new URL(candidateOrigin)
      return (
        canonical.protocol === 'http:'
        && candidate.protocol === 'http:'
        && canonical.port.length > 0
        && candidate.port === canonical.port
      )
    } catch {
      return false
    }
  }

  private assertActiveArtifactBridgeRecord(
    record: NativeWorkbenchSurfaceRecord,
    signal: AbortSignal,
  ): void {
    if (signal.aborted || !this.isActiveArtifactBridgeRecord(record)) {
      throw new Error('The active Desktop artifact surface changed.')
    }
  }

  private isActiveArtifactBridgeRecord(record: NativeWorkbenchSurfaceRecord): boolean {
    try {
      const live = this.surfaces.get(record.id) === record
        && isArtifactBridgeProtocolVersion(record.version)
        && !record.disposed
        && !record.crashed
        && !record.view.webContents.isDestroyed()
      return live && (
        record.artifactBridgePins > 0
        || (
          this.activeSurfaceId === record.id
          && record.visibleRequested
          && record.view.getVisible()
        )
      )
    } catch {
      return false
    }
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
      && record.kind === 'artifact-preview'
      && isArtifactBridgeProtocolVersion(record.version)
      && !record.disposed
      ? record
      : null
  }

  private isActiveAnnotationRecord(record: NativeWorkbenchSurfaceRecord): boolean {
    return record.kind === 'artifact-preview'
      && record.activePreviewArtifactId !== null
      && this.isActiveArtifactBridgeRecord(record)
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
        || current.elementProofSha256 !== candidate.selection.elementProofSha256
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
        void this.cancelAnnotationInteraction(record, 'selection-stale', true)
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

  private assertBrowserRecord(
    record: NativeWorkbenchSurfaceRecord,
    signal: AbortSignal,
  ): void {
    this.assertActiveArtifactBridgeRecord(record, signal)
    if (
      record.kind !== 'artifact-preview'
      || record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      || record.activePreviewArtifactId === null
      || !record.browserDocumentReady
      || !record.cdpReady
      || !record.view.webContents.debugger.isAttached()
    ) throw new Error('The active Desktop artifact preview does not support browser control.')
  }

  private assertBrowserVerificationHealthy(record: NativeWorkbenchSurfaceRecord): void {
    if (record.browserRuntimeException) {
      throw new Error(
        'The active Desktop artifact preview reported an uncaught runtime exception.',
      )
    }
    if (
      record.missingResourceReported
      || record.blockedNetworkReported
      || record.privilegedOriginReported
    ) {
      throw new Error(
        'The active Desktop artifact preview has a blocked or missing local resource.',
      )
    }
  }

  /**
   * Fence every browser-side operation to the candidate that the Gateway
   * observed immediately before issuing it.  The identity probe and the
   * actual bridge request are separate messages, so checking the opaque
   * handle here closes that small TOCTOU window for actions without anchors
   * (press/scroll) as well as screenshots and reloads.
   */
  private assertCandidateRequestBinding(
    record: NativeWorkbenchSurfaceRecord,
    candidateHandle: string | undefined,
  ): void {
    const candidate = record.candidatePreview
    if (candidate !== null) {
      if (candidateHandle !== candidate.handle) {
        throw new Error('The active candidate preview belongs to another turn.')
      }
      return
    }
    if (candidateHandle !== undefined) {
      throw new Error('The candidate preview is no longer active.')
    }
  }

  private invalidateBrowserAnchors(record: NativeWorkbenchSurfaceRecord): void {
    record.browserAnchors.clear()
    record.browserAnchorGeneration += 1
  }

  private artifactBridgeBindingGeneration(
    record: NativeWorkbenchSurfaceRecord,
  ): number | undefined {
    const state = this.artifactBridgeBindings.get(record.id)
    return state?.record === record && !state.released
      ? state.generation
      : undefined
  }

  private bumpArtifactBridgeBindingGeneration(
    record: NativeWorkbenchSurfaceRecord,
  ): void {
    const state = this.artifactBridgeBindings.get(record.id)
    if (state?.record !== record || state.released) return
    state.generation += 1
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
    ) throw new Error('The canonical preview root is unavailable.')
    return {
      rootObjectId,
      executionContextId: world.executionContextId as number,
    }
  }

  private async inspectBrowser(
    record: NativeWorkbenchSurfaceRecord,
    request: DesktopArtifactBrowserInspectRequest,
    signal: AbortSignal,
  ): Promise<DesktopArtifactBrowserSnapshot> {
    this.assertBrowserRecord(record, signal)
    this.assertCandidateRequestBinding(record, request.candidateHandle)
    this.assertBrowserVerificationHealthy(record)
    // The Gateway uses an identity-only probe immediately before a browser
    // action to fence the active candidate surface.  A normal inspection
    // deliberately replaces the bounded anchor table, but doing that here
    // would invalidate an anchor returned by the model's preceding inspect
    // (for example, an anchor outside the first node).  Keep the existing
    // table and return only the authenticated surface identity for this
    // internal probe.
    if (request.identityOnly === true) {
      return {
        scope: request.scope,
        nodes: [],
        truncated: false,
        activePreviewArtifactId: record.activePreviewArtifactId,
        scopeId: record.scopeId,
        candidateHandle: record.candidatePreview?.handle ?? null,
        bindingGeneration: this.artifactBridgeBindingGeneration(record),
      }
    }
    const objectGroup = `opensquilla-browser-${randomUUID()}`
    try {
      const { rootObjectId } = await this.browserRoot(record, objectGroup)
      const inspected = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: rootObjectId,
        objectGroup,
        functionDeclaration: NATIVE_WORKBENCH_BROWSER_SNAPSHOT_FUNCTION,
        arguments: [{ value: request.scope }, { value: request.maxNodes }],
        awaitPromise: true,
        returnByValue: true,
        silent: true,
      }) as {
        exceptionDetails?: unknown
        result?: { value?: unknown }
      }
      if (inspected.exceptionDetails) throw new Error('The browser snapshot failed.')
      const raw = inspected.result?.value
      if (!raw || typeof raw !== 'object' || (raw as Record<string, unknown>).ok !== true) {
        const reason = raw && typeof raw === 'object'
          ? (raw as Record<string, unknown>).reason
          : null
        throw new Error(typeof reason === 'string' ? reason : 'The browser snapshot failed.')
      }
      const payload = raw as Record<string, unknown>
      const rawNodes = payload.nodes
      if (!Array.isArray(rawNodes)) throw new Error('The browser snapshot was malformed.')
      const anchors = new Map<string, NativeWorkbenchBrowserAnchor>()
      const nodes: DesktopArtifactBrowserSnapshot['nodes'] = []
      const issuedAt = Date.now()
      const anchorGeneration = record.browserAnchorGeneration + 1
      for (const rawNode of rawNodes) {
        if (!rawNode || typeof rawNode !== 'object') continue
        const node = rawNode as Record<string, unknown>
        const anchor = node.anchor
        const elementPath = node.elementPath
        if (
          typeof anchor !== 'string'
          || !/^[A-Za-z0-9_-]{1,128}$/.test(anchor)
          || typeof elementPath !== 'string'
          || elementPath.length === 0
          || elementPath.length > 4096
        ) continue
        anchors.set(anchor, {
          elementPath,
          documentGeneration: record.annotationDocumentGeneration,
          anchorGeneration,
          surfaceId: record.id,
          scopeId: record.scopeId,
          activePreviewArtifactId: record.activePreviewArtifactId,
          candidateHandle: record.candidatePreview?.handle ?? null,
          expiresAt: issuedAt + NATIVE_WORKBENCH_BROWSER_ANCHOR_TTL_MS,
        })
        const bounded = (value: unknown, max: number): string | undefined => (
          typeof value === 'string' && value.length > 0
            ? value.slice(0, max)
            : undefined
        )
        nodes.push({
          anchor,
          role: bounded(node.role, 256),
          name: bounded(node.name, 256),
          text: bounded(node.text, 512),
          interactive: node.interactive === true,
          disabled: node.disabled === true,
          selected: node.selected === true,
        })
      }
      if (nodes.length > request.maxNodes) nodes.splice(request.maxNodes)
      // Candidate replacement may have occurred while CDP was collecting the
      // snapshot.  Do not let an old turn publish its anchors into the new
      // candidate's table.
      this.assertBrowserRecord(record, signal)
      this.assertCandidateRequestBinding(record, request.candidateHandle)
      record.browserAnchors = anchors
      record.browserAnchorGeneration = anchorGeneration
      return {
        scope: request.scope,
        nodes,
        truncated: payload.truncated === true || rawNodes.length > nodes.length,
        activePreviewArtifactId: record.activePreviewArtifactId,
        scopeId: record.scopeId,
        candidateHandle: record.candidatePreview?.handle ?? null,
        bindingGeneration: this.artifactBridgeBindingGeneration(record),
      }
    } finally {
      await this.cdpCommand(record, 'Runtime.releaseObjectGroup', { objectGroup })
        .catch(() => undefined)
    }
  }

  private async actBrowser(
    record: NativeWorkbenchSurfaceRecord,
    request: DesktopArtifactBrowserActRequest,
    signal: AbortSignal,
  ): Promise<DesktopArtifactBrowserActResult> {
    this.assertBrowserRecord(record, signal)
    this.assertCandidateRequestBinding(record, request.candidateHandle)
    if (
      record.candidatePreview === null
      || record.activePreviewArtifactId !== record.candidatePreview.artifactId
      || record.mode !== 'offline'
    ) {
      throw new Error(
        'The active Desktop browser action requires an offline candidate preview.',
      )
    }
    this.assertBrowserVerificationHealthy(record)
    const anchor = request.action === 'press' || request.action === 'scroll'
      ? null
      : record.browserAnchors.get(request.anchor)
    if (request.action !== 'press' && request.action !== 'scroll') {
      if (
        !anchor
        || anchor.documentGeneration !== record.annotationDocumentGeneration
        || anchor.anchorGeneration !== record.browserAnchorGeneration
        || anchor.surfaceId !== record.id
        || anchor.scopeId !== record.scopeId
        || anchor.activePreviewArtifactId !== record.activePreviewArtifactId
        || anchor.candidateHandle !== (record.candidatePreview?.handle ?? null)
        || anchor.expiresAt <= Date.now()
      ) {
        throw new Error('The browser anchor is stale; inspect the preview again.')
      }
    }
    const objectGroup = `opensquilla-browser-act-${randomUUID()}`
    try {
      const { rootObjectId } = await this.browserRoot(record, objectGroup)
      let foundObjectId = rootObjectId
      if (anchor) {
        const found = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
          objectId: rootObjectId,
          objectGroup,
          functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_FIND_BY_PATH_FUNCTION,
          arguments: [{ value: anchor.elementPath }],
          returnByValue: false,
          silent: true,
        }) as { exceptionDetails?: unknown; result?: { objectId?: unknown; subtype?: unknown } }
        if (
          found.exceptionDetails
          || found.result?.subtype === 'null'
          || typeof found.result?.objectId !== 'string'
          || !found.result.objectId
        ) throw new Error('The browser anchor no longer exists in the preview.')
        foundObjectId = found.result.objectId
      }
      let functionDeclaration: string
      let argumentsList: Array<{ value: unknown }> = []
      if (request.action === 'click' || request.action === 'focus') {
        functionDeclaration = NATIVE_WORKBENCH_BROWSER_CLICK_FUNCTION
        argumentsList = [{ value: request.action === 'focus' }]
      } else if (request.action === 'type') {
        functionDeclaration = NATIVE_WORKBENCH_BROWSER_TYPE_FUNCTION
        argumentsList = [{ value: request.text }, { value: request.replace }]
      } else if (request.action === 'press') {
        functionDeclaration = NATIVE_WORKBENCH_BROWSER_PRESS_FUNCTION
        argumentsList = [{ value: request.key }]
      } else if (request.action === 'scroll') {
        functionDeclaration = NATIVE_WORKBENCH_BROWSER_SCROLL_FUNCTION
        argumentsList = [{ value: request.direction }, { value: request.amount }]
      } else {
        throw new Error('The browser action is unsupported.')
      }
      try {
        const acted = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
          objectId: foundObjectId,
          objectGroup,
          functionDeclaration,
          arguments: argumentsList,
          awaitPromise: true,
          returnByValue: true,
          silent: true,
        }) as { exceptionDetails?: unknown; result?: { value?: unknown } }
        if (acted.exceptionDetails) throw new Error('The browser action failed.')
        const raw = acted.result?.value
        if (!raw || typeof raw !== 'object' || (raw as Record<string, unknown>).ok !== true) {
          const reason = raw && typeof raw === 'object'
            ? (raw as Record<string, unknown>).reason
            : null
          throw new Error(typeof reason === 'string' ? reason : 'The browser action failed.')
        }
        this.assertBrowserRecord(record, signal)
        this.assertCandidateRequestBinding(record, request.candidateHandle)
        // Actions can change the DOM without navigation. Never let an anchor
        // survive an action; the model must inspect again before acting again.
        this.invalidateBrowserAnchors(record)
        return {
          performed: true,
          changed: (raw as Record<string, unknown>).changed === true,
        }
      } catch {
        // Runtime.callFunctionOn may have reached the page even when CDP lost
        // the reply. Clear every anchor and force a fresh inspection instead
        // of allowing a blind replay of a potentially completed side effect.
        this.invalidateBrowserAnchors(record)
        throw this.artifactBridgeBindingError(
          'action-result-unknown',
          'The Desktop artifact action result is unknown; inspect again.',
        )
      }
    } finally {
      await this.cdpCommand(record, 'Runtime.releaseObjectGroup', { objectGroup })
        .catch(() => undefined)
    }
  }

  private async focusTrustedAnnotation(
    record: NativeWorkbenchSurfaceRecord,
    request: DesktopArtifactFocusAnnotationRequest,
    signal: AbortSignal,
  ): Promise<{ focused: true; activePreviewArtifactId: string }> {
    this.assertActiveArtifactBridgeRecord(record, signal)
    if (
      record.kind !== 'artifact-preview'
      || !record.activePreviewArtifactId
      || request.activePreviewArtifactId !== record.activePreviewArtifactId
      || request.scopeId !== record.scopeId
    ) {
      throw new Error('The active Desktop artifact preview does not match this annotation.')
    }
    if (this.activeAnnotationOverlayBinding(record)) {
      throw new Error('Finish the current annotation before focusing another element.')
    }
    await this.clearAnnotationFocusHighlight(record)
    const generation = record.annotationDocumentGeneration
    const objectGroup = `opensquilla-annotation-focus-${randomUUID()}`
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
      if (root.exceptionDetails || typeof rootObjectId !== 'string' || !rootObjectId) {
        throw new Error('The canonical preview root is unavailable.')
      }
      const found = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: rootObjectId,
        objectGroup,
        functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_FIND_BY_PATH_FUNCTION,
        arguments: [{ value: request.elementPath }],
        returnByValue: false,
        silent: true,
      }) as {
        exceptionDetails?: unknown
        result?: { objectId?: unknown; subtype?: unknown }
      }
      const selectedObjectId = found.result?.objectId
      if (
        found.exceptionDetails
        || found.result?.subtype === 'null'
        || typeof selectedObjectId !== 'string'
        || !selectedObjectId
      ) throw new Error('The annotation element path no longer exists in the preview.')
      const inspected = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: selectedObjectId,
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
        throw new Error('The annotation element could not be inspected safely.')
      }
      const selection = parseNativeWorkbenchAnnotationSelection(inspected.result?.value)
      if (
        selection.tagName !== request.tagName
        || selection.elementPath !== request.elementPath
        || selection.elementProofSha256 !== request.elementProofSha256
      ) throw new Error('The preview DOM no longer matches the annotation anchor.')
      const described = await this.cdpCommand(record, 'DOM.describeNode', {
        objectId: selectedObjectId,
      }) as { node?: { backendNodeId?: unknown } }
      const backendNodeId = described.node?.backendNodeId
      if (!Number.isSafeInteger(backendNodeId) || (backendNodeId as number) < 1) {
        throw new Error('The annotation element cannot be highlighted.')
      }
      const scrolled = await this.cdpCommand(record, 'Runtime.callFunctionOn', {
        objectId: selectedObjectId,
        objectGroup,
        functionDeclaration: NATIVE_WORKBENCH_ANNOTATION_SCROLL_FUNCTION,
        returnByValue: true,
        silent: true,
      }) as {
        exceptionDetails?: unknown
        result?: { value?: unknown }
      }
      if (scrolled.exceptionDetails) {
        throw new Error('The annotation element could not be focused safely.')
      }
      parseNativeWorkbenchAnnotationGeometry(scrolled.result?.value)
      this.assertActiveArtifactBridgeRecord(record, signal)
      if (generation !== record.annotationDocumentGeneration) {
        throw new Error('The preview navigated while the annotation was being focused.')
      }
      await this.cdpCommand(record, 'Overlay.highlightNode', {
        backendNodeId,
        highlightConfig: NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG,
      })
      record.annotationFocusTimer = setTimeout(() => {
        record.annotationFocusTimer = null
        if (
          record.cdpReady
          && !record.view.webContents.isDestroyed()
          && record.view.webContents.debugger.isAttached()
        ) void this.cdpCommand(record, 'Overlay.hideHighlight').catch(() => undefined)
      }, 2_500)
      record.annotationFocusTimer.unref()
      return {
        focused: true,
        activePreviewArtifactId: record.activePreviewArtifactId,
      }
    } catch (error) {
      await this.clearAnnotationFocusHighlight(record)
      throw error
    } finally {
      await this.cdpCommand(record, 'Runtime.releaseObjectGroup', { objectGroup })
        .catch(() => undefined)
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
        record.kind === 'artifact-preview'
        && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
        && (method === 'Runtime.exceptionThrown' || method === 'Runtime.consoleAPICalled')
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
  ): Promise<unknown> {
    const operation = record.cdpQueue.then(async () => {
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
        ...(candidate.domSha256 === undefined ? {} : { domSha256: candidate.domSha256 }),
        elementProofSha256: candidate.elementProofSha256,
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
      this.emit(record, 'annotation-selected', { selection })
    } catch (error) {
      this.clearAnnotationCandidate(record)
      this.emit(record, 'blocked-action', {
        action: 'annotation-picker',
        reason: errorMessage(error).slice(0, 200),
      })
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
    if (
      record.kind === 'artifact-preview'
      && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      && (
        request.action === 'back'
        || request.action === 'forward'
        || request.action === 'open-external'
        || (
          request.action === 'navigate'
          && request.url !== record.documentUrl
        )
      )
    ) {
      // Programmatic WebContents navigation does not reliably emit
      // ``will-navigate``.  Enforce the v4 exact-URL fence at this entry point
      // as well, so trusted UI IPC cannot bypass the candidate/canonical
      // preview boundary through history or an external address.
      return {
        ok: false,
        code: 'NAVIGATION_BLOCKED',
        retryable: false,
        message: 'Agent HTML previews cannot navigate outside the bound document.',
      }
    }
    const contents = record.view.webContents
    if (
      isArtifactBridgeProtocolVersion(record.version)
      && record.kind === 'artifact-preview'
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
    if (pending) this.cancelPendingAuthentication(pending)
    return await this.queueSurfaceOperation(
      surfaceId,
      () => this.destroySurfaceNow(surfaceId),
    )
  }

  private async destroySurfaceNow(surfaceId: string): Promise<NativeWorkbenchSurfaceResult> {
    const record = this.surfaces.get(surfaceId)
    if (!record) return { ok: true }
    if (record.artifactBridgePins > 0) {
      record.uiReleaseRequested = true
      record.visibleRequested = false
      this.setPhysicalVisibility(record, false)
      void this.cancelAnnotationInteraction(record, 'surface-hidden', true)
      return {
        ok: true,
        code: 'AGENT_EDIT_IN_PROGRESS',
        message: 'Agent editing is continuing in the background.',
      }
    }
    await this.destroyRecord(record)
    return { ok: true }
  }

  private destroyRecord(
    record: NativeWorkbenchSurfaceRecord,
    options: { preserveCandidatePreview?: boolean } = {},
  ): Promise<void> {
    if (record.cleanupPromise) return record.cleanupPromise
    const isCurrent = this.surfaces.get(record.id) === record
    if (isCurrent) this.surfaces.delete(record.id)
    if (isCurrent && this.activeSurfaceId === record.id) this.activeSurfaceId = null
    void this.cancelAnnotationInteraction(record, 'surface-closed', true)
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
          record.debuggerExpectedDetach = true
          record.view.webContents.debugger.detach()
        }
        record.view.webContents.close({ waitForBeforeUnload: false })
      }
    } catch {}

    const cleanupPromise = this.cleanupDisposedRecord(
      record,
      options.preserveCandidatePreview === true,
    )
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
    preserveCandidatePreview: boolean,
  ): Promise<void> {
    if (
      !preserveCandidatePreview
      && record.candidatePreview
      && this.options.releaseCandidatePreview
    ) {
      await this.options.releaseCandidatePreview(
        record.candidatePreview.handle,
        new AbortController().signal,
      ).catch(() => undefined)
      record.candidatePreview = null
    }
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
    // Candidate previews can temporarily switch a surface created in full
    // mode into the offline realm. Register this once for every v2 surface
    // and consult the live mode instead of capturing the creation mode.
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
      const activeCandidatePreview = (
        record.candidatePreview !== null
        && record.activePreviewArtifactId === record.candidatePreview.artifactId
      )
      if (
        webContents !== record.view.webContents
        || record.disposed
        || !nativeWorkbenchDownloadAllowed(item.hasUserGesture(), activeCandidatePreview)
      ) {
        event.preventDefault()
        this.emit(record, 'blocked-action', {
          action: 'download',
          targetUrl: item.getURL(),
          reason: activeCandidatePreview
            ? 'candidate-preview-download-denied'
            : 'user-gesture-required',
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
    candidateOnly = false,
  ): Promise<void> {
    const isInstalled = candidateOnly
      ? record.candidateOfflineRealmGuardInstalled
      : record.offlineRealmGuardInstalled
    if (isInstalled) return
    const setState = (installed: boolean, scriptId: string | null): void => {
      if (candidateOnly) {
        record.candidateOfflineRealmGuardInstalled = installed
        record.candidateOfflineRealmGuardScriptId = scriptId
      } else {
        record.offlineRealmGuardInstalled = installed
        record.offlineRealmGuardScriptId = scriptId
      }
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
        source: candidateOnly
          ? NATIVE_WORKBENCH_CANDIDATE_OFFLINE_REALM_GUARD
          : NATIVE_WORKBENCH_OFFLINE_REALM_GUARD,
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
      // it on a failed setup so a later canonical/full bind cannot inherit a
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

  private async removeOfflineRealmGuard(
    record: NativeWorkbenchSurfaceRecord,
    candidateOnly = false,
  ): Promise<void> {
    const isInstalled = candidateOnly
      ? record.candidateOfflineRealmGuardInstalled
      : record.offlineRealmGuardInstalled
    const scriptId = candidateOnly
      ? record.candidateOfflineRealmGuardScriptId
      : record.offlineRealmGuardScriptId
    if (!isInstalled && !scriptId) return
    await this.ensureDebuggerAttached(record)
    if (scriptId) {
      await this.cdpCommand(record, 'Page.removeScriptToEvaluateOnNewDocument', {
        identifier: scriptId,
      })
    }
    if (candidateOnly) {
      record.candidateOfflineRealmGuardInstalled = false
      record.candidateOfflineRealmGuardScriptId = null
    } else {
      record.offlineRealmGuardInstalled = false
      record.offlineRealmGuardScriptId = null
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
      if (isArtifactBridgeProtocolVersion(record.version)) {
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
      if (
        record.kind === 'artifact-preview'
        && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
      ) {
        // Autonomous browser control is fenced to the exact Gateway-issued
        // candidate/canonical URL currently recorded on this surface.  A
        // renderer link, script, or stale user gesture must never turn the
        // agent preview into an arbitrary top-level browser.
        if (targetUrl !== record.documentUrl) {
          event.preventDefault()
          this.reportPrivilegedGatewayBlock(record, targetUrl)
          this.emit(record, 'blocked-action', {
            action: 'navigation',
            targetUrl,
            reason: 'agent-preview-navigation-denied',
          })
        }
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
      if (isArtifactBridgeProtocolVersion(record.version)) {
        void this.cancelAnnotationInteraction(record, 'surface-redirect', true)
      }
      if (
        record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION
        || (
          record.kind === 'artifact-preview'
          && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
          && targetUrl !== record.documentUrl
        )
        || !this.v2TopLevelNavigationAllowed(record, targetUrl)
      ) {
        event.preventDefault()
        if (record.version !== NATIVE_WORKBENCH_PROTOCOL_VERSION) {
          this.reportPrivilegedGatewayBlock(record, targetUrl)
          this.emit(record, 'blocked-action', {
            action: 'redirect',
            targetUrl,
            reason: record.kind === 'artifact-preview'
              && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
              ? 'agent-preview-navigation-denied'
              : 'scheme-or-offline-policy',
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
        if (!isMainFrame || !isArtifactBridgeProtocolVersion(record.version)) return
        if (
          record.kind === 'artifact-preview'
          && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
        ) {
          record.browserDocumentReady = false
          record.browserRuntimeException = false
          record.missingResourceReported = false
          record.blockedNetworkReported = false
          record.privilegedOriginReported = false
        }
        record.annotationDocumentGeneration += 1
        this.bumpArtifactBridgeBindingGeneration(record)
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
        record.kind === 'artifact-preview'
        && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
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
      if (isArtifactBridgeProtocolVersion(record.version)) {
        record.annotationDocumentGeneration += 1
        this.bumpArtifactBridgeBindingGeneration(record)
        this.invalidateBrowserAnchors(record)
        void this.cancelAnnotationInteraction(record, 'surface-navigation', true)
      }
      this.emitNavigationState(record)
    })
    contents.on('did-finish-load', () => {
      if (
        record.kind === 'artifact-preview'
        && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
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
        record.kind === 'artifact-preview'
        && record.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V4
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
    if (record.artifactBridgePins > 0) {
      // A turn binding owns one deterministic recovery attempt. Preserve the
      // exact record and candidate mapping until its next operation decides
      // whether recovery is safe; UI lifecycle events must not pre-empt it.
      void this.cancelAnnotationInteraction(record, 'surface-failed', true)
      this.dispatchEvent(record, type, detail)
      return true
    }
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
