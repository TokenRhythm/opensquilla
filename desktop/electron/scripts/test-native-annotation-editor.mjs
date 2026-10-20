import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  parseNativeWorkbenchAnnotationOverlayShowRequest,
  parseNativeWorkbenchAnnotationOverlayUpdateRequest,
  parseNativeWorkbenchAnnotationOverlayMessage,
} from '../dist/native-workbench-annotation-contract.js'

const base = { version: 4, surfaceId: 'document:fixture', selectionId: 'selection_1', annotationId: 'annotation_1', initialBody: '' }
const editorState = { batchCount: 3, busy: false, theme: 'dark' }

test('old overlay requests and submit retain their single-record contract', () => {
  assert.deepEqual(parseNativeWorkbenchAnnotationOverlayShowRequest(base), base)
  assert.deepEqual(parseNativeWorkbenchAnnotationOverlayMessage({ version: 1, type: 'submit', body: 'one' }),
    { version: 1, type: 'submit', body: 'one' })
  assert.throws(() => parseNativeWorkbenchAnnotationOverlayMessage({ version: 1, type: 'request-batch-submit', body: 'one' }))
})

test('editor v2 explicitly negotiates state and distinct record/batch intents', () => {
  const request = { ...base, overlayEditorVersion: 2, editorState }
  assert.deepEqual(parseNativeWorkbenchAnnotationOverlayShowRequest(request), request)
  for (const type of ['record-current', 'request-batch-submit', 'cancel-current', 'draft-changed']) {
    const message = { version: 2, type, body: '保留正文' }
    assert.deepEqual(parseNativeWorkbenchAnnotationOverlayMessage(message), message)
  }
  assert.throws(() => parseNativeWorkbenchAnnotationOverlayShowRequest({ ...base, editorState }))
  assert.throws(() => parseNativeWorkbenchAnnotationOverlayShowRequest({ ...base, overlayEditorVersion: 2 }))
  assert.throws(() => parseNativeWorkbenchAnnotationOverlayMessage({ version: 2, type: 'submit', body: 'ambiguous' }))
})

test('updates remain bound to an opaque annotation and validate bounded state', () => {
  const request = { version: 4, surfaceId: base.surfaceId, annotationId: base.annotationId, editorState }
  assert.deepEqual(parseNativeWorkbenchAnnotationOverlayUpdateRequest(request), request)
  for (const state of [
    { ...editorState, batchCount: 17 }, { ...editorState, batchCount: -1 },
    { ...editorState, busy: 'false' }, { ...editorState, error: 'x'.repeat(1001) },
    { ...editorState, theme: 'custom-script' }, { ...editorState, body: 'cannot replace text' },
  ]) assert.throws(() => parseNativeWorkbenchAnnotationOverlayUpdateRequest({ ...request, editorState: state }))
  assert.throws(() => parseNativeWorkbenchAnnotationOverlayUpdateRequest({ ...request, annotationId: '../bad' }))
})

test('trusted size reports are clamped and cannot supply position or unbounded geometry', () => {
  assert.equal(parseNativeWorkbenchAnnotationOverlayMessage({ version: 2, type: 'content-size', height: 2 }).height, 48)
  assert.equal(parseNativeWorkbenchAnnotationOverlayMessage({ version: 2, type: 'content-size', height: 900 }).height, 320)
  for (const height of [NaN, Infinity, -1, 100_001]) {
    assert.throws(() => parseNativeWorkbenchAnnotationOverlayMessage({ version: 2, type: 'content-size', height }))
  }
  assert.throws(() => parseNativeWorkbenchAnnotationOverlayMessage({ version: 2, type: 'content-size', height: 100, x: 1 }))
})
