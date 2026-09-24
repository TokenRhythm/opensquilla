import assert from 'node:assert/strict'
import { request as httpRequest } from 'node:http'
import { mock } from 'node:test'
import {
  DesktopBrowserError,
  DesktopBrowserServer,
  DESKTOP_BROWSER_URL_ENV,
  DESKTOP_BROWSER_TOKEN_ENV,
} from '../dist/desktop-browser.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1kAAAAASUVORK5CYII='
const calls = []
const audit = []
const gates = new Map()
const failures = new Map()
const ownedTargets = new Map([
  ['session-a', 'opaque-primary-target'], ['session-b', 'opaque-secondary-target'],
])
const targetFor = sessionKey => {
  if (!ownedTargets.has(sessionKey)) ownedTargets.set(sessionKey, `opaque-target-${ownedTargets.size + 1}`)
  return ownedTargets.get(sessionKey)
}
let legacyCalls = 0
let observationSequence = 0
const server = new DesktopBrowserServer(
  async request => { legacyCalls++; return { legacy: true, session: request.sessionKey } },
  entry => audit.push(entry),
  async (request, signal) => {
    calls.push(request)
    if (request.operation !== 'list') failures.get(request.sessionKey)?.(request)
    const gate = gates.get(request.sessionKey)
    if (gate) {
      signal.addEventListener('abort', () => {
        gate.aborted.resolve()
        if (!gate.ignoreAbort) gate.completed.reject(new DesktopBrowserError('TIMEOUT', 'Fixture operation interrupted.'))
      }, { once: true })
      gate.started.resolve()
      return await gate.completed.promise
    }
    if (request.targetRef === 'foreign-target') {
      throw new DesktopBrowserError('TARGET_NOT_FOUND', 'Target is outside this session.')
    }
    if (request.operation === 'screenshot') {
      return { targetRef: request.targetRef, mime: 'image/png', width: 1, height: 1, dataBase64: png }
    }
    if (request.operation === 'list') return { targets: [{ targetRef: targetFor(request.sessionKey) }] }
    if (['observe', 'batch', 'dialog'].includes(request.operation)
      || request.operation === 'tab' && request.tabAction === 'switch') {
      const observationId = `observation-${++observationSequence}`
      const imageId = `image-${observationSequence}`
      const withImage = request.observationMode !== 'dom'
      return {
        targetRef: request.targetRef, operation: request.operation,
        observation: {
          targetRef: request.targetRef, observationId,
          consistency: 'consistent',
          text: 'Synthetic page state', refs: { 'element-1': { role: 'button', name: 'Continue' } },
          ...(withImage ? { image: { imageId, width: 1, height: 1 } } : {}),
        },
        ...(withImage ? { dataBase64: png } : {}),
      }
    }
    return { targetRef: request.targetRef ?? targetFor(request.sessionKey), operation: request.operation }
  },
)
const environment = await server.start()
const endpoint = environment[DESKTOP_BROWSER_URL_ENV]
const token = environment[DESKTOP_BROWSER_TOKEN_ENV]
const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
let requestId = 0
const envelope = (method, params) => ({ jsonrpc: '2.0', id: ++requestId, method, ...(params === undefined ? {} : { params }) })
const invoke = (body, extraHeaders = {}) => fetch(`${endpoint}/mcp`, {
  method: 'POST', headers: { ...headers, ...extraHeaders }, body: JSON.stringify(body),
})
const rpc = async (method, params) => {
  const body = envelope(method, params)
  const response = await invoke(body)
  assert.equal(response.status, 200)
  const result = await response.json()
  assert.equal(result.jsonrpc, '2.0')
  assert.equal(result.id, body.id)
  return result
}
const toolParams = (name, args = {}, sessionKey = 'session-a', operationId = `operation-${++requestId}`) => ({
  name, arguments: args, _meta: { sessionKey, operationId },
})
const call = (name, args = {}, sessionKey, operationId) => rpc('tools/call', toolParams(name, args, sessionKey, operationId))
const scopedCall = (name, args, sessionKey, recoveryScope, operationId) => {
  const params = toolParams(name, args, sessionKey, operationId)
  if (recoveryScope !== undefined) params._meta.recoveryScope = recoveryScope
  return rpc('tools/call', params)
}
const gateFor = sessionKey => {
  const gate = { started: deferred(), completed: deferred(), aborted: deferred() }
  gates.set(sessionKey, gate)
  return gate
}
const count = sessionKey => calls.filter(request => request.sessionKey === sessionKey).length

try {
  for (const protocolVersion of ['2024-11-05', '2025-06-18']) {
    const initialized = await rpc('initialize', { protocolVersion, capabilities: {}, clientInfo: { name: 'fixture', version: '1' } })
    assert.equal(initialized.result.protocolVersion, protocolVersion)
    assert.deepEqual(initialized.result.capabilities, {
      tools: {},
      experimental: { 'opensquilla/browser': { version: 2, observation: true, batch: true, dialogs: true, jsPrompt: false, coordinateAuthority: 'browser-state' } },
    })
  }
  const notified = await invoke({ jsonrpc: '2.0', method: 'notifications/initialized' })
  assert.equal(notified.status, 202)
  assert.equal(await notified.text(), '')
  assert.deepEqual((await rpc('ping')).result, {})
  assert.equal((await rpc('unknown/method')).error.code, -32601)

  const tools = (await rpc('tools/list')).result.tools
  assert.deepEqual(tools.map(tool => tool.name), [
    'browser_tabs', 'browser_open', 'browser_navigate', 'browser_reload',
    'browser_inspect', 'browser_act', 'browser_screenshot',
    'browser_observe', 'browser_batch', 'browser_handle_dialog', 'browser_tab',
  ])
  for (const tool of tools) {
    assert.equal(tool.inputSchema.type, 'object')
    assert.equal(tool.inputSchema.additionalProperties, false)
    assert.equal('sessionKey' in tool.inputSchema.properties, false)
    assert.equal('operationId' in tool.inputSchema.properties, false)
    assert.equal('recoveryScope' in tool.inputSchema.properties, false)
    assert.equal('_meta' in tool.inputSchema.properties, false)
    assert.equal('nativeImageEvidence' in tool.inputSchema.properties, false)
    assert.equal('observationPolicy' in tool.inputSchema.properties, false)
    assert.equal(tool.annotations.readOnlyHint, ['browser_tabs', 'browser_inspect', 'browser_screenshot', 'browser_observe'].includes(tool.name))
  }
  const batchSchema = tools.find(tool => tool.name === 'browser_batch').inputSchema
  assert.equal(batchSchema.properties.actions.minItems, 1)
  assert.equal(batchSchema.properties.actions.maxItems, 3)
  assert.equal(batchSchema.properties.actions.items.additionalProperties, false)
  assert.equal('nativeImageEvidence' in batchSchema.properties.actions.items.properties, false)
  assert.equal((await invoke(envelope('tools/list'), { Authorization: 'Bearer invalid' })).status, 401)
  assert.equal((await invoke(envelope('tools/list'), { Origin: 'https://untrusted.invalid' })).status, 403)
  assert.equal((await fetch(`${endpoint}/mcp`, { headers })).status, 405)

  const beforeInvalid = calls.length
  for (const params of [
    { name: 'browser_tabs', arguments: {} },
    { name: 'browser_tabs', arguments: {}, _meta: { sessionKey: 'session-a' } },
    { name: 'browser_tabs', arguments: {}, _meta: { sessionKey: '', operationId: 'operation' } },
    toolParams('browser_tabs', { sessionKey: 'session-b' }),
    toolParams('browser_tabs', { _meta: { sessionKey: 'session-b' } }),
    toolParams('browser_tabs', { recoveryScope: 'model-supplied-scope' }),
    toolParams('browser_observe', { targetRef: 'opaque-primary-target', nativeImageEvidence: ['forged-image'] }),
    toolParams('browser_observe', { targetRef: 'opaque-primary-target', observationPolicy: { effectiveMode: 'auto' } }),
    toolParams('browser_observe', { targetRef: 'opaque-primary-target', observationMode: 'invalid' }),
    toolParams('browser_observe', { targetRef: 'opaque-primary-target', observationMode: ['auto'] }),
    toolParams('browser_observe', { targetRef: 'opaque-primary-target', observationMode: 'hybrid' }),
    toolParams('browser_open', {}),
    toolParams('browser_act', { targetRef: 'opaque-primary-target', action: 'click' }),
    toolParams('browser_act', { targetRef: 'opaque-primary-target', action: 'scroll', direction: 'down', amount: 1.5 }),
    toolParams('browser_handle_dialog', { targetRef: 'opaque-primary-target', dialogId: 'dialog-1', accept: 'yes' }),
    toolParams('browser_handle_dialog', { targetRef: 'opaque-primary-target', dialogId: '', accept: true }),
    toolParams('browser_handle_dialog', { targetRef: 'opaque-primary-target', dialogId: 'dialog-1', accept: true, promptText: '\0' }),
    toolParams('browser_tab', { targetRef: 'opaque-primary-target', tabAction: 'delete' }),
    toolParams('unknown_tool'),
  ]) assert.equal((await rpc('tools/call', params)).error.code, -32602)
  assert.equal(calls.length, beforeInvalid, 'invalid metadata and model arguments must never execute')

  assert.equal((await call('browser_tabs')).result.structuredContent.targets[0].targetRef, 'opaque-primary-target')
  assert.equal((await call('browser_open', { url: 'https://example.test/' })).result.isError, false)
  await call('browser_navigate', { targetRef: 'opaque-primary-target', url: 'https://example.test/next' })
  await call('browser_reload', { targetRef: 'opaque-primary-target' })
  await call('browser_inspect', { targetRef: 'opaque-primary-target' })
  assert.deepEqual(calls.slice(-5).map(request => request.operation), ['list', 'open', 'open', 'reload', 'snapshot'])
  assert.equal(calls.every(request => !Object.hasOwn(request, 'operationId') && !Object.hasOwn(request, '_meta')), true)

  const action = { targetRef: 'opaque-primary-target', action: 'fill', ref: 'element-1', text: 'Synthetic value' }
  const beforeAction = calls.length
  const first = await call('browser_act', action, 'session-a', 'shared-operation')
  const replay = await call('browser_act', { text: 'Synthetic value', ref: 'element-1', action: 'fill', targetRef: 'opaque-primary-target' }, 'session-a', 'shared-operation')
  assert.deepEqual(replay.result, first.result)
  assert.equal(calls.length, beforeAction + 1, 'identical mutation retries must return the original receipt')
  assert.equal((await call('browser_act', { ...action, text: 'Changed' }, 'session-a', 'shared-operation')).error.code, -32602)
  assert.equal(calls.length, beforeAction + 1)
  await call('browser_act', { ...action, targetRef: 'opaque-secondary-target' }, 'session-b', 'shared-operation')
  assert.equal(calls.at(-1).sessionKey, 'session-b', 'operation receipts must be scoped by trusted session')
  assert.equal(calls.length, beforeAction + 2)

  const foreign = await call('browser_inspect', { targetRef: 'foreign-target' })
  assert.equal(foreign.result.isError, true)
  assert.equal(foreign.result.structuredContent.code, 'TARGET_NOT_FOUND')
  const screenshot = (await call('browser_screenshot', { targetRef: 'opaque-primary-target' })).result
  assert.equal(screenshot.isError, false)
  assert.deepEqual(screenshot.content[1], { type: 'image', mimeType: 'image/png', data: png })
  assert.equal(JSON.stringify(screenshot.structuredContent).includes(png), false)
  assert.equal(screenshot.content[0].text.includes(png), false)

  const observed = (await call('browser_observe', { targetRef: 'opaque-primary-target' })).result
  const observation = observed.structuredContent.observation
  assert.equal(observed.isError, false)
  assert.equal(observation.targetRef, 'opaque-primary-target')
  assert.equal(observed.content[1].type, 'image')
  assert.deepEqual(observed.content[1]._meta, {
    'opensquilla/browserObservation': {
      targetRef: 'opaque-primary-target', observationId: observation.observationId, imageId: observation.image.imageId,
    },
  })
  assert.equal(JSON.stringify(observed.structuredContent).includes(png), false)
  assert.equal(observed.content[0].text.includes(png), false)

  // Runtime image capability is independent of a model's observation preference.
  for (const [modelMode, runtimeMode, expectedMode] of [
    ['auto', 'dom', 'dom'], ['dom', 'auto', 'dom'], ['auto', 'auto', 'auto'],
  ]) {
    const params = toolParams('browser_observe', { targetRef: 'opaque-primary-target', observationMode: modelMode })
    params._meta.observationMode = runtimeMode
    params._meta.nativeImageEvidence = [observation.image.imageId]
    const result = (await rpc('tools/call', params)).result
    assert.equal(calls.at(-1).observationMode, expectedMode)
    assert.equal(Object.hasOwn(calls.at(-1), 'nativeImageEvidence'), false,
      'legacy image metadata is accepted without becoming executor authority')
    assert.equal(result.content.some(block => block.type === 'image'), expectedMode !== 'dom')
  }
  const beforeBadRuntime = calls.length
  for (const invalidMeta of [
    { recoveryScope: '' },
    { recoveryScope: 42 },
    { recoveryScope: ['scope'] },
    { recoveryScope: 'scope\0' },
    { recoveryScope: 's'.repeat(513) },
    { observationMode: 'invalid' },
    { observationMode: ['auto'] },
    { nativeImageEvidence: 'image-1' },
    { nativeImageEvidence: null },
    { nativeImageEvidence: {} },
    { nativeImageEvidence: [''] },
    { nativeImageEvidence: ['image-1', null] },
    { nativeImageEvidence: ['image\0'] },
    { nativeImageEvidence: ['i'.repeat(513)] },
    { nativeImageEvidence: Array.from({ length: 65 }, (_, index) => `image-${index}`) },
  ]) {
    const params = toolParams('browser_observe', { targetRef: 'opaque-primary-target' })
    Object.assign(params._meta, invalidMeta)
    assert.equal((await rpc('tools/call', params)).error.code, -32602)
  }
  assert.equal(calls.length, beforeBadRuntime)

  const policy = {
    requestedMode: 'auto', effectiveMode: 'auto', omittedReason: null,
    activeVisionSupport: 'supported', routingAuthority: 'automatic',
  }
  for (const [modelMode, runtimeMode, expectedMode] of [
    ['auto', 'auto', 'auto'], ['auto', 'dom', 'dom'], ['dom', 'auto', 'dom'],
  ]) {
    const params = toolParams('browser_observe', { targetRef: 'opaque-primary-target', observationMode: modelMode })
    params._meta.observationMode = runtimeMode
    params._meta.observationPolicy = policy
    const result = (await rpc('tools/call', params)).result
    assert.equal(result.isError, false)
    assert.equal(Object.hasOwn(result.structuredContent.observation, 'policy'), false)
    assert.equal(result.content.some(block => block.type === 'image'), expectedMode === 'auto',
      'legacy policy metadata cannot override the requested observation mode')
    assert.equal(Object.hasOwn(calls.at(-1), 'observationPolicy'), false)
  }
  const beforeInvalidPolicy = calls.length
  for (const observationPolicy of [
    'automatic', [],
    { ...policy, requestedMode: 'hybrid' },
    { ...policy, requestedMode: ['auto'] },
    { ...policy, effectiveMode: 'hybrid' },
    { ...policy, effectiveMode: ['auto'] },
    { ...policy, omittedReason: 'unrecognized' },
    { ...policy, omittedReason: ['runtime_dom'] },
    { ...policy, activeVisionSupport: 'maybe' },
    { ...policy, activeVisionSupport: ['supported'] },
    { ...policy, routingAuthority: 'model-selected' },
    { ...policy, routingAuthority: ['automatic'] },
  ]) {
    const params = toolParams('browser_observe', { targetRef: 'opaque-primary-target' })
    params._meta.observationPolicy = observationPolicy
    assert.equal((await rpc('tools/call', params)).error?.code, -32602,
      `invalid observation diagnostics must be rejected: ${JSON.stringify(observationPolicy)}`)
  }
  assert.equal(calls.length, beforeInvalidPolicy)

  const shortActions = [
    { action: 'fill', ref: 'field-1', text: 'Synthetic entry' },
    { action: 'select', ref: 'select-1', text: 'option-a' },
    { action: 'click', ref: 'submit-1' },
  ]
  const batchArgs = { targetRef: 'opaque-primary-target', actions: shortActions }
  const beforeBatch = calls.length
  const batch = (await call('browser_batch', batchArgs, 'session-a', 'batch-operation')).result
  assert.equal(batch.isError, false)
  assert.equal(calls.at(-1).operation, 'batch')
  assert.deepEqual(calls.at(-1).actions, shortActions)
  assert.deepEqual(batch.content[1]._meta['opensquilla/browserObservation'], {
    targetRef: 'opaque-primary-target',
    observationId: batch.structuredContent.observation.observationId,
    imageId: batch.structuredContent.observation.image.imageId,
  })
  const batchReplay = (await call('browser_batch', {
    actions: shortActions, targetRef: 'opaque-primary-target',
  }, 'session-a', 'batch-operation')).result
  assert.deepEqual(batchReplay, batch, 'batch replay must retain the original observation and image')
  assert.equal(calls.length, beforeBatch + 1)
  assert.equal((await call('browser_batch', {
    ...batchArgs, actions: [{ action: 'click', ref: 'different-submit' }],
  }, 'session-a', 'batch-operation')).error.code, -32602)
  assert.equal(calls.length, beforeBatch + 1, 'changed batch arguments cannot reuse a mutation receipt')

  const beforeInvalidBatch = calls.length
  for (const actions of [
    [], Array.from({ length: 4 }, () => ({ action: 'fill', ref: 'field-1', text: 'value' })),
    [{ action: 'click', ref: 'next-1' }, { action: 'click', ref: 'next-2' }],
    [{ action: 'hover', ref: 'next-1' }, { action: 'fill', ref: 'field-1', text: 'value' }],
    [{ action: 'click', ref: 'next-1', nativeImageEvidence: ['forged-image'] }],
    [{ action: 'click', ref: 'next-1', _meta: { nativeImageEvidence: ['forged-image'] } }],
    [{ action: 'click', x: 1, y: 1, imageId: 'image-1' }],
    [{ action: 'click', ref: 'next-1', x: 1, y: 1, imageId: 'image-1', observationId: 'observation-1' }],
    [{ action: 'fill', x: 1, y: 1, imageId: 'image-1', observationId: 'observation-1', text: 'value' }],
    [{ action: 'click', x: -1, y: 1, imageId: 'image-1', observationId: 'observation-1' }],
    [{ action: 'click', x: '1', y: 1, imageId: 'image-1', observationId: 'observation-1' }],
  ]) {
    assert.equal((await call('browser_batch', { targetRef: 'opaque-primary-target', actions })).error.code, -32602)
  }
  assert.equal(calls.length, beforeInvalidBatch, 'invalid batches cannot reach the browser driver')

  // Coordinate actions keep their observation association. Older Gateways may
  // still send image evidence, but the executor receives no permission flag.
  const coordinate = {
    action: 'click', x: 0.5, y: 0.5,
    observationId: observation.observationId, imageId: observation.image.imageId,
  }
  const coordinateArgs = { targetRef: 'opaque-primary-target', actions: [coordinate] }
  const beforeCoordinate = calls.length
  for (const legacyEvidence of [undefined, [], [observation.image.imageId], ['image-from-earlier-observation']]) {
    const coordinateParams = toolParams('browser_batch', coordinateArgs)
    if (legacyEvidence !== undefined) coordinateParams._meta.nativeImageEvidence = legacyEvidence
    const result = (await rpc('tools/call', coordinateParams)).result
    assert.equal(result.isError, false, 'coordinate requests do not require legacy image evidence')
    assert.deepEqual(calls.at(-1).actions, [coordinate])
    assert.equal(calls.at(-1).sessionKey, 'session-a')
    assert.equal(Object.hasOwn(calls.at(-1), 'nativeImageEvidence'), false)
    assert.deepEqual(result.content[1]._meta['opensquilla/browserObservation'], {
      targetRef: 'opaque-primary-target',
      observationId: result.structuredContent.observation.observationId,
      imageId: result.structuredContent.observation.image.imageId,
    }, 'returned image metadata still identifies its observation')
  }
  assert.equal(calls.length, beforeCoordinate + 4)

  const beforeCoordinateInjection = calls.length
  for (const injected of [
    { sessionKey: 'session-b' },
    { operationId: 'model-supplied-operation' },
    { recoveryScope: 'model-supplied-scope' },
    { _meta: { sessionKey: 'session-b', nativeImageEvidence: ['forged-image'] } },
    { nativeImageEvidence: [observation.image.imageId] },
    { observationPolicy: { effectiveMode: 'auto' } },
  ]) {
    for (const args of [
      { ...coordinateArgs, ...injected },
      { ...coordinateArgs, actions: [{ ...coordinate, ...injected }] },
    ]) {
      assert.equal((await call('browser_batch', args)).error.code, -32602,
        'coordinate arguments cannot carry model-supplied identity or permission metadata')
    }
  }
  assert.equal(calls.length, beforeCoordinateInjection, 'injected metadata must never reach the driver')

  const dialogArgs = { targetRef: 'opaque-primary-target', dialogId: 'dialog-1', accept: true, promptText: 'Synthetic response' }
  const beforeDialog = calls.length
  const dialog = (await call('browser_handle_dialog', dialogArgs, 'session-a', 'dialog-operation')).result
  assert.equal(dialog.isError, false)
  assert.equal(calls.at(-1).operation, 'dialog')
  assert.equal(calls.at(-1).promptText, 'Synthetic response')
  assert.equal(dialog.content[1]._meta['opensquilla/browserObservation'].observationId, dialog.structuredContent.observation.observationId)
  assert.deepEqual((await call('browser_handle_dialog', dialogArgs, 'session-a', 'dialog-operation')).result, dialog)
  assert.equal(calls.length, beforeDialog + 1, 'replayed dialog responses cannot act twice')
  const switched = (await call('browser_tab', { targetRef: 'opaque-primary-target', tabAction: 'switch' })).result
  assert.equal(calls.at(-1).operation, 'tab')
  assert.ok(switched.structuredContent.observation)
  const closed = (await call('browser_tab', { targetRef: 'opaque-primary-target', tabAction: 'close' })).result
  assert.equal(calls.at(-1).tabAction, 'close')
  assert.equal(closed.content.some(block => block.type === 'image'), false)

  const readParams = toolParams('browser_observe', { targetRef: 'opaque-primary-target' }, 'session-a', 'read-operation')
  const readOne = (await rpc('tools/call', readParams)).result
  const readTwo = (await rpc('tools/call', readParams)).result
  assert.notEqual(readOne.structuredContent.observation.observationId, readTwo.structuredContent.observation.observationId,
    'read-only observations must stay fresh instead of using mutation receipts')

  // A failed initial navigation can still create a usable page. Preserve the
  // diagnostic receipt before enforcing the next recovery admission decision.
  const tlsSession = 'synthetic-tls-session'
  const tlsScope = 'synthetic-tls-turn'
  const tlsUrl = 'https://certificate-fixture.test/private?access_token=synthetic-url-secret'
  const tlsTarget = 'partial-navigation-target'
  const tlsDetails = {
    targetRef: tlsTarget, operation: 'open', pageState: 'navigation_failed',
    navigation: { url: tlsUrl, code: 'ERR_CERT_AUTHORITY_INVALID', errorCode: -202 },
    outcome: 'completed', retryable: false, recovery: 'change_url_or_network',
  }
  const failTls = request => {
    if (request.operation === 'observe') return
    throw new DesktopBrowserError('NAVIGATION_FAILED', 'Synthetic certificate failure.', 409, {
      ...tlsDetails, targetRef: request.targetRef ?? tlsTarget, operation: request.operation,
      navigation: { ...tlsDetails.navigation, url: request.url ?? tlsUrl },
    })
  }
  failures.set(tlsSession, failTls)
  const partialOpen = (await scopedCall('browser_open', { url: tlsUrl }, tlsSession, tlsScope, 'partial-open')).result
  assert.equal(partialOpen.isError, true)
  for (const [key, value] of Object.entries(tlsDetails)) assert.deepEqual(partialOpen.structuredContent[key], value)
  assert.equal(partialOpen.structuredContent.code, 'NAVIGATION_FAILED')
  assert.equal(partialOpen.structuredContent.operationId, 'partial-open')
  assert.equal(partialOpen.structuredContent.recoveryBudget.attempts, 1)
  assert.equal(partialOpen.structuredContent.recoveryBudget.limit, 1)
  assert.equal(partialOpen.structuredContent.recoveryBudget.exhausted, true)
  assert.equal(typeof partialOpen.structuredContent.recoveryBudget.domain, 'string')
  assert.deepEqual(JSON.parse(partialOpen.content[0].text), partialOpen.structuredContent)
  assert.equal(JSON.stringify(partialOpen).includes(tlsSession), false)
  assert.deepEqual((await scopedCall('browser_open', { url: tlsUrl }, tlsSession, tlsScope, 'partial-open')).result, partialOpen)
  assert.equal(count(tlsSession), 1, 'a partial-open receipt must survive an exhausted recovery budget')
  assert.equal(audit.at(-1).outcome, 'rejected')
  assert.equal(audit.at(-1).code, 'NAVIGATION_FAILED', 'HTTP 200 MCP errors must retain their real audit outcome')

  const assertExhausted = (result, causeCode) => {
    assert.equal(result.isError, true)
    assert.equal(result.structuredContent.code, 'BROWSER_RECOVERY_EXHAUSTED')
    assert.equal(result.structuredContent.causeCode, causeCode)
    assert.equal(result.structuredContent.outcome, 'not_started')
    assert.equal(result.structuredContent.retryable, false)
    assert.equal(result.structuredContent.recoveryBudget.exhausted, true)
    assert.equal(audit.at(-1).outcome, 'rejected')
    assert.equal(audit.at(-1).code, 'BROWSER_RECOVERY_EXHAUSTED')
  }
  await scopedCall('browser_tabs', {}, tlsSession, tlsScope)
  for (const [name, args] of [
    ['browser_navigate', { targetRef: tlsTarget, url: `${tlsUrl}&attempt=2` }],
    ['browser_reload', { targetRef: tlsTarget }],
    ['browser_open', { url: 'https://certificate-fixture.test/another-path' }],
  ]) {
    const before = count(tlsSession)
    assertExhausted((await scopedCall(name, args, tlsSession, tlsScope)).result, 'NAVIGATION_FAILED')
    assert.equal(count(tlsSession), before, 'changing operation identity or tool cannot bypass a TLS recovery limit')
  }
  await scopedCall('browser_observe', { targetRef: tlsTarget }, tlsSession, tlsScope)
  const beforeTlsObservationRetry = count(tlsSession)
  assertExhausted((await scopedCall('browser_reload', { targetRef: tlsTarget }, tlsSession, tlsScope)).result, 'NAVIGATION_FAILED')
  assert.equal(count(tlsSession), beforeTlsObservationRetry, 'observing an error page cannot clear a terminal TLS failure')

  // An origin includes scheme and port; failures at one service must not
  // prevent a different service or a later trusted recovery scope from running.
  for (const url of ['https://another-fixture.test/', 'https://certificate-fixture.test:8443/', 'http://certificate-fixture.test/']) {
    const before = count(tlsSession)
    const result = (await scopedCall('browser_open', { url }, tlsSession, tlsScope)).result
    assert.equal(result.structuredContent.code, 'NAVIGATION_FAILED')
    assert.equal(count(tlsSession), before + 1)
  }
  const beforeNewScope = count(tlsSession)
  assert.equal((await scopedCall('browser_open', { url: tlsUrl }, tlsSession, 'synthetic-next-turn')).result.structuredContent.code, 'NAVIGATION_FAILED')
  assert.equal(count(tlsSession), beforeNewScope + 1)
  failures.set('synthetic-other-tls-session', failTls)
  assert.equal((await scopedCall('browser_open', { url: tlsUrl }, 'synthetic-other-tls-session', tlsScope)).result.structuredContent.code, 'NAVIGATION_FAILED')
  assert.equal(count('synthetic-other-tls-session'), 1, 'recovery history must not cross trusted sessions')

  for (const [code, errorCode] of [['ERR_SSL_PROTOCOL_ERROR', -107], ['ERR_NAME_NOT_RESOLVED', -105]]) {
    const session = `synthetic-${code.toLowerCase()}-session`
    failures.set(session, request => {
      throw new DesktopBrowserError('NAVIGATION_FAILED', 'Synthetic origin failure.', 409, {
        targetRef: 'origin-failure-page', operation: request.operation, pageState: 'navigation_failed',
        navigation: { url: request.url, code, errorCode }, outcome: 'completed', retryable: false,
      })
    })
    const first = (await call('browser_open', { url: 'https://origin-fixture.test/first' }, session)).result
    assert.equal(first.structuredContent.recoveryBudget.limit, 1)
    assertExhausted((await call('browser_open', { url: 'https://origin-fixture.test/another' }, session)).result, 'NAVIGATION_FAILED')
    assert.equal(count(session), 1, 'TLS and DNS failures must stop repeated navigation to the same origin')
  }

  const failedNavigationSession = 'synthetic-navigation-recovery-session'
  const failedNavigationTarget = 'navigation-recovery-page'
  const failedNavigationUrl = 'https://navigation-fixture.test/failing-path'
  let currentNavigationUrl = failedNavigationUrl
  failures.set(failedNavigationSession, request => {
    if (request.operation === 'open') currentNavigationUrl = request.url.split('#')[0]
    if (!['open', 'reload'].includes(request.operation) || currentNavigationUrl !== failedNavigationUrl) return
    throw new DesktopBrowserError('NAVIGATION_FAILED', 'Synthetic connection closed.', 409, {
      targetRef: request.targetRef ?? failedNavigationTarget, operation: request.operation,
      pageState: 'navigation_failed',
      navigation: { url: request.url ?? currentNavigationUrl, code: 'ERR_CONNECTION_CLOSED', errorCode: -100 },
      outcome: 'completed', retryable: false,
    })
  })
  const firstConnectionFailure = (await call('browser_open', { url: failedNavigationUrl }, failedNavigationSession)).result
  assert.equal(firstConnectionFailure.structuredContent.recoveryBudget.attempts, 1)
  assert.equal(firstConnectionFailure.structuredContent.recoveryBudget.limit, 2)
  const secondConnectionFailure = (await call('browser_navigate', {
    targetRef: failedNavigationTarget, url: `${failedNavigationUrl}#second-attempt`,
  }, failedNavigationSession)).result
  assert.equal(secondConnectionFailure.structuredContent.recoveryBudget.attempts, 2)
  await call('browser_tabs', {}, failedNavigationSession)
  await call('browser_observe', { targetRef: failedNavigationTarget }, failedNavigationSession)
  for (const url of [failedNavigationUrl, `${failedNavigationUrl}#different-fragment`]) {
    const before = count(failedNavigationSession)
    assertExhausted((await call('browser_open', { url }, failedNavigationSession)).result, 'NAVIGATION_FAILED')
    assert.equal(count(failedNavigationSession), before,
      'an observed error page or changed fragment cannot reset the failed navigation budget')
  }
  const workingNavigationUrl = 'https://navigation-fixture.test/working-path'
  assert.equal((await call('browser_navigate', {
    targetRef: failedNavigationTarget, url: workingNavigationUrl,
  }, failedNavigationSession)).result.isError, false,
  'a URL-specific connection failure must not block a different path on the same origin')
  assert.equal((await call('browser_reload', { targetRef: failedNavigationTarget }, failedNavigationSession)).result.isError, false,
    'the former failure must not remain attached to a target that navigated elsewhere')
  const beforeOldUrlRetry = count(failedNavigationSession)
  assertExhausted((await call('browser_open', { url: failedNavigationUrl }, failedNavigationSession)).result, 'NAVIGATION_FAILED')
  assert.equal(count(failedNavigationSession), beforeOldUrlRetry,
    'working navigation elsewhere cannot erase the original failed URL budget')

  const initializationSession = 'synthetic-initialization-session'
  const initializationUrl = 'https://initialization-fixture.test/'
  failures.set(initializationSession, request => {
    throw new DesktopBrowserError('PAGE_NOT_READY', 'Synthetic browser initialization failure.', 409, {
      targetRef: 'initializing-target', operation: request.operation, pageState: 'initializing',
      navigation: { url: request.url, code: 'ERR_BROWSER_INITIALIZATION_FAILED' },
      outcome: 'not_started', retryable: true,
    })
  })
  for (const attempt of [1, 2]) {
    const result = (await call('browser_open', { url: initializationUrl }, initializationSession)).result
    assert.equal(result.structuredContent.code, 'PAGE_NOT_READY')
    assert.equal(result.structuredContent.recoveryBudget.attempts, attempt)
    assert.equal(result.structuredContent.recoveryBudget.limit, 2)
    assert.equal(result.structuredContent.retryable, attempt === 1)
  }
  assertExhausted((await call('browser_open', { url: initializationUrl }, initializationSession)).result, 'PAGE_NOT_READY')
  assert.equal(count(initializationSession), 2, 'browser initialization is recoverable but must retain a bounded retry budget')

  const unknownOpenSession = 'synthetic-unknown-open-session'
  const unknownOpenScope = 'unknown-open-turn'
  const unknownOpenUrl = 'https://unknown-open-fixture.test/first#initial'
  failures.set(unknownOpenSession, request => {
    if (request.operation === 'open') throw new DesktopBrowserError('TIMEOUT', 'Synthetic open outcome is unknown.')
    if (request.targetRef === 'unreadable-target') throw new DesktopBrowserError('BROWSER_UNAVAILABLE', 'Synthetic target read failure.')
  })
  const unknownOpen = (await scopedCall('browser_open', { url: unknownOpenUrl }, unknownOpenSession, unknownOpenScope, 'unknown-open')).result
  assert.equal(unknownOpen.structuredContent.outcome, 'unknown')
  assert.equal(unknownOpen.structuredContent.retryable, false)
  assert.equal(unknownOpen.structuredContent.targetRef, undefined)
  assert.equal(unknownOpen.structuredContent.recoveryBudget.limit, 1)
  assert.equal(unknownOpen.structuredContent.recoveryBudget.exhausted, true)
  assert.deepEqual((await scopedCall('browser_open', { url: unknownOpenUrl }, unknownOpenSession, unknownOpenScope, 'unknown-open')).result, unknownOpen)
  assert.equal(count(unknownOpenSession), 1, 'an unknown initial open must replay its receipt without creating another page')
  await scopedCall('browser_tabs', {}, unknownOpenSession, unknownOpenScope)
  assert.equal((await scopedCall('browser_observe', { targetRef: 'unrelated-observable-target' }, unknownOpenSession, unknownOpenScope)).result.isError, false)
  const beforeUnknownUrlRetry = count(unknownOpenSession)
  assertExhausted((await scopedCall('browser_open', {
    url: 'https://unknown-open-fixture.test/first#changed',
  }, unknownOpenSession, unknownOpenScope)).result, 'TIMEOUT')
  assert.equal(count(unknownOpenSession), beforeUnknownUrlRetry, 'changing an operation ID or URL fragment cannot duplicate an uncertain open')
  assert.equal((await scopedCall('browser_open', {
    url: 'https://unknown-open-fixture.test/second',
  }, unknownOpenSession, unknownOpenScope)).result.structuredContent.code, 'TIMEOUT')
  const beforeTransportBlock = count(unknownOpenSession)
  const transportBlocked = (await scopedCall('browser_open', {
    url: 'https://unknown-open-fixture.test/third',
  }, unknownOpenSession, unknownOpenScope)).result
  assertExhausted(transportBlocked, 'TIMEOUT')
  assert.equal(transportBlocked.structuredContent.recoveryBudget.domain, 'transport')
  assert.equal(transportBlocked.structuredContent.recoveryBudget.limit, 2)
  assert.equal(count(unknownOpenSession), beforeTransportBlock, 'changing URLs cannot evade repeated unassociated open failures')
  assert.deepEqual((await scopedCall('browser_open', { url: unknownOpenUrl }, unknownOpenSession, unknownOpenScope, 'unknown-open')).result, unknownOpen)
  assert.equal(count(unknownOpenSession), beforeTransportBlock, 'a receipt remains available even when the transport budget is exhausted')
  assert.equal((await scopedCall('browser_tabs', {}, unknownOpenSession, unknownOpenScope)).result.isError, false)
  for (const attempt of [1, 2]) {
    const read = (await scopedCall('browser_inspect', { targetRef: 'unreadable-target' }, unknownOpenSession, unknownOpenScope)).result
    assert.equal(read.structuredContent.code, 'BROWSER_UNAVAILABLE')
    assert.equal(read.structuredContent.recoveryBudget.domain, 'target')
    assert.equal(read.structuredContent.recoveryBudget.attempts, attempt)
    assert.equal(read.structuredContent.recoveryBudget.limit, 2)
  }
  assertExhausted((await scopedCall('browser_inspect', { targetRef: 'unreadable-target' }, unknownOpenSession, unknownOpenScope)).result, 'BROWSER_UNAVAILABLE')
  assert.equal((await scopedCall('browser_observe', { targetRef: 'unrelated-observable-target' }, unknownOpenSession, unknownOpenScope)).result.isError, false)
  const beforeObservedOpenRetry = count(unknownOpenSession)
  const stillUnknown = (await scopedCall('browser_open', { url: unknownOpenUrl }, unknownOpenSession, unknownOpenScope)).result
  assertExhausted(stillUnknown, 'TIMEOUT')
  assert.equal(stillUnknown.structuredContent.recoveryBudget.domain, 'navigation',
    'a specific uncertain URL must take diagnostic precedence over an exhausted transport budget')
  assert.equal(count(unknownOpenSession), beforeObservedOpenRetry, 'observing another target cannot resolve an unknown open')
  const stillTransportBlocked = (await scopedCall('browser_open', {
    url: 'https://unknown-open-fixture.test/third',
  }, unknownOpenSession, unknownOpenScope)).result
  assertExhausted(stillTransportBlocked, 'TIMEOUT')
  assert.equal(stillTransportBlocked.structuredContent.recoveryBudget.domain, 'transport')
  assert.equal(count(unknownOpenSession), beforeObservedOpenRetry,
    'an unrelated healthy page cannot reset repeated opens with unassociated outcomes')
  assert.equal((await scopedCall('browser_open', { url: unknownOpenUrl }, unknownOpenSession, 'new-unknown-open-turn')).result.structuredContent.code, 'TIMEOUT',
    'a new trusted recovery scope may retry the previously uncertain URL')

  for (const code of ['TIMEOUT', 'BROWSER_UNAVAILABLE', 'PAGE_NOT_READY']) {
    const session = `synthetic-${code.toLowerCase()}-session`
    const scope = 'synthetic-recovery-turn'
    const targetRef = 'recoverable-page'
    failures.set(session, request => {
      throw new DesktopBrowserError(code, 'Synthetic recoverable failure.', 409, {
        targetRef: request.targetRef, operation: request.operation, pageState: 'loading',
        outcome: 'not_started', retryable: true, recovery: 'Observe the page before trying again.',
      })
    })
    for (const [index, name] of ['browser_inspect', 'browser_screenshot'].entries()) {
      const result = (await scopedCall(name, { targetRef }, session, scope)).result
      assert.equal(result.structuredContent.code, code)
      assert.equal(result.structuredContent.recoveryBudget.attempts, index + 1)
      assert.equal(result.structuredContent.recoveryBudget.limit, 2)
      assert.equal(result.structuredContent.retryable, index === 0,
        'the final admitted failure must not advertise another retry after its budget is exhausted')
    }
    await scopedCall('browser_tabs', {}, session, scope)
    const beforeBlocked = count(session)
    assertExhausted((await scopedCall('browser_inspect', { targetRef }, session, scope)).result, code)
    assert.equal(count(session), beforeBlocked, 'listing tabs cannot reset repeated failures')
    assert.equal((await scopedCall('browser_inspect', { targetRef }, session, 'synthetic-new-turn')).result.structuredContent.code, code)
  }

  const progressSession = 'synthetic-progress-session'
  const progressTarget = 'progress-page'
  const failUntilObserved = request => {
    if (['observe', 'open', 'screenshot'].includes(request.operation)) return
    throw new DesktopBrowserError('PAGE_NOT_READY', 'Synthetic page loading.', 409, {
      targetRef: request.targetRef, operation: request.operation, pageState: 'loading',
      outcome: 'not_started', retryable: true,
    })
  }
  failures.set(progressSession, failUntilObserved)
  const progressFailure = () => scopedCall('browser_inspect', { targetRef: progressTarget }, progressSession, 'progress-turn')
  assert.equal((await progressFailure()).result.structuredContent.recoveryBudget.attempts, 1)
  assert.equal((await scopedCall('browser_observe', { targetRef: progressTarget }, progressSession, 'progress-turn')).result.isError, false)
  assert.equal((await progressFailure()).result.structuredContent.recoveryBudget.attempts, 1,
    'a valid observation must clear the corresponding recoverable failure history')
  assert.equal((await scopedCall('browser_navigate', { targetRef: progressTarget, url: 'https://progress-fixture.test/' }, progressSession, 'progress-turn')).result.isError, false)
  assert.equal((await progressFailure()).result.structuredContent.recoveryBudget.attempts, 1,
    'a successful navigation must clear the corresponding recoverable failure history')

  const imageOnlySession = 'synthetic-image-only-session'
  failures.set(imageOnlySession, failUntilObserved)
  assert.equal((await call('browser_inspect', { targetRef: progressTarget }, imageOnlySession)).result.structuredContent.recoveryBudget.attempts, 1)
  assert.equal((await call('browser_screenshot', { targetRef: progressTarget }, imageOnlySession)).result.isError, false)
  assert.equal((await call('browser_inspect', { targetRef: progressTarget }, imageOnlySession)).result.structuredContent.recoveryBudget.attempts, 2,
    'a transport-successful screenshot alone does not establish a valid actionable observation')

  const missingSession = 'synthetic-missing-target-session'
  failures.set(missingSession, request => {
    throw new DesktopBrowserError('TARGET_NOT_FOUND', 'Synthetic page no longer exists.', 404, {
      targetRef: request.targetRef, operation: request.operation, pageState: 'closed',
      outcome: 'not_started', retryable: false,
    })
  })
  assert.equal((await call('browser_inspect', { targetRef: 'missing-page' }, missingSession)).result.structuredContent.code, 'TARGET_NOT_FOUND')
  await call('browser_tabs', {}, missingSession)
  const beforeMissingReplay = count(missingSession)
  assertExhausted((await call('browser_act', { targetRef: 'missing-page', action: 'click', ref: 'element-1' }, missingSession)).result, 'TARGET_NOT_FOUND')
  assert.equal(count(missingSession), beforeMissingReplay)
  assert.equal((await call('browser_inspect', { targetRef: 'different-missing-page' }, missingSession)).result.structuredContent.code, 'TARGET_NOT_FOUND')

  for (const code of ['TARGET_NOT_FOUND', 'PAGE_NOT_READY']) {
    const session = `synthetic-interrupted-${code.toLowerCase()}`
    const args = { targetRef: 'interrupted-action-target', action: 'click', ref: 'submit-1' }
    failures.set(session, () => { throw new DesktopBrowserError(code, 'Synthetic page changed during action.') })
    const interruptedAction = (await call('browser_act', args, session, 'interrupted-action')).result
    assert.equal(interruptedAction.structuredContent.code, code)
    assert.equal(interruptedAction.structuredContent.outcome, 'unknown',
      'a driver error without execution evidence cannot prove a mutation never started')
    assert.equal(interruptedAction.structuredContent.retryable, false)
    assert.deepEqual((await call('browser_act', args, session, 'interrupted-action')).result, interruptedAction)
    assert.equal(count(session), 1, 'a page disappearing during a mutation must retain its uncertain outcome receipt')
  }

  // An in-flight duplicate shares one execution and one eventual result.
  const pendingGate = gateFor('pending-session')
  const pendingParams = toolParams('browser_act', action, 'pending-session', 'pending-operation')
  const pendingFirst = rpc('tools/call', pendingParams)
  await pendingGate.started.promise
  const pendingSecond = rpc('tools/call', pendingParams)
  pendingGate.completed.resolve({ completed: true })
  const [pendingOne, pendingTwo] = await Promise.all([pendingFirst, pendingSecond])
  assert.deepEqual(pendingOne.result, pendingTwo.result)
  assert.equal(count('pending-session'), 1)

  // Expired requests are rejected before admission; admitted timeouts retain
  // their unknown outcome receipt rather than dispatching the mutation again.
  const expired = await invoke(envelope('tools/call', toolParams('browser_act', action, 'expired-session')), {
    'x-opensquilla-deadline-at-ms': String(Date.now() - 1),
  })
  assert.equal(expired.status, 504)
  assert.equal(count('expired-session'), 0)
  const timeoutGate = gateFor('timeout-session')
  const timeoutParams = toolParams('browser_act', action, 'timeout-session', 'timeout-operation')
  const realSetTimeout = globalThis.setTimeout
  let watchdog
  const entered = Promise.race([timeoutGate.started.promise, new Promise((_, reject) => {
    watchdog = realSetTimeout(() => reject(new Error('MCP timeout fixture did not start')), 5000)
  })])
  mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.now() })
  try {
    const timed = invoke(envelope('tools/call', timeoutParams), {
      'x-opensquilla-deadline-at-ms': String(Date.now() + 40),
    })
    await entered
    mock.timers.tick(40)
    const timedResponse = await timed
    assert.equal(timedResponse.status, 200, 'a cooperative MCP adapter must return structured deadline diagnostics')
    const timedResult = (await timedResponse.json()).result
    assert.equal(timedResult.isError, true)
    assert.equal(timedResult.structuredContent.code, 'TIMEOUT')
    assert.equal(timedResult.structuredContent.operationId, 'timeout-operation')
    assert.equal(timedResult.structuredContent.outcome, 'unknown')
    await timeoutGate.aborted.promise
  } finally {
    mock.timers.reset()
    clearTimeout(watchdog)
  }
  const timeoutReplay = (await rpc('tools/call', timeoutParams)).result
  assert.equal(timeoutReplay.isError, true)
  assert.equal(timeoutReplay.structuredContent.outcome, 'unknown')
  assert.equal(timeoutReplay.structuredContent.retryable, false)
  assert.equal(timeoutReplay.structuredContent.operationId, 'timeout-operation')
  assert.equal(count('timeout-session'), 1)
  assert.deepEqual((await rpc('tools/call', timeoutParams)).result, timeoutReplay)
  assert.equal(count('timeout-session'), 1, 'an unknown mutation outcome cannot be replayed even after another receipt read')
  assertExhausted((await call('browser_batch', {
    targetRef: action.targetRef, actions: [{ action: 'click', ref: 'element-1' }],
  }, 'timeout-session')).result, 'TIMEOUT')
  assert.equal(count('timeout-session'), 1, 'a new mutation identity or tool cannot replay an uncertain action')
  gates.delete('timeout-session')
  assert.equal((await call('browser_observe', { targetRef: action.targetRef }, 'timeout-session')).result.isError, false)
  assert.equal((await call('browser_act', action, 'timeout-session')).result.isError, false,
    'a valid fresh observation permits a subsequent deliberate action')
  assert.deepEqual((await rpc('tools/call', timeoutParams)).result, timeoutReplay,
    'progress must not discard the original uncertain mutation receipt')
  assert.equal(count('timeout-session'), 3)

  const uncooperativeGate = gateFor('uncooperative-session')
  uncooperativeGate.ignoreAbort = true
  const uncooperativeParams = toolParams('browser_act', action, 'uncooperative-session', 'uncooperative-operation')
  let uncooperativeReceipt
  mock.timers.enable({ apis: ['Date', 'setTimeout'], now: Date.now() })
  try {
    const timed = invoke(envelope('tools/call', uncooperativeParams), {
      'x-opensquilla-deadline-at-ms': String(Date.now() + 40),
    })
    await uncooperativeGate.started.promise
    mock.timers.tick(40)
    await uncooperativeGate.aborted.promise
    await new Promise(resolve => setImmediate(resolve))
    mock.timers.tick(200)
    const response = await timed
    assert.equal(response.status, 200, 'an adapter ignoring cancellation must still yield a bounded structured result')
    uncooperativeReceipt = (await response.json()).result
    assert.equal(uncooperativeReceipt.isError, true)
    assert.equal(uncooperativeReceipt.structuredContent.code, 'TIMEOUT')
    assert.equal(uncooperativeReceipt.structuredContent.operationId, 'uncooperative-operation')
    assert.equal(uncooperativeReceipt.structuredContent.outcome, 'unknown')
  } finally {
    mock.timers.reset()
  }
  const uncooperativeReplay = (await rpc('tools/call', uncooperativeParams)).result
  assert.deepEqual(uncooperativeReplay, uncooperativeReceipt,
    'a sealed receipt must return without waiting for the cancelled driver')
  assert.equal(count('uncooperative-session'), 1)
  uncooperativeGate.completed.resolve({ completed: true })
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual((await rpc('tools/call', uncooperativeParams)).result, uncooperativeReceipt,
    'a late driver success cannot rewrite an already sealed uncertain outcome')
  assertExhausted((await call('browser_act', action, 'uncooperative-session')).result, 'TIMEOUT')
  assert.equal(count('uncooperative-session'), 1, 'late success cannot silently clear the uncertain mutation budget')
  gates.delete('uncooperative-session')
  assert.equal((await call('browser_observe', { targetRef: action.targetRef }, 'uncooperative-session')).result.isError, false)
  assert.equal((await call('browser_act', action, 'uncooperative-session')).result.isError, false)
  assert.equal(count('uncooperative-session'), 3)

  const disconnectGate = gateFor('disconnect-session')
  const disconnectParams = toolParams('browser_act', action, 'disconnect-session', 'disconnect-operation')
  const disconnected = httpRequest(`${endpoint}/mcp`, { method: 'POST', headers })
  disconnected.on('error', () => {})
  disconnected.end(JSON.stringify(envelope('tools/call', disconnectParams)))
  await disconnectGate.started.promise
  disconnected.destroy()
  await disconnectGate.aborted.promise
  const disconnectReplay = (await rpc('tools/call', disconnectParams)).result
  assert.equal(disconnectReplay.isError, true)
  assert.equal(disconnectReplay.structuredContent.outcome, 'unknown')
  assert.equal(count('disconnect-session'), 1, 'disconnect must not trigger an automatic action retry')

  const legacy = await fetch(endpoint, { method: 'POST', headers, body: JSON.stringify({ sessionKey: 'legacy-session', operation: 'list' }) })
  assert.deepEqual(await legacy.json(), { legacy: true, session: 'legacy-session' })
  assert.equal(legacyCalls, 1, 'MCP dispatch must not use the legacy execution handler')
  const forbiddenLegacyBatch = await fetch(endpoint, {
    method: 'POST', headers,
    body: JSON.stringify({ sessionKey: 'legacy-session', operation: 'batch', targetRef: 'target-legacy',
      actions: [{ action: 'click', ref: 'element-1' }] }),
  })
  assert.equal(forbiddenLegacyBatch.status, 400, 'new browser operations require the authenticated MCP metadata boundary')
  assert.equal(legacyCalls, 1)
  assert.equal(JSON.stringify(audit).includes(token), false)
  assert.equal(JSON.stringify(audit).includes('session-a'), false)
  for (const forbidden of [tlsSession, tlsScope, tlsUrl, 'synthetic-url-secret', 'Synthetic certificate failure.']) {
    assert.equal(JSON.stringify(audit).includes(forbidden), false, 'audit events must omit raw navigation and trusted session data')
  }
  assert.equal(calls.every(request => !Object.hasOwn(request, 'recoveryScope')), true,
    'recovery metadata belongs to the authenticated protocol layer, not browser driver arguments')
} finally {
  await server.close()
}

const legacyOnly = new DesktopBrowserServer(async () => ({ legacy: true }))
try {
  const env = await legacyOnly.start()
  const response = await fetch(`${env[DESKTOP_BROWSER_URL_ENV]}/mcp`, {
    method: 'POST', headers: { Authorization: `Bearer ${env[DESKTOP_BROWSER_TOKEN_ENV]}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(envelope('tools/list')),
  })
  assert.equal(response.status, 404, 'old Desktop configurations must not advertise an unusable MCP endpoint')
} finally {
  await legacyOnly.close()
}
console.log('Desktop browser MCP protocol, diagnostics, recovery limits, media, receipts and cancellation passed.')
