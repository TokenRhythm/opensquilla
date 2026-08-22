import assert from 'node:assert/strict'
import { join, resolve } from 'node:path'
import {
  DESKTOP_RENDERER_URL,
  isDesktopRendererDocumentUrl,
  isDesktopRendererUrl,
  resolveDesktopRendererFile,
  routeDesktopRendererRequest,
} from '../dist/desktop-renderer-protocol.js'

assert.equal(isDesktopRendererUrl(DESKTOP_RENDERER_URL), true)
assert.equal(isDesktopRendererUrl('opensquilla-app://desktop.evil/chat/new'), false)
assert.equal(isDesktopRendererUrl('https://desktop/chat/new'), false)
assert.equal(isDesktopRendererDocumentUrl(DESKTOP_RENDERER_URL), true)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/settings/runtime'), true)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/api/system/status'), false)
assert.equal(isDesktopRendererDocumentUrl('opensquilla-app://desktop/assets/app.js'), false)

assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/api/v1/files?q=1', 'POST'),
  { kind: 'gateway', pathAndQuery: '/api/v1/files?q=1' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/assets/app-123.js'),
  { kind: 'file', relativePath: 'assets/app-123.js' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/static/img/QRcode.png'),
  { kind: 'gateway', pathAndQuery: '/static/img/QRcode.png' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/static/img/QRcode.png', 'POST'),
  { kind: 'reject' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/chat/new'),
  { kind: 'spa', relativePath: 'desktop.html' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/assets/%00secret'),
  { kind: 'reject' },
)
assert.deepEqual(
  routeDesktopRendererRequest('opensquilla-app://desktop/assets/app.js', 'DELETE'),
  { kind: 'reject' },
)

const root = resolve('/tmp/opensquilla-control-ui-dist')
assert.equal(
  resolveDesktopRendererFile(root, 'assets/app.js'),
  join(root, 'assets', 'app.js'),
)
assert.equal(resolveDesktopRendererFile(root, '../secret'), null)

console.log('Desktop renderer protocol tests passed.')
