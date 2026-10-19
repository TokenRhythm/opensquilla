import assert from 'node:assert/strict'
import { parseDesktopBrowserRequest } from '../dist/desktop-browser.js'
import { browserToolDefinitions } from '../dist/browser-action-contract.js'

const target = { sessionKey: 'synthetic-browser-owner', targetRef: 'synthetic-page' }
const act = fields => parseDesktopBrowserRequest({ ...target, operation: 'act', ...fields })
const batch = action => parseDesktopBrowserRequest({ ...target, operation: 'batch', actions: [action] })
const inspect = fields => parseDesktopBrowserRequest({ ...target, operation: 'snapshot', ...fields })
const rejected = operation => assert.throws(operation, error => error.code === 'INVALID_REQUEST')

const open = fields => parseDesktopBrowserRequest({ sessionKey: target.sessionKey, operation: 'open', ...fields })
assert.equal(open({}).url, 'about:blank')
assert.equal(open({ url: 'about:blank' }).url, 'about:blank')
assert.equal(open({ contextTargetRef: target.targetRef }).url, 'about:blank')
assert.equal(open({ url: 'https://synthetic.test/' }).url, 'https://synthetic.test/')
rejected(() => open({ targetRef: target.targetRef }))
const openSchema = browserToolDefinitions.find(tool => tool.name === 'browser_open').inputSchema
const navigateSchema = browserToolDefinitions.find(tool => tool.name === 'browser_navigate').inputSchema
assert.equal(openSchema.required.includes('url'), false)
assert.deepEqual(navigateSchema.required, ['targetRef', 'url'])

// A press with a duration is a hold, not an accidental delay before an ordinary click.
assert.equal(act({ action: 'click', ref: 'e1', button: 'right' }).button, 'right')
assert.equal(act({ action: 'hold', ref: 'e1', durationMs: 3000 }).durationMs, 3000)
for (const durationMs of [0, -1, 10001, 1.5, '3000', NaN]) {
  rejected(() => act({ action: 'hold', ref: 'e1', durationMs }))
}
rejected(() => act({ action: 'hold', ref: 'e1' }))
rejected(() => act({ action: 'click', ref: 'e1', durationMs: 3000 }))
rejected(() => act({ action: 'fill', ref: 'e1', text: 'value', button: 'right' }))
rejected(() => act({ action: 'click', ref: 'e1', button: 'unknown' }))
rejected(() => act({ action: 'click', ref: 'e1', button: ['left'] }))

assert.equal(act({ action: 'drag', ref: 'e1', endRef: 'e2' }).endRef, 'e2')
rejected(() => act({ action: 'drag', ref: 'e1' }))
rejected(() => act({ action: 'click', ref: 'e1', endRef: 'e2' }))
const image = { observationId: 'synthetic-observation', imageId: 'synthetic-image', x: 5, y: 8 }
const moved = batch({ action: 'drag', ...image, toX: 100, toY: 200 }).actions[0]
assert.equal(moved.toX, 100)
assert.equal(moved.endRef, undefined)
assert.equal(moved.ref, undefined)
assert.equal(batch({ action: 'hold', ...image, durationMs: 3000 }).actions[0].durationMs, 3000)
for (const bad of [
  { action: 'drag', ...image, toX: 100 },
  { action: 'drag', ...image, toX: -1, toY: 200 },
  { action: 'drag', ...image, toX: 100, toY: 200, endRef: 'e2' },
  { action: 'click', ...image, toX: 100, toY: 200 },
  { action: 'drag', ref: 'e1', endRef: 'e2', toX: 100, toY: 200 },
]) rejected(() => batch(bad))

// New capability requests cannot silently become navigation or legacy snapshots.
assert.equal(parseDesktopBrowserRequest({ sessionKey: target.sessionKey, operation: 'open',
  url: 'https://synthetic.test/', contextTargetRef: target.targetRef }).contextTargetRef, target.targetRef)
rejected(() => parseDesktopBrowserRequest({ ...target, operation: 'open',
  url: 'https://synthetic.test/', contextTargetRef: target.targetRef }))
assert.equal(inspect({ ref: 'e1', maxChars: 65536 }).maxChars, 65536)
assert.equal(inspect({ downloadId: 'synthetic-download', maxChars: 120 }).downloadId, 'synthetic-download')
assert.equal(inspect({ scope: '#order-details' }).scope, '#order-details')
rejected(() => inspect({ ref: 'e1', downloadId: 'synthetic-download' }))
rejected(() => inspect({ scope: '#order-details', ref: 'e1' }))
rejected(() => inspect({ scope: '#order-details', downloadId: 'synthetic-download' }))
for (const scope of ['', 12, '#section\0secret', '#section\nbutton', 'a'.repeat(4097)]) {
  rejected(() => inspect({ scope }))
}
rejected(() => parseDesktopBrowserRequest({ ...target, operation: 'observe', scope: '#order-details' }))
const inspectSchema = browserToolDefinitions.find(tool => tool.name === 'browser_inspect').inputSchema
assert.equal(inspectSchema.properties.scope.maxLength, 4096)
assert.equal(Object.hasOwn(browserToolDefinitions.find(tool => tool.name === 'browser_observe').inputSchema.properties, 'scope'), false)
for (const maxChars of [0, -1, 65537, '100']) rejected(() => inspect({ ref: 'e1', maxChars }))
rejected(() => inspect({ maxChars: 100 }))
rejected(() => inspect({ scope: '#order-details', maxChars: 100 }))

assert.equal(act({ action: 'upload', ref: 'e1', fileId: 'att_synthetic' }).fileId, 'att_synthetic')
assert.equal(act({ action: 'upload', chooserId: 'chooser-1', fileId: 'att_synthetic' }).chooserId, 'chooser-1')
assert.equal(act({ action: 'cancelUpload', chooserId: 'chooser-1' }).action, 'cancelUpload')
assert.equal(act({ action: 'download', ref: 'e1' }).action, 'download')
for (const action of [
  { action: 'upload', fileId: 'att_synthetic' },
  { action: 'upload', ref: 'e1', chooserId: 'chooser-1', fileId: 'att_synthetic' },
  { action: 'upload', ref: 'e1', path: '/not-an-attachment.txt' },
  { action: 'upload', ref: 'e1', fileId: 'att_synthetic', uploadFile: { dataBase64: 'dGVzdA==' } },
  { action: 'cancelUpload', ref: 'e1', chooserId: 'chooser-1' },
  { action: 'click', ref: 'e1', fileId: 'att_synthetic' },
  { action: 'download', ref: 'e1', chooserId: 'chooser-1' },
]) rejected(() => act(action))
for (const action of ['upload', 'cancelUpload', 'download']) {
  rejected(() => batch({ action, ref: 'e1' }))
}
console.log('Browser gesture, text, related-tab and file request contracts passed.')

// Examples advertised to the model must pass the same parser as actual calls.
const batchTool = browserToolDefinitions.find(tool => tool.name === 'browser_batch')
for (const form of ['DOM', 'Coordinate']) {
  const example = batchTool.description.match(new RegExp(`${form} drag example: (\\{.*?\\})\\.`))
  assert.ok(example, `${form} drag must have a complete argument example`)
  const args = JSON.parse(example[1])
  const parsed = parseDesktopBrowserRequest({ sessionKey: target.sessionKey, operation: 'batch', ...args })
  assert.equal(parsed.targetRef, args.targetRef)
  assert.deepEqual(parsed.actions, args.actions)
  assert.equal(Object.hasOwn(parsed.actions[0], 'targetRef'), false)
}

// Argument diagnostics identify rejection before dispatch without retaining submitted values.
const diagnostic = (request, field, rule, expected) => assert.throws(
  () => parseDesktopBrowserRequest({ sessionKey: target.sessionKey, ...request }),
  error => {
    assert.equal(error.code, 'INVALID_REQUEST')
    assert.deepEqual(error.details, { phase: 'argument_validation', contractVersion: 1,
      outcome: 'not_started', retryable: false, issues: [{ field, rule, expected }] })
    return true
  },
)
diagnostic({ operation: 'act', targetRef: target.targetRef, action: 'click' }, 'ref', 'required', 'present')
diagnostic({ operation: 'open', targetRef: target.targetRef }, 'url', 'required', 'present')
diagnostic({ operation: 'open', url: '' }, 'url', 'range', 'within_bounds')
diagnostic({ operation: 'open', url: null }, 'url', 'type', 'string')
for (const url of ['synthetic.test', 'https://', ' ', 'file:///synthetic.html', 'javascript:0',
  'data:text/html,synthetic', 'about:config', 'about:blank#fragment', 'about:blank?query',
  'https://synthetic-user:synthetic-secret@synthetic.test/']) {
  diagnostic({ operation: 'open', url }, 'url', 'enum', 'supported_value')
}
diagnostic({ operation: 'dialog', targetRef: target.targetRef, dialogId: 'dialog', accept: 'yes' }, 'accept', 'type', 'boolean')
diagnostic({ operation: 'act', targetRef: target.targetRef, action: 'hold', ref: 'e1', durationMs: 10001 }, 'durationMs', 'range', 'within_bounds')
diagnostic({ operation: 'tab', targetRef: target.targetRef, tabAction: 'erase' }, 'tabAction', 'enum', 'supported_value')
diagnostic({ operation: 'list', 'synthetic-secret-key': 'synthetic-secret-value' }, '$', 'unknown_field', 'known_fields')
diagnostic({ operation: 'snapshot', targetRef: target.targetRef, ref: 'e1', downloadId: 'd1' }, 'ref', 'conflict', 'exclusive_fields')
diagnostic({ operation: 'snapshot', targetRef: target.targetRef, scope: '#order-details', ref: 'e1' }, 'scope', 'conflict', 'exclusive_fields')
diagnostic({ operation: 'snapshot', targetRef: target.targetRef, scope: '#order-details', downloadId: 'd1' }, 'scope', 'conflict', 'exclusive_fields')
diagnostic({ operation: 'snapshot', targetRef: target.targetRef, maxChars: 100 }, 'maxChars', 'dependency', 'related_fields')
diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: [{ action: 'click', ref: 'e1' }, { action: 'click', ref: 'e2' }] },
  'actions.0.action', 'order', 'field_update_before_final')
diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: [{ action: 'fill', ref: 'e1', text: 'safe' }, { action: 'fill', ref: 'e2', text: 'synthetic-secret\0value' }] },
  'actions.1.text', 'range', 'within_bounds')
diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: [{ action: 'click', ref: 'e1', 'synthetic-secret-key': 'value' }] },
  'actions.0', 'unknown_field', 'known_fields')
diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: [null] }, 'actions.0', 'type', 'object')
diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: 'synthetic-secret-value' }, 'actions', 'type', 'array')
diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: [{ action: 'click', ...image, x: Infinity }] },
  'actions.0.x', 'type', 'finite_number')
for (const field of ['ref', 'endRef']) {
  diagnostic({ operation: 'batch', targetRef: target.targetRef,
    actions: [{ action: 'drag', ...image, toX: 100, toY: 200, [field]: 'synthetic-secret-ref' }] },
  `actions.0.${field}`, 'conflict', 'exclusive_fields')
}
diagnostic({ operation: 'batch', targetRef: target.targetRef,
  actions: [{ action: 'fill', ...image, text: 'synthetic-secret-value' }] },
  'actions.0.action', 'dependency', 'related_fields')
for (const field of ['imageId', 'observationId']) {
  const action = { action: 'drag', ...image, toX: 100, toY: 200 }
  delete action[field]
  diagnostic({ operation: 'batch', targetRef: target.targetRef, actions: [action] },
    `actions.0.${field}`, 'required', 'present')
}
assert.throws(() => parseDesktopBrowserRequest({ operation: 'list', sessionKey: '' }), error => {
  assert.equal(error.details.phase, undefined, 'authenticated identity is outside public argument diagnostics')
  return true
})

// Existing normalization remains compatible: unused legacy action fields were ignored.
for (const ignored of [{ text: 12 }, { key: null }, { direction: ['left'] }, { amount: -1 }]) {
  assert.deepEqual(act({ action: 'click', ref: 'e1', ...ignored }), { ...target, operation: 'act', action: 'click', ref: 'e1' })
}
assert.equal(act({ action: 'scroll', direction: 'right' }).amount, 600)
assert.equal(parseDesktopBrowserRequest({ ...target, operation: 'open', url: 'https://synthetic.test/' }).targetRef, target.targetRef)
assert.equal(act({ action: 'fill', ref: 'e1', text: '' }).text, '')
assert.equal(act({ action: 'fill', ref: 'e1', text: 'first\n\nsecond' }).text, 'first\n\nsecond')
console.log('Browser argument diagnostic safety and legacy normalization passed.')

// Prompt maxLength follows JSON Schema's Unicode code-point semantics.
const promptSchema = browserToolDefinitions.find(tool => tool.name === 'browser_handle_dialog')
  .inputSchema.properties.promptText
assert.equal(promptSchema.maxLength, 16384)
const prompt = promptText => parseDesktopBrowserRequest({ ...target, operation: 'dialog',
  dialogId: 'synthetic-prompt', accept: true, promptText })
for (const promptText of ['', 'a'.repeat(4096), 'a'.repeat(4097), 'a'.repeat(16384),
  '文'.repeat(16384), '😀'.repeat(16384), '😀'.repeat(16383) + 'a']) {
  assert.equal(prompt(promptText).promptText, promptText)
}
for (const promptText of ['a'.repeat(16385), '文'.repeat(16385),
  '😀'.repeat(16384) + 'a', '😀'.repeat(16385), '\0']) {
  diagnostic({ operation: 'dialog', targetRef: target.targetRef, dialogId: 'synthetic-prompt',
    accept: true, promptText }, 'promptText', 'range', 'within_bounds')
}
assert.equal(prompt('😀'.repeat(16384)).promptText.length, 32768)
// Other text inputs keep their established UTF-16 bound.
rejected(() => act({ action: 'fill', ref: 'e1', text: '😀'.repeat(8193) }))
console.log('Prompt argument boundaries preserve the public Unicode maxLength contract.')
