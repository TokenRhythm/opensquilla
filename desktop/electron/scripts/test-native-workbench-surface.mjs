import { readFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { DesktopBrowserError } from '../dist/desktop-browser.js'
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
  NATIVE_WORKBENCH_PROTOCOL_VERSION_V4,
  nativeWorkbenchArtifactRequestIsDocument,
  nativeWorkbenchArtifactUrl,
  nativeWorkbenchCssRectToDip,
  nativeWorkbenchDownloadAllowed,
  nativeWorkbenchMissingResourceIsLocal,
  nativeWorkbenchNetworkUrlAllowed,
  nativeWorkbenchV2NetworkUrlAllowed,
  parseNativeWorkbenchCreateRequest,
  parseNativeWorkbenchNavigationRequest,
  parseNativeWorkbenchNavigationUrl,
  parseNativeWorkbenchPermissionResponse,
  parseNativeWorkbenchSurfaceId,
  parseNativeWorkbenchSurfaceRectRequest,
} from '../dist/native-workbench-surface-contract.js'

const nativeWorkbenchSurfaceRuntime = await readFile(
  new URL('../dist/native-workbench-surface.js', import.meta.url),
  'utf8',
)
// Exercise the compiled manager method without starting an Electron application.
const browserExecutorStart = nativeWorkbenchSurfaceRuntime.indexOf('    async executeBrowser(')
const browserExecutorEnd = nativeWorkbenchSurfaceRuntime.indexOf('    async snapshotBrowser(', browserExecutorStart)
assert.ok(browserExecutorStart >= 0 && browserExecutorEnd > browserExecutorStart)
const executeBrowser = new Function(
  'DesktopBrowserError', 'randomUUID', 'parseNativeWorkbenchNavigationUrl', 'NATIVE_WORKBENCH_PROTOCOL_VERSION_V4',
  `return ({${nativeWorkbenchSurfaceRuntime.slice(browserExecutorStart, browserExecutorEnd)}}).executeBrowser`,
)(DesktopBrowserError, randomUUID, parseNativeWorkbenchNavigationUrl, NATIVE_WORKBENCH_PROTOCOL_VERSION_V4)

async function assertBrowserOpenPreservesForeground(sessionKey, switchWhileOpening, failure = null) {
  const foreground = { id: 'foreground', visible: true, requestedRect: { x: 180, y: 90, width: 700, height: 500 } }
  const switched = { id: 'switched', visible: false, requestedRect: { x: 240, y: 110, width: 620, height: 480 } }
  const events = []
  const rectCalls = []
  const commands = []
  const destroyed = []
  const controller = new AbortController()
  const setError = new Error('Synthetic viewport initialization failure')
  const clearError = new Error('Synthetic viewport cleanup failure')
  let openingRecord
  let ownerDestroyed = false
  let finishOpening
  const opening = new Promise(resolve => { finishOpening = resolve })
  const manager = {
    surfaces: new Map([[foreground.id, foreground], [switched.id, switched]]),
    activeSurfaceId: foreground.id,
    isPrivilegedGatewayTarget: () => false,
    async createSurface(request) {
      openingRecord = {
        id: request.surfaceId, scopeId: request.payload.scopeId, kind: request.kind,
        url: request.payload.url, targetRef: `target-${request.surfaceId}`, visible: false,
        owner: { isDestroyed: () => ownerDestroyed },
        view: { webContents: { isDestroyed: () => false } },
      }
      this.surfaces.set(request.surfaceId, openingRecord)
      await opening
      return { ok: true }
    },
    async queueSurfaceOperation(key, operation) {
      assert.equal(key, `operation:${openingRecord.targetRef}`)
      return await operation()
    },
    async cdpCommand(record, method, params, beforeSend) {
      beforeSend?.()
      assert.equal(record, openingRecord)
      assert.deepEqual(events, [], 'the UI must not adopt before viewport initialization completes')
      commands.push({ method, params })
      if (method === 'Emulation.setDeviceMetricsOverride') {
        if (failure === 'cancel') controller.abort()
        if (failure === 'owner-close') ownerDestroyed = true
        if (failure === 'replace') this.surfaces.set(record.id, { ...record, targetRef: 'replacement-target' })
        if (failure === 'close') { record.disposed = true; this.surfaces.delete(record.id) }
        if (failure === 'set' || failure === 'both') throw setError
      }
      if (method === 'Emulation.clearDeviceMetricsOverride' && (failure === 'clear' || failure === 'both')) throw clearError
    },
    async destroyRecord(record) {
      assert.equal(this.surfaces.get(record.id), record, 'initialization cleanup must never destroy a replacement')
      destroyed.push(record)
      this.surfaces.delete(record.id)
    },
    setSurfaceRect(request) {
      rectCalls.push(request)
      if (request.visible) {
        for (const record of this.surfaces.values()) record.visible = record.id === request.surfaceId
        this.activeSurfaceId = request.surfaceId
      }
    },
    describeBrowserRecord: record => ({ targetRef: record.targetRef, sessionKey: record.scopeId, url: record.url }),
    getBrowserTarget(surfaceId) { return this.describeBrowserRecord(this.surfaces.get(surfaceId)) },
    emit(record, type, detail) { events.push({ surfaceId: record.id, type, detail }) },
  }
  const pending = executeBrowser.call(manager, { operation: 'open', sessionKey, url: 'https://example.test/background' }, controller.signal)
  if (switchWhileOpening) {
    foreground.visible = false
    switched.visible = true
    manager.activeSurfaceId = switched.id
  }
  finishOpening()
  let target
  if (failure) {
    await assert.rejects(pending, error => {
      if (failure === 'set' || failure === 'both') return error === setError
      if (failure === 'clear') return error === clearError
      return error.code === (failure === 'cancel' ? 'TIMEOUT' : 'TARGET_NOT_FOUND')
    })
  } else target = await pending
  const expectedForeground = switchWhileOpening ? switched : foreground
  assert.equal(manager.activeSurfaceId, expectedForeground.id, 'agent open must not replace the user-selected foreground')
  assert.equal(expectedForeground.visible, true)
  assert.deepEqual(rectCalls, [], 'only the adopting UI may assign a visible layout rectangle')
  assert.deepEqual(commands[0], { method: 'Emulation.setDeviceMetricsOverride',
    params: { width: 960, height: 720, deviceScaleFactor: 0, mobile: false } })
  if (failure === 'replace' || failure === 'close' || failure === 'owner-close') {
    assert.equal(commands.length, 1, 'no cleanup command may target a replaced or closed page')
    assert.deepEqual(destroyed, failure === 'owner-close' ? [openingRecord] : [])
    if (failure === 'replace') assert.equal(manager.surfaces.get(openingRecord.id).targetRef, 'replacement-target')
  } else {
    assert.deepEqual(commands[1], { method: 'Emulation.clearDeviceMetricsOverride', params: undefined })
    assert.equal(commands.length, 2)
  }
  if (failure) {
    assert.deepEqual(events, [])
    if (failure !== 'replace' && failure !== 'close') assert.deepEqual(destroyed, [openingRecord])
    return
  }
  assert.deepEqual(destroyed, [])
  const created = [...manager.surfaces.values()].find(record => record.targetRef === target.targetRef)
  assert.ok(created, 'a background target must remain available, not be destroyed')
  assert.equal(created.visible, false)
  assert.deepEqual((await executeBrowser.call(manager, { operation: 'list', sessionKey }, new AbortController().signal)).targets, [target])
  assert.deepEqual(events, [{ surfaceId: created.id, type: 'browser-opened', detail: {
    url: target.url, title: undefined, sessionKey, targetRef: target.targetRef,
  } }])
}
for (const sessionKey of ['foreground-session', 'background-session']) {
  await assertBrowserOpenPreservesForeground(sessionKey, false)
  await assertBrowserOpenPreservesForeground(sessionKey, true)
}
for (const failure of ['set', 'clear', 'both', 'cancel', 'replace', 'close', 'owner-close']) {
  await assertBrowserOpenPreservesForeground('background-session', false, failure)
}

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
  /const rearmed = await this\.armAnnotationPicker\([\s\S]*?!rearmed\.ok[\s\S]*?code: 'ANNOTATION_REARM_FAILED'[\s\S]*?surfaceInstanceId: record\.surfaceInstanceId/,
  'a rejected target must report a stable failure when its one-shot picker cannot rearm',
)
assert.match(
  nativeWorkbenchSurfaceRuntime,
  /surfaceInstanceId: randomUUID\(\)[\s\S]*?return \{ ok: true, surfaceInstanceId: record\.surfaceInstanceId \}/,
  'surface creation must return the exact instance identity used to fence late picker events',
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
const parsedArtifactV4 = parseNativeWorkbenchCreateRequest({
  version: 4,
  surfaceId: 'artifact:v4',
  kind: 'artifact-preview',
  payload: {
    launchUrl: `${previewOrigin}/sites/index.html`,
    expectedOrigin: previewOrigin,
    scopeId: 'synthetic:v4',
    mode: 'offline',
  },
})
assert.equal(parsedArtifactV4.version, 4)
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
  parseNativeWorkbenchAnnotationOverlayCloseRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    annotationId: 'annotation_42',
    rearm: true,
  }),
  { version: 3, surfaceId: 'artifact:v3', annotationId: 'annotation_42', rearm: true },
)
assert.throws(
  () => parseNativeWorkbenchAnnotationOverlayCloseRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    annotationId: 'annotation_42',
    rearm: false,
  }),
  /overlay close request is invalid/,
)
assert.throws(
  () => parseNativeWorkbenchAnnotationOverlayCloseRequest({
    version: 3,
    surfaceId: 'artifact:v3',
    annotationId: 'annotation_42',
    rearm: true,
    unexpected: true,
  }),
  /overlay close request is invalid/,
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
const pageSelection = { ok: true, tagName: 'button', elementPath: 'html > body > button', locatorHint: 'html > body > button', selectionText: 'Save', rect: {x:1,y:2,width:30,height:20}, viewportWidth:800, viewportHeight:600 }
assert.equal(parseNativeWorkbenchAnnotationSelection(pageSelection).selectionText, 'Save')
assert.throws(() => parseNativeWorkbenchAnnotationSelection({...pageSelection, locatorHint: 'x'.repeat(4097)}))
assert.throws(() => parseNativeWorkbenchAnnotationSelection({...pageSelection, sourceAuthority: 'untrusted'}))
assert.equal(NATIVE_WORKBENCH_CAPABILITIES.browser, true)
console.log('Native Workbench contracts passed.')
