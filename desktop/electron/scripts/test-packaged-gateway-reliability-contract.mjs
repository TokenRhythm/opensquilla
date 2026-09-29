// Pure harness checks: importing the scenario module cannot launch the product.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readdir, rmdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { HISTORY_ANSWER_BYTES, HISTORY_TURNS, ORDINARY_RUNS_ROOT, appendOnlyLogSuffix,
  gatewayFlowFailureEvidence, gatewayShutdownCountFromLog, hasCleanGatewayFlowEvidence, hasNaturalGatewayExits, historyTurn, inside, isSettledDraft, isolatedEnvironment,
  observeFrame, onboardingSaveEvidence, ordinaryMigrationEvidence, ordinaryPerformanceMetrics, parseArguments, runtimeRestartControl,
  safeFailureReason, syntheticConfig, validateOrdinaryProfileMarker } from './test-packaged-gateway-reliability.mjs'

const script = fileURLToPath(new URL('./test-packaged-gateway-reliability.mjs', import.meta.url))
const options = ['--executable', process.execPath, '--workdir', join(tmpdir(), 'synthetic-reliability'), '--output', join(tmpdir(), 'synthetic-report.json')]

test('reject missing/unknown/duplicate arguments before any launch; GPU flag is explicit', () => {
  assert.throws(() => parseArguments([]), /Missing --executable/)
  assert.throws(() => parseArguments([...options, '--scenario', 'unsupported']), /Supported scenarios/)
  assert.throws(() => parseArguments([...options, '--scenario', 'restart', '--scenario', 'restart']), /Duplicate/)
  assert.equal(parseArguments([...options, '--scenario', 'configuration']).disableGpu, false)
  assert.equal(parseArguments([...options, '--scenario', 'late-ready', '--disable-gpu']).disableGpu, true)
  for (const scenario of ['fresh-onboarding', 'history-streaming-restart']) {
    assert.equal(parseArguments([...options, '--scenario', scenario, '--disable-gpu']).scenario, scenario)
  }
  assert.equal(parseArguments([...options, '--scenario', 'restart']).startupTiming, false)
  assert.equal(parseArguments([...options, '--scenario', 'restart', '--startup-timing']).startupTiming, true)
})

test('startup timing is opt-in after inherited environment scrubbing', () => {
  const source = {}
  Object.defineProperty(source, 'OPENSQUILLA_STARTUP_TIMING', { enumerable: true,
    get() { throw new Error('must not inherit the caller setting') } })
  const root = join(tmpdir(), 'synthetic-timing-env')
  assert.equal(isolatedEnvironment(source, root).OPENSQUILLA_STARTUP_TIMING, undefined)
  assert.equal(isolatedEnvironment(source, root, { startupTiming: true }).OPENSQUILLA_STARTUP_TIMING, '1')
  assert.equal(isolatedEnvironment(source, root).OPENSQUILLA_NAMING_ENABLED, 'false')
  assert.equal(isolatedEnvironment(source, root).OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS, '131072')
  assert.equal(isolatedEnvironment(source, root, { longHistory: true }).OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS, '1048576')
  assert.equal(isolatedEnvironment(source, root, { longHistory: true }).OPENSQUILLA_LLM_MAX_TOKENS, '262144')
})

test('repeat profiles are explicit fixed variants with new evidence under the bounded run root', () => {
  const root = join(ORDINARY_RUNS_ROOT, 'contract-no-launch')
  const args = ['--executable', process.execPath, '--workdir', root, '--output', join(root, 'report.json'),
    '--scenario', 'restart', '--repeat-profile', 'main']
  assert.equal(parseArguments(args).repeatProfile, 'main')
  assert.equal(parseArguments(args).initializeRepeatProfile, false)
  assert.equal(parseArguments([...args, '--initialize-repeat-profile']).initializeRepeatProfile, true)
  assert.throws(() => parseArguments([...options, '--scenario', 'restart', '--repeat-profile', 'main']), /isolated ordinary run/)
  assert.throws(() => parseArguments(args.map(value => value === 'main' ? '../../real-profile' : value)), /Unknown repeat profile/)
  assert.throws(() => parseArguments(args.map(value => value === 'restart' ? 'configuration' : value)), /only support restart/)
  assert.throws(() => parseArguments(args.map(value => value === join(root, 'report.json') ? join(ORDINARY_RUNS_ROOT, 'outside.json') : value)), /isolated ordinary run/)
  assert.throws(() => parseArguments([...options, '--scenario', 'restart', '--initialize-repeat-profile']), /requires a repeat profile/)
})

test('repeat markers must prove completed preparation, clean exit and exact config bindings', () => {
  const marker = { kind: 'opensquilla-synthetic-ordinary-performance-v1', clean: true, completedRuns: 2,
    providerPort: 12345, configSha256: 'a'.repeat(64), credentialSha256: 'b'.repeat(64),
    executableSha256: 'c'.repeat(64), executablePath: process.execPath }
  assert.equal(validateOrdinaryProfileMarker(marker), marker)
  for (const update of [{ kind: 'real-profile' }, { clean: false }, { completedRuns: 0 }, { completedRuns: 1.5 },
    { providerPort: 80 }, { providerPort: 65536 }, { configSha256: '' }, { credentialSha256: 'short' },
    { executableSha256: undefined }, { executablePath: 'relative.exe' }]) {
    assert.throws(() => validateOrdinaryProfileMarker({ ...marker, ...update }))
  }
})

test('only current-run log suffix can establish lifecycle, migrations or clean flow', () => {
  const previous = '{"event":"gateway.started"}\n{"event":"gateway.stopped"}\n'
  const current = '{"event":"gateway.started"}\n'
  assert.equal(appendOnlyLogSuffix(previous, previous + current), current)
  assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(appendOnlyLogSuffix(previous, previous + current))), false)
  for (const replaced of ['', current, 'rotation\n' + previous]) assert.throws(() => appendOnlyLogSuffix(previous, replaced), /replaced or truncated/)
  const migrations = count => JSON.stringify({ event: 'build_services.migrations_ready', count }) + '\n'
  assert.equal(ordinaryMigrationEvidence(migrations(0).repeat(2), 2).alreadyMigrated, true)
  for (const log of ['', migrations(0), migrations(1) + migrations(0), migrations(null).repeat(2)]) {
    assert.equal(ordinaryMigrationEvidence(log, 2).alreadyMigrated, false)
  }
})

test('performance metrics require distinct connected, usable, answer, terminal and history boundaries', () => {
  const phases = [
    ['launch', 10], ['initial-ui-connected', 50], ['initial-ui-connected-composer-usable', 60],
    ['runtime-restart-click', 100], ['runtime-restart-connected', 190], ['runtime-restart-connected-composer-usable', 205],
    ['single-ui-send-click', 220], ['single-ui-answer-visible', 270], ['single-ui-send-complete', 285],
    ['history-reread-click', 300], ['history-reread-complete', 330],
  ].map(([phase, ms]) => ({ phase, ms }))
  assert.deepEqual(ordinaryPerformanceMetrics(phases), { launchToConnectedMs: 40, launchToComposerUsableMs: 50,
    restartToConnectedMs: 90, restartToComposerUsableMs: 105, firstSendToAnswerVisibleMs: 50,
    firstSendToCompletedMs: 65, historyClickToReadAndVisibleMs: 30 })
  assert.throws(() => ordinaryPerformanceMetrics(phases.filter(item => item.phase !== 'history-reread-complete')))
  assert.throws(() => ordinaryPerformanceMetrics([...phases, phases[0]]))
  assert.throws(() => ordinaryPerformanceMetrics(phases.map(item => item.phase === 'launch' ? { ...item, ms: 99 } : item)))
})

test('a draft URL alone cannot prove the previous session was left before reopening history', () => {
  const ready = { routeIsDraft: true, routeHasSession: false, draftLanding: true, messageRowsEmpty: true, composerEditable: true, composerVisible: true }
  assert.equal(isSettledDraft(ready), true)
  for (const [key, value] of Object.entries(ready)) assert.equal(isSettledDraft({ ...ready, [key]: !value }), false, key)
  assert.equal(isSettledDraft({ routeIsDraft: true }), false)
  assert.equal(isSettledDraft(undefined), false)
})

test('runtime restart selector accepts the explicit control or only the verified legacy layout', async () => {
  function pageWith(modernCount, legacyCount) {
    const button = { fixed: true }
    const modern = { count: async () => modernCount }
    return { button, modern, page: { locator: selector => selector.startsWith('[data-testid=')
      ? modern : { count: async () => legacyCount, last: () => button } } }
  }
  const modern = pageWith(1, 2)
  assert.deepEqual(await runtimeRestartControl(modern.page), { button: modern.modern, layout: 'explicit-test-id' })
  const old = pageWith(0, 3)
  assert.deepEqual(await runtimeRestartControl(old.page), { button: old.button, layout: 'legacy-three-button' })
  await assert.rejects(runtimeRestartControl(pageWith(0, 2).page), /Known real runtime controls required/)
  await assert.rejects(runtimeRestartControl(pageWith(2, 3).page), /Known real runtime controls required/)
})

test('native save proof requires real successful persistence, not window closure or a started write', () => {
  const lines = [
    { event: 'onboarding_save_started', privateText: 'PRIVATE' },
    { event: 'onboarding_save_finished', outcome: 'threw', writerAdmitted: true, settingsPersistedConfirmed: true },
    { event: 'onboarding_save_finished', outcome: 'ok', writerAdmitted: true, settingsPersistedConfirmed: false },
  ]
  assert.deepEqual(onboardingSaveEvidence(lines.map(row => JSON.stringify(row)).join('\n')), { successfulSaves: 0 })
  lines.push({ event: 'onboarding_save_finished', outcome: 'ok', writerAdmitted: true, settingsPersistedConfirmed: true, privateText: 'PRIVATE' })
  const proof = onboardingSaveEvidence('not json\n' + lines.map(row => JSON.stringify(row)).join('\n'))
  assert.deepEqual(proof, { successfulSaves: 1 })
  assert.equal(JSON.stringify(proof).includes('PRIVATE'), false)
})

test('Gateway log parser counts actual structured events through the frozen logger prefix', () => {
  const lines = [
    '2026-09-29T00:00:00Z [INFO] opensquilla.cli.gateway_cmd: {"event":"gateway.shutdown_requested","reason":"PRIVATE"}',
    '{"event":"gateway.shutdown_requested"}',
    'gateway.shutdown_requested without structured evidence',
    '{"event":"unrelated","body":"gateway.shutdown_requested"}',
  ].join('\n')
  assert.equal(gatewayShutdownCountFromLog(lines), 2)
})

test('Gateway flow evidence preserves only closed reason classifications, never arbitrary error text', () => {
  const input = [
    { event: 'gateway.ws_flow_encode_or_budget_failed', reason_code: 'snapshot_delivery_missing', exception_type: 'FlowDeliveryStaleError', token: 'PRIVATE' },
    { event: 'gateway.ws_flow_encode_or_budget_failed', reason_code: 'PRIVATE', exception_type: 'PRIVATE', exception: 'PRIVATE' },
    { event: 'unrelated', reason_code: 'control_buffer_limit' },
  ].map(row => 'prefix logger: ' + JSON.stringify(row)).join('\n')
  const result = gatewayFlowFailureEvidence(input)
  assert.equal(result.failures, 2)
  assert.deepEqual(result.reasonCodes, { snapshot_delivery_missing: 1, unclassified: 1 })
  assert.deepEqual(result.exceptionTypes, { FlowDeliveryStaleError: 1, 'other-or-unavailable': 1 })
  assert.equal(JSON.stringify(result).includes('PRIVATE'), false)
})

test('clean flow acceptance rejects a recovered error and unavailable evidence', () => {
  const healthy = 'prefix logger: {"event":"gateway.started"}\nprefix logger: {"event":"gateway.stopped"}\n'
  assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(healthy)), true)
  for (const reason of ['snapshot_delivery_missing', 'control_buffer_limit', 'PRIVATE']) {
    const error = 'prefix logger: ' + JSON.stringify({ event: 'gateway.ws_flow_encode_or_budget_failed', reason_code: reason })
    // A later healthy connection cannot erase an earlier encoding/budget fault.
    assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(error + '\n' + healthy)), false)
  }
  for (const unavailable of [undefined, null, {}, { available: false, failures: 0 }, { available: true }]) {
    assert.equal(hasCleanGatewayFlowEvidence(unavailable), false)
  }
  for (const incomplete of ['', 'unparseable gateway.ws_flow_encode_or_budget_failed',
    '{"event":"unrelated"}', '{"event":"gateway.started"}', '{"event":"gateway.stopped"}']) {
    assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(incomplete)), false)
  }
  assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(healthy, 2)), false,
    'a log covering only the replacement cannot prove both Gateway lifecycles')
  assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(healthy.repeat(2), 2)), true)
  assert.equal(hasCleanGatewayFlowEvidence(gatewayFlowFailureEvidence(healthy, 0)), false)
})

test('final clean quit cannot hide an abnormal, missing or duplicate earlier Gateway exit', () => {
  const first = { event: 'gateway_exited', pid: 101, code: 0, signal: null, abnormalExit: false }
  const final = { ...first, pid: 202 }
  const log = events => events.concat([
    { event: 'quit_gateway_exit', exited: true, hardTerminated: false },
    { event: 'desktop_exit_phase', to: 'committed', reason: 'all lifecycle-owned Gateways exited' },
  ]).map(event => JSON.stringify(event)).join('\n')
  assert.equal(hasNaturalGatewayExits(log([first, final]), [101, 202]), true)
  for (const exits of [
    [{ ...first, code: 1, abnormalExit: true }, final],
    [{ ...first, code: null, signal: 'SIGKILL', abnormalExit: true }, final],
    [final], [first, first], [first, final, { ...first, pid: 303 }],
  ]) assert.equal(hasNaturalGatewayExits(log(exits), [101, 202]), false)
  assert.equal(hasNaturalGatewayExits(log([]), []), false)
  assert.equal(hasNaturalGatewayExits(log([first, first]), [101, 101]), false)
})

test('long history fixture is bounded and contains distinct real UI turn markers', () => {
  assert.equal(HISTORY_TURNS, 12)
  const messages = new Set()
  for (let index = 0; index < HISTORY_TURNS; index += 1) {
    const turn = historyTurn(index)
    messages.add(turn.message)
    assert.equal(Buffer.byteLength(turn.answer), HISTORY_ANSWER_BYTES)
    assert.ok(turn.answer.startsWith(`Synthetic retained history answer ${index + 1}.`))
    const completeBlocks = turn.answer.split('\n').slice(1, -1)
    assert.equal(new Set(completeBlocks).size, completeBlocks.length)
    assert.equal(turn.answer, historyTurn(index).answer)
  }
  assert.equal(messages.size, HISTORY_TURNS)
  assert.ok(HISTORY_TURNS * HISTORY_ANSWER_BYTES > 192 * 1024)
  assert.throws(() => historyTurn(-1))
  assert.throws(() => historyTurn(HISTORY_TURNS))
})

test('partial visible output cannot hide a real failed or cancelled turn', () => {
  const summary = { methods: {}, requestedCaps: [] }
  for (const event of ['session.event.error', 'task.failed', 'task.timeout', 'task.cancelled', 'task.abandoned']) {
    observeFrame(summary, 'received', JSON.stringify({ type: 'event', event,
      payload: { error: 'PRIVATE', message: 'PRIVATE' } }), tmpdir())
  }
  assert.equal(summary.conversationFailures, 5)
  assert.equal(JSON.stringify(summary).includes('PRIVATE'), false)
})

test('drop sensitive and override names before reading their values; isolate every writable environment root', () => {
  const source = { PATH: 'synthetic-path' }
  for (const name of ['OPENAI_API_KEY', 'OPENSQUILLA_STATE_DIR', 'UV_CACHE_DIR', 'NODE_OPTIONS', 'HTTP_PROXY']) {
    Object.defineProperty(source, name, { enumerable: true, get() { throw new Error('must not read') } })
  }
  const root = join(tmpdir(), 'synthetic-env')
  const env = isolatedEnvironment(source, root)
  assert.equal(env.PATH, 'synthetic-path')
  assert.equal(env.OPENAI_API_KEY, undefined)
  assert.equal(env.OPENSQUILLA_STATE_DIR, undefined)
  for (const key of ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'OPENSQUILLA_USER_STATE_DIR']) assert.ok(inside(root, env[key]))
  assert.equal(inside(root, `${root}-sibling`), false)
  assert.equal(inside(root, dirname(root)), false)
})

test('synthetic config accepts only credential-free explicit loopback endpoints', () => {
  const profile = join(tmpdir(), 'synthetic', 'profile')
  const config = syntheticConfig(profile, 'http://127.0.0.1:12345', 'http://127.0.0.1:12346/sse')
  assert.ok(config.includes('connect_timeout_seconds = 135'))
  assert.ok(config.includes(JSON.stringify(join(profile, 'workspace'))))
  assert.throws(() => syntheticConfig(profile, 'https://example.com:443'))
  assert.throws(() => syntheticConfig(profile, 'http://secret@127.0.0.1:12345'))
  assert.throws(() => syntheticConfig(profile, 'http://127.0.0.1:12345?token=secret'))
})

test('passive frame evidence never serializes credentials, bodies, URL, unknown policy, or raw epoch', () => {
  const secret = 'SYNTHETIC_SECRET_MUST_NOT_ESCAPE'
  const state = join(tmpdir(), 'synthetic-state')
  const summary = { methods: {}, requestedCaps: [] }
  observeFrame(summary, 'sent', JSON.stringify({ type: 'req', method: 'connect', params: {
    token: secret, caps: ['transport.flow.v1', 'transport.recovery.v1', secret] } }), state)
  observeFrame(summary, 'received', JSON.stringify({ type: 'res', payload: { type: 'hello-ok', protocol: 3,
    auth: secret, snapshot: { state_dir: state, secret }, features: { methods: ['sessions.messages.resume', 'sessions.messages.snapshot.release'] },
    policy: { transport_flow: { delivery_epoch: secret, window_frames: 128, window_bytes: 4194304 }, secret } } }), state)
  observeFrame(summary, 'sent', JSON.stringify({ type: 'req', method: 'chat.send', params: { message: secret, sessionKey: secret } }), state)
  assert.equal(summary.hello.flow, true)
  assert.equal(summary.hello.recoveryMethods, true)
  assert.equal(summary.hello.stateDirMatches, true)
  assert.equal(summary.methods['chat.send'], 1)
  assert.equal(JSON.stringify(summary).includes(secret), false)
  assert.equal(JSON.stringify(summary).includes(state), false)
})

test('configuration success requires a response to the actual observed configure request', () => {
  const summary = { methods: {}, requestedCaps: [] }
  observeFrame(summary, 'received', '{"type":"res","id":"other","ok":true}', tmpdir())
  assert.equal(summary.configurationSucceeded, undefined)
  observeFrame(summary, 'sent', '{"type":"req","id":"save","method":"onboarding.provider.configure","params":{"secret":"not persisted"}}', tmpdir())
  observeFrame(summary, 'received', '{"type":"res","id":"save","ok":false}', tmpdir())
  observeFrame(summary, 'received', '{"type":"res","id":"save","ok":true}', tmpdir())
  assert.equal(summary.configurationFailed, 1)
  assert.equal(summary.configurationSucceeded, undefined)
  assert.equal(JSON.stringify(summary).includes('not persisted'), false)
})

test('failure reasons accept only fixed script text, never arbitrary Playwright error strings', () => {
  assert.equal(safeFailureReason(new Error('Timed out: persisted sidebar row')), 'Timed out: persisted sidebar row')
  assert.equal(safeFailureReason(new Error('Playwright failed at https://example.com?token=SECRET')), 'details-redacted')
  assert.equal(safeFailureReason(new Error('Timed out: persisted sidebar row SECRET')), 'details-redacted')
  assert.equal(safeFailureReason(new Error('No repeated chat.send\nsecret actual/expected values')), 'No repeated chat.send')
})

test('history proof ignores earlier, unrelated, failed and incomplete reads', () => {
  const summary = { methods: {}, requestedCaps: [] }
  const probe = { key: 'synthetic-target', armed: false }
  const request = (id, key) => observeFrame(summary, 'sent', JSON.stringify({ type: 'req', id,
    method: 'sessions.messages.snapshot.read', params: { key } }), tmpdir(), probe)
  const response = (id, ok, segment_index = 0, segment_count = 1) => observeFrame(summary, 'received', JSON.stringify({ type: 'res', id, ok,
    payload: { segment_index, segment_count } }), tmpdir(), probe)
  request('earlier', probe.key)
  probe.armed = true
  response('earlier', true)
  request('other-session', 'unrelated'); response('other-session', true)
  request('failed', probe.key); response('failed', false)
  request('first-segment', probe.key); response('first-segment', true, 0, 2)
  assert.equal(summary.targetReadCompleted, undefined)
  assert.equal(summary.targetReadMaxSegments, 2)
  assert.equal(summary.targetMultiSegmentCompleted, undefined)
  request('last-segment', probe.key); response('last-segment', true, 1, 2)
  assert.equal(summary.targetReadCompleted, 1)
  assert.equal(summary.targetMultiSegmentCompleted, 1)
  assert.equal(JSON.stringify(summary).includes(probe.key), false)
})

test('old probe replies cannot prove a later history/restart phase; target resume needs its own successful response', () => {
  const summary = { methods: {}, requestedCaps: [] }
  const probe = { key: 'target', armed: true, revision: 1 }
  const send = (id, method, key = probe.key) => observeFrame(summary, 'sent', JSON.stringify({ type: 'req', id, method, params: { key } }), tmpdir(), probe)
  const receive = (id, ok, payload = {}) => observeFrame(summary, 'received', JSON.stringify({ type: 'res', id, ok, payload }), tmpdir(), probe)
  send('old-history', 'chat.history')
  send('old-resume', 'sessions.messages.resume')
  probe.revision++
  receive('old-history', true, { messages: ['PRIVATE'] })
  receive('old-resume', true)
  send('wrong-resume', 'sessions.messages.resume', 'other'); receive('wrong-resume', true)
  send('failed-resume', 'sessions.messages.resume'); receive('failed-resume', false)
  assert.equal(summary.targetHistoryCompleted, undefined)
  assert.equal(summary.targetResumeCompleted, undefined)
  send('history', 'chat.history'); receive('history', true, { messages: ['PRIVATE', 'PRIVATE'] })
  send('resume', 'sessions.messages.resume'); receive('resume', true)
  assert.equal(summary.targetHistoryCompleted, 1)
  assert.equal(summary.targetHistoryMaxMessages, 2)
  assert.ok(summary.targetHistoryMaxWireBytes > 0)
  assert.equal(summary.targetResumeCompleted, 1)
  assert.equal(JSON.stringify(summary).includes('PRIVATE'), false)
})

test('unrelated or failed large snapshots cannot prove multi-segment target recovery', () => {
  const summary = { methods: {}, requestedCaps: [] }
  const probe = { key: 'target', armed: true }
  for (const [id, key, ok] of [['other', 'other-key', true], ['failed', 'target', false]]) {
    observeFrame(summary, 'sent', JSON.stringify({ type: 'req', id, method: 'sessions.messages.snapshot.read', params: { key } }), tmpdir(), probe)
    observeFrame(summary, 'received', JSON.stringify({ type: 'res', id, ok, payload: { segment_index: 0, segment_count: 100 } }), tmpdir(), probe)
  }
  assert.equal(summary.targetReadMaxSegments, undefined)
  assert.equal(summary.targetReadCompleted, undefined)
})

test('long-history acceptance checks complete retained content and stable message IDs, not response size', () => {
  const retained = Array.from({ length: HISTORY_TURNS }, (_, index) => [
    { message_id: `PRIVATE-user-${index}`, role: 'user', text: `Synthetic retained history request ${index + 1}.` },
    { message_id: `PRIVATE-assistant-${index}`, role: 'assistant', text: historyTurn(index).answer },
  ]).flat()
  const complete = [...retained,
    { message_id: 'PRIVATE-stream-user', role: 'user', text: 'Synthetic request that crosses Gateway restart.' },
    { message_id: 'PRIVATE-stream-assistant', role: 'assistant', text: 'Synthetic response started before Gateway restart.' },
  ]
  const probe = { key: 'PRIVATE-session', armed: true, revision: 1, historyPhase: 'capture' }
  const summary = { methods: {}, requestedCaps: [] }
  let sequence = 0
  const request = (key = probe.key) => {
    const id = String(++sequence)
    observeFrame(summary, 'sent', JSON.stringify({ type: 'req', id, method: 'chat.history', params: { sessionKey: key } }), tmpdir(), probe)
    return id
  }
  const response = (id, messages, ok = true) => observeFrame(summary, 'received',
    JSON.stringify({ type: 'res', id, ok, payload: { messages } }), tmpdir(), probe)
  response(request(), retained)
  assert.equal(summary.targetSyntheticHistoryCaptured, 1)
  const earlier = request()
  probe.historyPhase = 'verify'; probe.revision++
  response(earlier, complete) // A pre-restart request cannot establish preservation.
  response(request('other-session'), complete)
  response(request(), complete, false)
  assert.equal(summary.targetSyntheticHistoryVerified, undefined)

  const oversized = [{ ...complete.at(-1), text: complete.at(-1).text + 'x'.repeat(HISTORY_TURNS * HISTORY_ANSWER_BYTES) }]
  response(request(), oversized)
  assert.ok(summary.targetHistoryMaxWireBytes >= HISTORY_TURNS * HISTORY_ANSWER_BYTES,
    'the old byte-only assertion would pass this one-message response')
  for (const broken of [
    complete.slice(1),
    complete.map((message, index) => index < HISTORY_TURNS * 2
      ? { ...message, text: message.text.split('\n')[0] } : message),
    complete.map((message, index) => index === 0 ? { ...message, text: 'missing historical marker' } : message),
    complete.map((message, index) => index === 0 ? { ...message, message_id: 'changed-id' } : message),
    complete.map((message, index) => index === 0 ? { ...message, message_id: complete[1].message_id } : message),
    complete.map((message, index) => index === 0 ? { ...message, message_id: '' } : message),
    [...complete, complete[0]],
  ]) response(request(), broken)
  assert.equal(summary.targetSyntheticHistoryVerified, undefined)
  response(request(), complete)
  assert.equal(summary.targetSyntheticHistoryVerified, 1)
  assert.equal(JSON.stringify(summary).includes('PRIVATE'), false)
  assert.equal(JSON.stringify(summary).includes('Synthetic retained history'), false)

  const uncaptured = { key: probe.key, armed: true, revision: 1, historyPhase: 'verify' }
  const noBaseline = { methods: {}, requestedCaps: [] }
  observeFrame(noBaseline, 'sent', JSON.stringify({ type: 'req', id: 'no-baseline', method: 'chat.history', params: { sessionKey: probe.key } }), tmpdir(), uncaptured)
  observeFrame(noBaseline, 'received', JSON.stringify({ type: 'res', id: 'no-baseline', ok: true, payload: { messages: complete } }), tmpdir(), uncaptured)
  assert.equal(noBaseline.targetSyntheticHistoryVerified, undefined)
})

test('no-argument CLI fails without creating evidence or importing a packaged runtime', () => {
  const result = spawnSync(process.execPath, [script], { encoding: 'utf8', timeout: 5_000 })
  assert.equal(result.status, 1)
  assert.match(result.stderr, /Missing --executable/)
})

test('existing workdir is rejected before any packaged launch or file overwrite', { skip: process.platform !== 'win32' }, async () => {
  const root = await mkdtemp(join(tmpdir(), 'osq-reliability-contract-'))
  try {
    const result = spawnSync(process.execPath, [script, '--executable', process.execPath,
      '--workdir', root, '--output', join(root, 'report.json'), '--scenario', 'restart', '--disable-gpu'], { encoding: 'utf8', timeout: 5_000 })
    assert.equal(result.status, 1)
    assert.deepEqual(await readdir(root), [])
  } finally {
    // This test created the unique, empty root; never remove a supplied path.
    await rmdir(root)
  }
})
