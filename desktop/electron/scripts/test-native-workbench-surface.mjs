import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  DESKTOP_ARTIFACT_BRIDGE_CONTRACT,
  DESKTOP_ARTIFACT_BRIDGE_UNSUPPORTED_CAPABILITIES,
  parseDesktopArtifactBrowserActRequest,
  parseDesktopArtifactBrowserInspectRequest,
  parseDesktopArtifactCaptureSelectionRequest,
  parseDesktopArtifactFocusAnnotationRequest,
  parseDesktopArtifactOfficeFlushRequest,
  parseDesktopArtifactReloadSurfaceRequest,
  parseDesktopArtifactResolveAnnotationSelectionRequest,
  parseDesktopArtifactScreenshotRequest,
} from '../dist/desktop-artifact-bridge-contract.js'
import { DesktopArtifactBridge } from '../dist/desktop-artifact-bridge.js'
import {
  parseNativeWorkbenchAnnotationGeometry,
  parseNativeWorkbenchAnnotationModeRequest,
  parseNativeWorkbenchAnnotationOverlayCloseRequest,
  parseNativeWorkbenchAnnotationOverlayMessage,
  parseNativeWorkbenchAnnotationOverlayShowRequest,
  parseNativeWorkbenchAnnotationSelection,
} from '../dist/native-workbench-annotation-contract.js'
import {
  clampNativeWorkbenchSurfaceRect,
  NATIVE_WORKBENCH_CAPABILITIES,
  NATIVE_WORKBENCH_MAX_HTML_BYTES,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchDownloadAllowed,
  nativeWorkbenchMissingResourceIsLocal,
  nativeWorkbenchNetworkUrlAllowed,
  nativeWorkbenchV2NetworkUrlAllowed,
  parseNativeWorkbenchCreateRequest,
  parseNativeWorkbenchNavigationRequest,
  parseNativeWorkbenchPermissionResponse,
  parseNativeWorkbenchSurfaceId,
  parseNativeWorkbenchSurfaceRectRequest,
} from '../dist/native-workbench-surface-contract.js'

const nativeWorkbenchSurfaceRuntime = await readFile(
  new URL('../dist/native-workbench-surface.js', import.meta.url),
  'utf8',
)
const annotationHighlightConfig = nativeWorkbenchSurfaceRuntime.match(
  /const NATIVE_WORKBENCH_ANNOTATION_HIGHLIGHT_CONFIG = Object\.freeze\(\{([\s\S]*?)\n\}\);/,
)?.[1]
assert.ok(annotationHighlightConfig, 'annotation highlight configuration must be present')
assert.match(annotationHighlightConfig, /showInfo:\s*false/)
assert.match(annotationHighlightConfig, /showAccessibilityInfo:\s*false/)
assert.match(
  annotationHighlightConfig,
  /borderColor:\s*\{\s*r:\s*25,\s*g:\s*118,\s*b:\s*255,\s*a:\s*0\.95\s*\}/,
  'annotation selection must retain its visible blue border',
)
assert.match(
  nativeWorkbenchSurfaceRuntime,
  /if \(!request\.enabled\) \{[\s\S]*?cancelAnnotationInteraction\([\s\S]*?if \(cleanupFailure\)[\s\S]*?return \{[\s\S]*?ok: false,[\s\S]*?code: 'ANNOTATION_BUSY',[\s\S]*?message: cleanupFailure/,
  'explicit picker disable must report a failed native-overlay cleanup',
)
assert.match(
  nativeWorkbenchSurfaceRuntime,
  /request\.enabled\s*\? this\.annotationRecordForUiRequest\(request\.surfaceId\)\s*: this\.annotationRecordForCleanupRequest\(request\.surfaceId\)/,
  'picker disable must resolve the exact live v3 surface even while its overlay hides the preview',
)
assert.match(
  nativeWorkbenchSurfaceRuntime,
  /annotationRecordForCleanupRequest\(surfaceId\) \{[\s\S]*?record\.kind === 'artifact-preview'[\s\S]*?record\.version === NATIVE_WORKBENCH_PROTOCOL_VERSION_V3[\s\S]*?!record\.disposed/,
  'picker cleanup lookup must remain scoped to an undisposed protocol-v3 artifact preview',
)
assert.match(
  nativeWorkbenchSurfaceRuntime,
  /catch \(error\) \{[\s\S]*?annotationPickerActive = false;[\s\S]*?clearAnnotationInspectState\(record, true\)/,
  'picker enable failures must roll the native inspect overlay back',
)
const annotationInspectCleanup = nativeWorkbenchSurfaceRuntime.match(
  /async clearAnnotationInspectState\(record, inspectModeMayBeActive\) \{([\s\S]*?)\n    \}\n    async annotationOverlayForOwner/,
)?.[1]
assert.ok(annotationInspectCleanup, 'annotation inspect cleanup must be present')
assert.ok(
  annotationInspectCleanup.indexOf("'Overlay.setInspectMode'")
    < annotationInspectCleanup.indexOf("'Overlay.hideHighlight'"),
  'picker cleanup must disable inspect mode before hiding the highlight',
)
assert.match(annotationInspectCleanup, /let inspectModeDisableError = null/)
assert.match(
  annotationInspectCleanup,
  /catch \(error\) \{\s*inspectModeDisableError = error;\s*\}/,
)
assert.match(
  annotationInspectCleanup,
  /this\.cdpCommand\(record, 'Overlay\.hideHighlight'\);\s*\}\s*catch \{\s*\}/,
  'hideHighlight must remain compatibility-only best-effort cleanup',
)
assert.match(
  annotationInspectCleanup,
  /return inspectModeDisableError[\s\S]*?The annotation picker could not be fully disabled: \$\{boundedAnnotationCdpError\(inspectModeDisableError\)\}[\s\S]*?: null/,
)

const parsed = parseNativeWorkbenchCreateRequest({
  version: 1,
  surfaceId: 'artifact:synthetic-1',
  kind: 'artifact-html',
  payload: {
    data: new TextEncoder().encode('<!doctype html><title>Fixture</title>'),
    name: '../../fixture.html',
    mime: 'text/html; charset=utf-8',
    scopeId: 'agent:fixture:webchat:fixture',
    allowRemoteResources: false,
  },
})
assert.equal(parsed.payload.name, 'fixture.html')
assert.equal(parsed.payload.mime, 'text/html')
assert.equal(parsed.payload.data.byteLength > 0, true)
assert.equal(
  parseNativeWorkbenchCreateRequest({
    ...parsed,
    payload: { ...parsed.payload, allowRemoteResources: true },
  }).payload.allowRemoteResources,
  true,
)

assert.deepEqual(NATIVE_WORKBENCH_CAPABILITIES, {
  latestVersion: 3,
  protocolVersions: [1, 2, 3],
  versions: [1, 2, 3],
  kinds: ['artifact-html', 'artifact-preview', 'url-preview'],
  modes: ['full', 'offline'],
  navigationActions: ['navigate', 'back', 'forward', 'reload', 'stop', 'open-external'],
  permissionResponses: true,
  artifactBridge: DESKTOP_ARTIFACT_BRIDGE_CONTRACT,
  maxSurfaces: 8,
})

const previewOrigin = 'http://p-0123456789abcdef0123456789abcdef.localhost:48721'
const parsedArtifactV2 = parseNativeWorkbenchCreateRequest({
  version: 2,
  surfaceId: 'artifact:v2',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
})
assert.deepEqual(parsedArtifactV2, {
  version: 2,
  surfaceId: 'artifact:v2',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
})
const parsedArtifactV3 = parseNativeWorkbenchCreateRequest({
  version: 3,
  surfaceId: 'artifact:v3',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v3',
    mode: 'offline',
  },
})
assert.deepEqual(parsedArtifactV3, {
  version: 3,
  surfaceId: 'artifact:v3',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v3',
    mode: 'offline',
  },
})
assert.deepEqual(
  parseNativeWorkbenchCreateRequest({
    version: 2,
    surfaceId: 'browser:v2',
    kind: 'url-preview',
    payload: {
      url: 'https://example.test/path',
      scopeId: 'synthetic:url',
    },
  }),
  {
    version: 2,
    surfaceId: 'browser:v2',
    kind: 'url-preview',
    payload: {
      url: 'https://example.test/path',
      scopeId: 'synthetic:url',
    },
  },
)
for (const payload of [
  {
    launchUrl: 'https://p-0123456789abcdef0123456789abcdef.localhost:48721/index.html',
    expectedOrigin: 'https://p-0123456789abcdef0123456789abcdef.localhost:48721',
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
  {
    launchUrl: `${previewOrigin}/index.html?token=leak`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
  {
    launchUrl: `${previewOrigin}/index.html`,
    expectedOrigin: 'http://127.0.0.1:48721',
    scopeId: 'synthetic:v2',
    mode: 'full',
  },
]) {
  assert.throws(
    () => parseNativeWorkbenchCreateRequest({
      version: 2,
      surfaceId: 'artifact:v2',
      kind: 'artifact-preview',
      payload,
    }),
    /preview address|preview origin/,
  )
}
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    version: 2,
    surfaceId: 'browser:v2',
    kind: 'url-preview',
    payload: { url: 'file:///synthetic/secret', scopeId: 'synthetic:url' },
  }),
  /HTTP or HTTPS/,
)
assert.deepEqual(
  parseNativeWorkbenchNavigationRequest({
    version: 2,
    surfaceId: 'browser:v2',
    action: 'navigate',
    url: 'http://127.0.0.1:5173/demo',
  }),
  {
    version: 2,
    surfaceId: 'browser:v2',
    action: 'navigate',
    url: 'http://127.0.0.1:5173/demo',
  },
)
assert.throws(
  () => parseNativeWorkbenchNavigationRequest({
    version: 2,
    surfaceId: 'browser:v2',
    action: 'reload',
    url: 'https://example.test',
  }),
  /does not accept/,
)
assert.deepEqual(
  parseNativeWorkbenchPermissionResponse({
    version: 2,
    surfaceId: 'browser:v2',
    requestId: '00000000-0000-4000-8000-000000000000',
    allow: true,
  }),
  {
    version: 2,
    surfaceId: 'browser:v2',
    requestId: '00000000-0000-4000-8000-000000000000',
    allow: true,
  },
)
assert.deepEqual(
  parseNativeWorkbenchNavigationRequest({
    version: 3,
    surfaceId: 'browser:v3',
    action: 'reload',
  }),
  {
    version: 3,
    surfaceId: 'browser:v3',
    action: 'reload',
  },
)
assert.deepEqual(
  parseNativeWorkbenchPermissionResponse({
    version: 3,
    surfaceId: 'browser:v3',
    requestId: '00000000-0000-4000-8000-000000000000',
    allow: false,
  }),
  {
    version: 3,
    surfaceId: 'browser:v3',
    requestId: '00000000-0000-4000-8000-000000000000',
    allow: false,
  },
)

assert.deepEqual(
  parseDesktopArtifactCaptureSelectionRequest({ version: 3 }),
  { version: 3 },
)
const selectionDigest = 'a'.repeat(64)
const selectionElementProof = 'b'.repeat(64)
const activePreviewArtifactId = 'art-synthetic-preview'
const selectionPath = JSON.stringify([
  ['', 'html', 1],
  ['', 'body', 1],
  ['', 'button', 2],
])
assert.deepEqual(
  parseDesktopArtifactResolveAnnotationSelectionRequest({
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_42',
    tagName: 'button',
    elementPath: selectionPath,
    domSha256: selectionDigest,
    elementProofSha256: selectionElementProof,
  }),
  {
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_42',
    tagName: 'button',
    elementPath: selectionPath,
    domSha256: selectionDigest,
    elementProofSha256: selectionElementProof,
  },
)
assert.deepEqual(
  parseDesktopArtifactResolveAnnotationSelectionRequest({
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_without_dom_digest',
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
  }),
  {
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_without_dom_digest',
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
  },
)
assert.deepEqual(
  parseDesktopArtifactFocusAnnotationRequest({
    version: 3,
    activePreviewArtifactId,
    annotationId: 'annotation_42',
    scopeId: 'synthetic:scope',
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
  }),
  {
    version: 3,
    activePreviewArtifactId,
    annotationId: 'annotation_42',
    scopeId: 'synthetic:scope',
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
  },
)
assert.throws(
  () => parseDesktopArtifactResolveAnnotationSelectionRequest({
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_42',
    tagName: 'button',
    elementPath: selectionPath,
    domSha256: selectionDigest,
  }),
  /annotation selection is invalid/,
)
assert.throws(
  () => parseDesktopArtifactFocusAnnotationRequest({
    version: 3,
    activePreviewArtifactId,
    annotationId: 'annotation_42',
    scopeId: 'synthetic:scope',
    tagName: 'button',
    elementPath: selectionPath,
    domSha256: selectionDigest,
  }),
  /annotation focus request is invalid/,
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationModeRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    enabled: true,
  }),
  { version: 3, surfaceId: 'artifact:v3', enabled: true },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationOverlayShowRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    selectionId: 'selection_42',
    annotationId: 'annotation_42',
    initialBody: 'Make this concise.',
  }),
  {
    version: 3,
    surfaceId: 'artifact:v3',
    selectionId: 'selection_42',
    annotationId: 'annotation_42',
    initialBody: 'Make this concise.',
  },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationOverlayShowRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    selectionId: 'selection_42',
    annotationId: 'annotation_42',
    initialBody: '',
    overlayCopyVersion: 1,
    copy: {
      targetLabel: 'Heading: Welcome',
      contextLabel: 'Current selection',
      bodyLabel: 'Page annotation',
      placeholder: 'Describe the change…',
      newlineHint: 'Shift + Enter for a new line',
      cancelLabel: 'Cancel',
      submitLabel: 'Add annotation',
      emptyBodyMessage: 'Describe the requested change.',
    },
  }),
  {
    version: 3,
    surfaceId: 'artifact:v3',
    selectionId: 'selection_42',
    annotationId: 'annotation_42',
    initialBody: '',
    overlayCopyVersion: 1,
    copy: {
      targetLabel: 'Heading: Welcome',
      contextLabel: 'Current selection',
      bodyLabel: 'Page annotation',
      placeholder: 'Describe the change…',
      newlineHint: 'Shift + Enter for a new line',
      cancelLabel: 'Cancel',
      submitLabel: 'Add annotation',
      emptyBodyMessage: 'Describe the requested change.',
    },
  },
)
assert.throws(
  () => parseNativeWorkbenchAnnotationOverlayShowRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    selectionId: 'selection_42',
    annotationId: 'annotation_42',
    overlayCopyVersion: 1,
  }),
  /overlay copy is invalid/,
)
assert.throws(
  () => parseNativeWorkbenchAnnotationOverlayShowRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    selectionId: 'selection_42',
    annotationId: 'annotation_42',
    copy: {},
  }),
  /overlay copy is invalid/,
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationOverlayCloseRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    annotationId: 'annotation_42',
  }),
  { version: 3, surfaceId: 'artifact:v3', annotationId: 'annotation_42' },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationOverlayMessage({
    version: 1,
    type: 'draft-changed',
    body: 'Synthetic body',
  }),
  { version: 1, type: 'draft-changed', body: 'Synthetic body' },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationOverlayMessage({
    version: 1,
    type: 'submit',
    body: 'Apply only after Gateway persistence succeeds.',
  }),
  {
    version: 1,
    type: 'submit',
    body: 'Apply only after Gateway persistence succeeds.',
  },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationOverlayMessage({ version: 1, type: 'cancel' }),
  { version: 1, type: 'cancel' },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationSelection({
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
    rect: { x: 1, y: 2, width: 30, height: 20 },
    viewportWidth: 800,
    viewportHeight: 600,
  }),
  {
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
    rect: { x: 1, y: 2, width: 30, height: 20 },
    viewportWidth: 800,
    viewportHeight: 600,
  },
)
assert.deepEqual(
  parseNativeWorkbenchAnnotationGeometry({
    ok: true,
    rect: { x: -4, y: 2, width: 30, height: 20 },
    viewportWidth: 800,
    viewportHeight: 600,
  }),
  {
    rect: { x: -4, y: 2, width: 30, height: 20 },
    viewportWidth: 800,
    viewportHeight: 600,
  },
)
assert.throws(
  () => parseNativeWorkbenchAnnotationGeometry({
    ok: true,
    rect: { x: 0, y: 0, width: Number.POSITIVE_INFINITY, height: 20 },
    viewportWidth: 800,
    viewportHeight: 600,
  }),
  /geometry is invalid/,
)
assert.deepEqual(
  parseDesktopArtifactBrowserInspectRequest({
    version: 3,
    scope: 'viewport',
    maxNodes: 100,
  }),
  { version: 3, scope: 'viewport', maxNodes: 100 },
)
assert.deepEqual(
  parseDesktopArtifactBrowserActRequest({
    version: 3,
    action: 'type',
    anchor: 'node_42',
    text: 'Synthetic input',
    replace: true,
  }),
  {
    version: 3,
    action: 'type',
    anchor: 'node_42',
    text: 'Synthetic input',
    replace: true,
  },
)
assert.deepEqual(parseDesktopArtifactScreenshotRequest({ version: 3 }), { version: 3 })
assert.deepEqual(parseDesktopArtifactOfficeFlushRequest({ version: 3 }), { version: 3 })
assert.deepEqual(parseDesktopArtifactReloadSurfaceRequest({ version: 3 }), { version: 3 })

for (const [parse, payload] of [
  [parseDesktopArtifactCaptureSelectionRequest, { version: 3, surfaceId: 'model-choice' }],
  [parseDesktopArtifactResolveAnnotationSelectionRequest, {
    version: 3,
    activePreviewArtifactId,
    selectionId: 'selection_42',
    tagName: 'button',
    elementPath: selectionPath,
    domSha256: selectionDigest,
    elementProofSha256: selectionElementProof,
    url: 'file:///synthetic/secret',
  }],
  [parseDesktopArtifactFocusAnnotationRequest, {
    version: 3,
    activePreviewArtifactId,
    annotationId: 'annotation_42',
    scopeId: 'synthetic:scope',
    tagName: 'button',
    elementPath: selectionPath,
    elementProofSha256: selectionElementProof,
    selector: '#renderer-controlled',
  }],
  [parseDesktopArtifactBrowserInspectRequest, {
    version: 3,
    scope: 'document',
    maxNodes: 20,
    url: 'https://example.test',
  }],
  [parseDesktopArtifactBrowserInspectRequest, {
    version: 3,
    scope: 'document',
    maxNodes: 20,
    expression: 'document.cookie',
  }],
  [parseDesktopArtifactBrowserActRequest, {
    version: 3,
    action: 'click',
    anchor: 'node_42',
    cdpMethod: 'Runtime.evaluate',
  }],
  [parseDesktopArtifactBrowserActRequest, {
    version: 3,
    action: 'click',
    selector: '#dangerous-arbitrary-selector',
  }],
  [parseDesktopArtifactScreenshotRequest, { version: 3, surfaceId: 'model-choice' }],
  [parseDesktopArtifactOfficeFlushRequest, { version: 3, adapter: 'renderer-chosen' }],
  [parseDesktopArtifactReloadSurfaceRequest, { version: 3, url: 'file:///secret' }],
]) {
  assert.throws(() => parse(payload), /Desktop artifact|browser anchor/)
}
assert.throws(
  () => parseDesktopArtifactBrowserActRequest({
    version: 3,
    action: 'press',
    key: 'F12',
  }),
  /supported Desktop artifact browser key/,
)

const unavailableBridge = new DesktopArtifactBridge({ getActiveTarget: () => null })
assert.deepEqual(
  unavailableBridge.getCapabilities(),
  DESKTOP_ARTIFACT_BRIDGE_UNSUPPORTED_CAPABILITIES,
)
assert.deepEqual(
  await unavailableBridge.captureSelection({ version: 3 }),
  {
    ok: false,
    method: 'captureSelection',
    code: 'unavailable',
    message: 'No active protocol-v3 Desktop artifact surface is available.',
  },
)
assert.equal(
  (await unavailableBridge.screenshot({ version: 3, surfaceId: 'model-choice' })).code,
  'invalid-request',
)

const missingHandlerBridge = new DesktopArtifactBridge({
  getActiveTarget: () => ({
    isCurrent: () => true,
    capabilities: {
      captureSelection: true,
      resolveAnnotationSelection: false,
      focusAnnotation: false,
      browserInspect: false,
      browserAct: false,
      screenshot: false,
      officeFlush: false,
      reloadSurface: false,
    },
  }),
})
assert.equal(missingHandlerBridge.getCapabilities().available, true)
assert.equal(missingHandlerBridge.getCapabilities().captureSelection, false)
assert.equal(
  (await missingHandlerBridge.captureSelection({ version: 3 })).code,
  'unsupported',
)

const bridgeCalls = []
const controlledBridge = new DesktopArtifactBridge({
  getActiveTarget: () => ({
    isCurrent: () => true,
    capabilities: {
      captureSelection: true,
      resolveAnnotationSelection: true,
      focusAnnotation: true,
      browserInspect: true,
      browserAct: true,
      screenshot: true,
      officeFlush: false,
      reloadSurface: true,
    },
    captureSelection: async request => {
      bridgeCalls.push(['captureSelection', request])
      return { kind: 'text', anchor: 'selection_1', text: 'Synthetic selection' }
    },
    resolveAnnotationSelection: async request => {
      bridgeCalls.push(['resolveAnnotationSelection', request])
      return {
        activePreviewArtifactId: request.activePreviewArtifactId,
        selectionId: request.selectionId,
        tagName: request.tagName,
        elementPath: request.elementPath,
        ...(request.domSha256 === undefined ? {} : { domSha256: request.domSha256 }),
        elementProofSha256: request.elementProofSha256,
        scopeId: 'synthetic:scope',
        rect: { x: 1, y: 2, width: 30, height: 20 },
      }
    },
    focusAnnotation: async request => {
      bridgeCalls.push(['focusAnnotation', request])
      return {
        focused: true,
        activePreviewArtifactId: request.activePreviewArtifactId,
      }
    },
    browserInspect: async request => {
      bridgeCalls.push(['browserInspect', request])
      return {
        scope: request.scope,
        nodes: [{ anchor: 'node_42', role: 'button', name: 'Run' }],
        truncated: false,
      }
    },
    browserAct: async request => {
      bridgeCalls.push(['browserAct', request])
      return { performed: true, changed: request.action === 'type' }
    },
    screenshot: async request => {
      bridgeCalls.push(['screenshot', request])
      return {
        mime: 'image/png',
        data: Uint8Array.of(137, 80, 78, 71),
        width: 1,
        height: 1,
      }
    },
    reloadSurface: async request => {
      bridgeCalls.push(['reloadSurface', request])
      return { reloaded: true }
    },
  }),
})
assert.deepEqual(controlledBridge.getCapabilities(), {
  version: 3,
  available: true,
  captureSelection: true,
  resolveAnnotationSelection: true,
  focusAnnotation: true,
  browserInspect: true,
  browserAct: true,
  screenshot: true,
  officeFlush: false,
  reloadSurface: true,
})
assert.equal((await controlledBridge.captureSelection({ version: 3 })).ok, true)
assert.equal((await controlledBridge.resolveAnnotationSelection({
  version: 3,
  activePreviewArtifactId,
  selectionId: 'selection_42',
  tagName: 'button',
  elementPath: selectionPath,
  domSha256: selectionDigest,
  elementProofSha256: selectionElementProof,
})).ok, true)
assert.equal((await controlledBridge.focusAnnotation({
  version: 3,
  activePreviewArtifactId,
  annotationId: 'annotation_42',
  scopeId: 'synthetic:scope',
  tagName: 'button',
  elementPath: selectionPath,
  elementProofSha256: selectionElementProof,
})).ok, true)
assert.equal((await controlledBridge.browserInspect({
  version: 3,
  scope: 'viewport',
  maxNodes: 20,
})).ok, true)
assert.equal((await controlledBridge.browserAct({
  version: 3,
  action: 'type',
  anchor: 'node_42',
  text: 'Synthetic input',
  replace: true,
})).ok, true)
assert.equal((await controlledBridge.screenshot({ version: 3 })).ok, true)
assert.equal((await controlledBridge.officeFlush({ version: 3 })).code, 'unsupported')
assert.equal((await controlledBridge.reloadSurface({ version: 3 })).ok, true)
assert.deepEqual(
  bridgeCalls.map(([method]) => method),
  [
    'captureSelection',
    'resolveAnnotationSelection',
    'focusAnnotation',
    'browserInspect',
    'browserAct',
    'screenshot',
    'reloadSurface',
  ],
)

let staleTargetCurrent = true
let staleTargetCalled = false
const staleTarget = {
  isCurrent: () => staleTargetCurrent,
  capabilities: { captureSelection: true },
  captureSelection: async () => {
    staleTargetCalled = true
    return { kind: 'none' }
  },
}
const staleBridge = new DesktopArtifactBridge({ getActiveTarget: () => staleTarget })
const staleResultPromise = staleBridge.captureSelection({ version: 3 })
staleTargetCurrent = false
assert.equal((await staleResultPromise).code, 'unavailable')
assert.equal(staleTargetCalled, false, 'queued requests must not retarget after the UI switches')

assert.equal(parseNativeWorkbenchSurfaceId('artifact:one'), 'artifact:one')
assert.throws(() => parseNativeWorkbenchSurfaceId('../artifact'), /valid native Workbench surface/)
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    ...parsed,
    payload: { ...parsed.payload, data: new Uint8Array(NATIVE_WORKBENCH_MAX_HTML_BYTES + 1) },
  }),
  /5 MiB preview limit/,
)
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    ...parsed,
    kind: 'browser',
  }),
  /Unsupported native Workbench request/,
)
assert.throws(
  () => parseNativeWorkbenchCreateRequest({
    ...parsed,
    payload: { ...parsed.payload, mime: 'application/javascript' },
  }),
  /Only HTML artifacts/,
)

const rectRequest = parseNativeWorkbenchSurfaceRectRequest({
  surfaceId: parsed.surfaceId,
  x: -12.4,
  y: 50.2,
  width: 900.1,
  height: 700.6,
  visible: true,
})
assert.deepEqual(
  clampNativeWorkbenchSurfaceRect(rectRequest, { width: 800, height: 600 }),
  { x: 0, y: 50, width: 800, height: 550 },
)
assert.equal(
  clampNativeWorkbenchSurfaceRect(
    { x: 900, y: 900, width: 20, height: 20 },
    { width: 800, height: 600 },
  ),
  null,
)
assert.deepEqual(
  nativeWorkbenchCssRectToDip({ x: 400, y: 64, width: 416, height: 560 }, 1.25),
  { x: 500, y: 80, width: 520, height: 700 },
  'DOM CSS pixels scale by Chromium zoom but not OS devicePixelRatio',
)
assert.deepEqual(
  nativeWorkbenchCssRectToDip({ x: 4, y: 5, width: 6, height: 7 }, Number.NaN),
  { x: 4, y: 5, width: 6, height: 7 },
  'invalid zoom factors fail closed to the 1x geometry',
)
assert.equal(
  nativeWorkbenchArtifactUrl('00000000-0000-4000-8000-000000000000'),
  'opensquilla-artifact://00000000-0000-4000-8000-000000000000/index.html',
)
assert.equal(parsed.payload.allowRemoteResources, false)
assert.equal(nativeWorkbenchNetworkUrlAllowed('https://assets.example.test/app.js'), false)
assert.equal(nativeWorkbenchNetworkUrlAllowed('data:image/png;base64,AA=='), true)
assert.equal(nativeWorkbenchNetworkUrlAllowed('blob:null/fixture'), true)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/poster.png',
    true,
    'image',
  ),
  true,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/theme.css',
    true,
    'stylesheet',
  ),
  true,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/app.js',
    true,
    'script',
  ),
  false,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed(
    'https://assets.example.test/data.json',
    true,
    'xhr',
  ),
  false,
)
assert.equal(
  nativeWorkbenchNetworkUrlAllowed('https://assets.example.test/unknown', true),
  false,
)
assert.equal(nativeWorkbenchNetworkUrlAllowed('http://assets.example.test/app.js'), false)
assert.equal(nativeWorkbenchNetworkUrlAllowed('file:///synthetic/secret.txt'), false)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('https://cdn.example.test/app.js', 'full'),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('ws://127.0.0.1:3000/socket', 'full'),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('file:///synthetic/secret.txt', 'full'),
  false,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('opensquilla-artifact://fixture/index.html', 'full'),
  false,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed(`${previewOrigin}/app.js`, 'offline', previewOrigin),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed(
    'ws://p-0123456789abcdef0123456789abcdef.localhost:48721/socket',
    'offline',
    previewOrigin,
  ),
  true,
)
assert.equal(
  nativeWorkbenchV2NetworkUrlAllowed('https://cdn.example.test/app.js', 'offline', previewOrigin),
  false,
)
assert.equal(nativeWorkbenchV2NetworkUrlAllowed('about:config', 'offline', previewOrigin), false)
assert.equal(
  nativeWorkbenchMissingResourceIsLocal(`${previewOrigin}/missing.css`, previewOrigin),
  true,
)
assert.equal(
  nativeWorkbenchMissingResourceIsLocal(
    'https://fonts.googleapis.com/css2?family=Inter',
    previewOrigin,
  ),
  false,
)
assert.equal(
  nativeWorkbenchMissingResourceIsLocal('not a URL', previewOrigin),
  false,
)
assert.equal(nativeWorkbenchDownloadAllowed(true), true)
for (const untrustedGesture of [false, undefined, null, 1, 'true']) {
  assert.equal(
    nativeWorkbenchDownloadAllowed(untrustedGesture),
    false,
    "only Electron's exact user-gesture signal may authorize a save dialog",
  )
}
assert.equal(
  nativeWorkbenchArtifactRequestIsDocument(
    'opensquilla-artifact://fixture-handle/index.html',
    'GET',
    'fixture-handle',
  ),
  true,
)
assert.equal(
  nativeWorkbenchArtifactRequestIsDocument(
    'opensquilla-artifact://fixture-handle/assets/app.css',
    'GET',
    'fixture-handle',
  ),
  false,
)
assert.equal(
  nativeWorkbenchArtifactRequestIsDocument(
    'opensquilla-artifact://other-handle/index.html',
    'GET',
    'fixture-handle',
  ),
  false,
)

console.log('native Workbench surface contract checks passed')
