import assert from 'node:assert/strict'
import {
  NATIVE_WORKBENCH_NAVIGATION_ACTIONS,
  parseNativeWorkbenchCreateRequest,
  parseNativeWorkbenchNavigationRequest,
} from '../dist/native-workbench-surface-contract.js'

const base = { version: 2, surfaceId: 'browser:synthetic' }
const parse = request => parseNativeWorkbenchNavigationRequest({ ...base, ...request })

for (const action of ['find', 'find-next', 'find-stop', 'zoom', 'close', 'download-open']) {
  assert.ok(NATIVE_WORKBENCH_NAVIGATION_ACTIONS.includes(action))
}
assert.deepEqual(parse({ action: 'find', query: 'multiple words' }),
  { ...base, action: 'find', query: 'multiple words' })
assert.deepEqual(parse({ action: 'find', query: '' }), { ...base, action: 'find', query: '' })
assert.deepEqual(parse({ action: 'find-next', forward: false }),
  { ...base, action: 'find-next', forward: false })
assert.deepEqual(parse({ action: 'find-stop' }), { ...base, action: 'find-stop' })
assert.deepEqual(parse({ action: 'close' }), { ...base, action: 'close' })
assert.deepEqual(parse({ action: 'download-open', downloadId: 'download-1234' }),
  { ...base, action: 'download-open', downloadId: 'download-1234' })
assert.deepEqual(parse({ action: 'zoom', zoomFactor: 0.5 }),
  { ...base, action: 'zoom', zoomFactor: 0.5 })
assert.deepEqual(parse({ action: 'zoom', zoomFactor: 3 }),
  { ...base, action: 'zoom', zoomFactor: 3 })
assert.deepEqual(parse({ action: 'navigate', url: 'https://example.test/path' }),
  { ...base, action: 'navigate', url: 'https://example.test/path' })

for (const request of [
  { action: 'find' },
  { action: 'find', query: 'a'.repeat(513) },
  { action: 'find', query: 42 },
  { action: 'find-next', forward: 'backward' },
  { action: 'find-stop', query: 'stale' },
  { action: 'back', forward: false },
  { action: 'reload', zoomFactor: 1.5 },
  { action: 'zoom', zoomFactor: NaN },
  { action: 'zoom', zoomFactor: Infinity },
  { action: 'zoom', zoomFactor: 0.49 },
  { action: 'zoom', zoomFactor: 3.01 },
  { action: 'zoom' },
  { action: 'download-open' },
  { action: 'download-open', downloadId: '../escape' },
  { action: 'download-open', downloadId: 'x'.repeat(129) },
  { action: 'download-open', downloadId: 123 },
  { action: 'reload', downloadId: 'download-1234' },
]) assert.throws(() => parse(request), Error)

const browserCreate = { version: 2, surfaceId: 'browser:synthetic', kind: 'url-preview',
  payload: { url: 'https://example.test/', scopeId: 'synthetic-session' } }
assert.deepEqual(parseNativeWorkbenchCreateRequest(browserCreate), browserCreate)
assert.deepEqual(parseNativeWorkbenchCreateRequest({ ...browserCreate,
  payload: { ...browserCreate.payload, contextTargetRef: 'page-synthetic' } }),
{ ...browserCreate, payload: { ...browserCreate.payload, contextTargetRef: 'page-synthetic' } })
for (const contextTargetRef of ['', 'x'.repeat(129), 'bad\nref', 123]) {
  assert.throws(() => parseNativeWorkbenchCreateRequest({ ...browserCreate,
    payload: { ...browserCreate.payload, contextTargetRef } }), Error)
}

console.log('Browser navigation contract tests passed')
