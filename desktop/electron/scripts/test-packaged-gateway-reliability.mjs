// Narrow native Windows probes. No installer, real profile, reload, WS proxy,
// IPC replacement, or product timeout override. Run each scenario separately.
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { lstat, mkdir, open, readFile, realpath, stat, unlink, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { performance } from 'node:perf_hooks'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { crashAndReloadRenderer, hardKillAndReloadRenderer } from './packaged-renderer-fault.mjs'

const MODEL = 'opensquilla-gateway-reliability'
const ANSWER = 'Synthetic gateway reliability response.'
const MESSAGE = 'Synthetic gateway reliability request.'
const STREAM_MESSAGE = 'Synthetic request that crosses Gateway restart.'
const STREAM_ANSWER = 'Synthetic response started before Gateway restart.'
const STREAM_CONTENT = syntheticText(STREAM_ANSWER, 256 * 1024)
export const HISTORY_TURNS = 12
export const HISTORY_ANSWER_BYTES = 32 * 1024
function syntheticText(marker, bytes) {
  // Repeated padding trips the real model repetition guard. Distinct,
  // deterministic blocks keep that guard enabled without creating secrets.
  let content = marker + '\n'
  for (let block = 0; content.length < bytes; block += 1) {
    content += createHash('sha256').update(`${marker}:${block}`).digest('hex') + '\n'
  }
  return content.slice(0, bytes)
}
export function historyTurn(index) {
  assert.ok(Number.isInteger(index) && index >= 0 && index < HISTORY_TURNS)
  const marker = `Synthetic retained history answer ${index + 1}.`
  return { message: `Synthetic retained history request ${index + 1}.`,
    answer: syntheticText(marker, HISTORY_ANSWER_BYTES) }
}
// The bundled desktop client must negotiate the same lane-ACK capability that
// the Gateway exposes. Keeping this assertion in the packaged observer catches
// stale dist/app.asar artifacts that would otherwise silently use v1.
const FLOW_CAPS = ['transport.flow.v1', 'transport.recovery.v1', 'transport.session-flow.v2']
const FLOW_V2_METHOD = 'transport.sessionFlow.update.v2'
const READ_V2_METHODS = ['sessions.read.open.v2', 'sessions.read.state.v2',
  'sessions.read.install.v2', 'sessions.read.close.v2', 'sessions.history.page.v2']
const HISTORY_METHODS = ['chat.history', 'sessions.history.page.v2']
const READ_FAILURE_CODES = new Set(['NOT_FOUND', 'SESSION_NOT_FOUND', 'SNAPSHOT_STALE',
  'STORAGE_BUSY', 'TIMEOUT', 'RECOVERY_GAP', 'LEASE_STALE'])
const REPO_ROOT = fileURLToPath(new URL('../../../', import.meta.url))
export const ORDINARY_PROFILE_ROOT = join(REPO_ROOT, '.cache', 'perf-ordinary-profile')
export const ORDINARY_RUNS_ROOT = join(REPO_ROOT, '.cache', 'perf-ordinary-runs')
const ORDINARY_PROFILE_MARKER = 'opensquilla-synthetic-ordinary-performance-v1'
const METHODS = new Set(['connect', 'chat.send', 'chat.history', 'sessions.list',
  'sessions.subscribe', 'sessions.unsubscribe',
  'sessions.messages.subscribe', 'sessions.messages.unsubscribe', ...READ_V2_METHODS,
  'sessions.messages.snapshot.read', 'sessions.messages.snapshot.release', FLOW_V2_METHOD,
  'sessions.messages.resume', 'transport.flow.update', 'transport.probe',
  'onboarding.configure', 'onboarding.provider.configure'])
const SAFE_FAILURE_REASONS = new Set([
  'Ownership/descriptor observation failed', 'Owner identity must be live at first observation',
  'Ownership changed within one process', 'Unknown descriptor state',
  'Ready requires an observed owner', 'Ready listener identity verification',
  'The actual renderer must negotiate flow/recovery with this profile; a missed Hello is not a pass',
  'The real foreground budget must expire', 'Delayed MCP fixture was actually reached exactly once',
  'The pre-readiness child must be identified', 'Late recovery must use the original child',
  'Late recovery must not spawn a replacement', 'Timed-out MCP connection must converge before ready',
  'Known real runtime controls required', 'Exactly one runtime replacement', 'Old Gateway must actually exit',
  'Restart must preserve the original renderer document', 'Each launch needs new control authority',
  'Synthetic primary endpoint must be selected', 'WebUI provider save should hot-apply without a child replacement',
  'Provider configure must preserve the current owner', 'Configuration save must preserve the original document',
  'One real configuration submission', 'Fixture must not make hidden model requests before user submission',
  'One UI submission must produce exactly one provider chat request', 'The edited endpoint must serve the actual user turn',
  'No repeated chat.send', 'Renderer must not raise page errors', 'The submitted session must materialize',
  'Persisted session must appear in the real sidebar', 'Reopening history must not replay the turn',
  'Operation deadline exceeded', 'Timed out: Gateway ready', 'Timed out: foreground readiness timeout',
  'Timed out: replacement owner', 'Timed out: real provider.configure success', 'Timed out: send enabled',
  'Timed out: visible answer', 'Timed out: turn finished', 'Timed out: materialized session route',
  'Timed out: new draft route', 'Timed out: persisted sidebar row', 'Timed out: history re-read and visible answer',
  'No child identity captured; cleanup remains unproven', 'Owned child still present',
  'A child missed by ownership observation leaves cleanup unproven',
  'Every owned Gateway must exit naturally',
  'Exactly one synthetic user message must render', 'Exactly one synthetic answer must render',
  'Gateway child count must reflect the actual save outcome', 'Onboarding save must preserve the original renderer document',
  'Fresh onboarding must be an actual trusted setup window', 'Fresh onboarding must save the synthetic provider',
  'Fresh onboarding must finish a real persisted save', 'No hidden model calls during onboarding',
  'Synthetic stream must remain active until shutdown begins', 'Stream must finish only after shutdown was observed',
  'Long history recovery must use the original session',
  'Synthetic turns must not fail after displaying partial text',
  'Exactly one provider request for each submitted UI turn', 'No prior Gateway shutdown in the streaming fixture',
  'Timed out: native onboarding window', 'Timed out: main renderer document', 'Timed out: native onboarding save',
  'Timed out: onboarding ready after save', 'Timed out: synthetic stream started', 'Timed out: Gateway shutdown request',
  'Timed out: long history recovery', 'Timed out: synthetic history answer', 'Timed out: completed UI turn',
  'Timed out: composer usable', 'Timed out: initial session directory', 'Timed out: settled new draft',
  'Repeated profile must not perform first-run migrations', 'Ordinary run must add exactly one sidebar session',
  'Existing log was replaced or truncated', 'Repeated profile configuration changed', 'Repeated profile credential changed',
])

export function safeFailureReason(error) {
  const firstLine = typeof error?.message === 'string' ? error.message.split(/\r?\n/, 1)[0] : ''
  return SAFE_FAILURE_REASONS.has(firstLine) ? firstLine : 'details-redacted'
}

export function parseArguments(args) {
  const options = {}
  for (let i = 0; i < args.length; i += 1) {
    const key = args[i]
    assert.ok(['--executable', '--workdir', '--output', '--scenario', '--disable-gpu', '--startup-timing',
      '--repeat-profile', '--initialize-repeat-profile'].includes(key), 'Unknown argument')
    assert.ok(!(key in options), 'Duplicate argument')
    if (key === '--disable-gpu' || key === '--startup-timing' || key === '--initialize-repeat-profile') options[key] = true
    else {
      assert.ok(args[i + 1] && !args[i + 1].startsWith('--'), `Missing value for ${key}`)
      options[key] = args[++i]
    }
  }
  for (const key of ['--executable', '--workdir', '--output', '--scenario']) assert.ok(options[key], `Missing ${key}`)
  assert.ok(['restart', 'late-ready', 'configuration', 'fresh-onboarding', 'history-streaming-restart', 'renderer-crash-reload', 'renderer-hard-kill'].includes(options['--scenario']),
    'Supported scenarios: restart, late-ready, configuration, fresh-onboarding, history-streaming-restart, renderer-crash-reload, renderer-hard-kill. Nothing was launched.')
  const result = { executable: resolve(options['--executable']), workdir: resolve(options['--workdir']),
    output: resolve(options['--output']), scenario: options['--scenario'], disableGpu: Boolean(options['--disable-gpu']),
    startupTiming: Boolean(options['--startup-timing']) }
  if (options['--repeat-profile']) {
    assert.equal(result.scenario, 'restart', 'Repeat profiles only support restart')
    assert.ok(['main', 'candidate'].includes(options['--repeat-profile']), 'Unknown repeat profile')
    assert.ok(inside(ORDINARY_RUNS_ROOT, result.workdir) && inside(result.workdir, result.output),
      'Repeat reports require an isolated ordinary run directory')
    result.repeatProfile = options['--repeat-profile']
    result.initializeRepeatProfile = Boolean(options['--initialize-repeat-profile'])
  } else assert.equal(Boolean(options['--initialize-repeat-profile']), false, 'Initialization requires a repeat profile')
  return result
}

export function validateOrdinaryProfileMarker(value) {
  assert.equal(value?.kind, ORDINARY_PROFILE_MARKER, 'Not a synthetic ordinary profile')
  assert.equal(value.clean, true, 'Previous ordinary profile run did not finish cleanly')
  assert.ok(Number.isSafeInteger(value.completedRuns) && value.completedRuns >= 1, 'Missing profile preparation')
  assert.ok(Number.isInteger(value.providerPort) && value.providerPort > 1024 && value.providerPort < 65536, 'Invalid synthetic provider port')
  for (const key of ['configSha256', 'credentialSha256', 'executableSha256']) assert.match(value[key] || '', /^[a-f0-9]{64}$/, 'Missing profile binding')
  assert.ok(typeof value.executablePath === 'string' && isAbsolute(value.executablePath), 'Missing profile executable')
  return value
}

export function appendOnlyLogSuffix(before, after) {
  assert.ok(after.startsWith(before), 'Existing log was replaced or truncated')
  return after.slice(before.length)
}

export function ordinaryMigrationEvidence(text, expectedStarts) {
  const ready = structuredLogRecords(text).filter(item => item.event === 'build_services.migrations_ready')
  return { observedStarts: ready.length, appliedCounts: ready.map(item => item.count),
    alreadyMigrated: expectedStarts > 0 && ready.length === expectedStarts && ready.every(item => item.count === 0) }
}

export function ordinaryPerformanceMetrics(phases) {
  const time = name => {
    const matches = phases.filter(value => value.phase === name)
    assert.equal(matches.length, 1, `Missing or repeated performance boundary: ${name}`)
    return matches[0].ms
  }
  const duration = (from, to) => {
    const ms = time(to) - time(from)
    assert.ok(Number.isFinite(ms) && ms >= 0, 'Invalid performance boundary order')
    return ms
  }
  return {
    launchToConnectedMs: duration('launch', 'initial-ui-connected'),
    launchToComposerUsableMs: duration('launch', 'initial-ui-connected-composer-usable'),
    restartToConnectedMs: duration('runtime-restart-click', 'runtime-restart-connected'),
    restartToComposerUsableMs: duration('runtime-restart-click', 'runtime-restart-connected-composer-usable'),
    firstSendToAnswerVisibleMs: duration('single-ui-send-click', 'single-ui-answer-visible'),
    firstSendToCompletedMs: duration('single-ui-send-click', 'single-ui-send-complete'),
    historyClickToReadAndVisibleMs: duration('history-reread-click', 'history-reread-complete'),
  }
}

export function inside(root, path) {
  const rel = relative(resolve(root), resolve(path))
  return rel !== '' && rel !== '..' && !rel.startsWith(`..${process.platform === 'win32' ? '\\' : '/'}`) && !isAbsolute(rel)
}

export function isolatedEnvironment(source, root, { startupTiming = false, longHistory = false } = {}) {
  const env = {}
  for (const key of Object.keys(source)) {
    // Check names before accessing values, including getter-backed secrets.
    if (/(?:^|_)(?:API_?KEY|ACCESS_?KEY|AUTH_?TOKEN|ACCESS_?TOKEN|SESSION_?TOKEN|BEARER_?TOKEN|TOKEN|PASSWORD|PASSWD|PRIVATE_?KEY|CLIENT_?SECRET|SECRET|CREDENTIALS?|WEBHOOK|KEY)(?:$|_)/i.test(key)
      || /^(?:OPENSQUILLA_|TOKENRHYTHM_|UV_)/i.test(key)
      || /^(?:PYTHONPATH|PYTHONHOME|VIRTUAL_ENV|NODE_OPTIONS|ELECTRON_RUN_AS_NODE)$/i.test(key)
      || /_PROXY$/i.test(key)) continue
    if (source[key] !== undefined) env[key] = source[key]
  }
  return { ...env, HOME: join(root, 'home'), USERPROFILE: join(root, 'home'),
    APPDATA: join(root, 'appdata'), LOCALAPPDATA: join(root, 'localappdata'),
    TEMP: join(root, 'temp'), TMP: join(root, 'temp'),
    OPENSQUILLA_USER_STATE_DIR: join(root, 'user-state'), OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain', OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0', OPENSQUILLA_TELEMETRY_DISABLED: '1',
    OPENSQUILLA_NAMING_ENABLED: 'false', OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: 'true',
    // This synthetic model has no catalog entry. Match syntheticConfig's
    // declared model capacity even before a fresh wizard writes its config.
    OPENSQUILLA_LLM_CONTEXT_WINDOW_TOKENS: longHistory ? '1048576' : '131072',
    ...(longHistory ? { OPENSQUILLA_LLM_MAX_TOKENS: '262144' } : {}),
    ...(startupTiming ? { OPENSQUILLA_STARTUP_TIMING: '1' } : {}),
    OPENSQUILLA_TESTING: '0', GITHUB_ACTIONS: '0',
    HTTP_PROXY: 'http://127.0.0.1:1', HTTPS_PROXY: 'http://127.0.0.1:1', ALL_PROXY: 'http://127.0.0.1:1',
    http_proxy: 'http://127.0.0.1:1', https_proxy: 'http://127.0.0.1:1', all_proxy: 'http://127.0.0.1:1',
    NO_PROXY: '127.0.0.1,localhost,::1', no_proxy: '127.0.0.1,localhost,::1' }
}

// Prefer the explicit post-#1851 control. The legacy fallback matches the
// verified three-button layout only, never a positional guess on another UI.
export async function runtimeRestartControl(page) {
  const modern = page.locator('[data-testid="runtime-restart-gateway"]')
  if (await modern.count() === 1) return { button: modern, layout: 'explicit-test-id' }
  const legacy = page.locator('#settings-gateway-runtime .runtime-actions > button')
  assert.equal(await modern.count(), 0, 'Known real runtime controls required')
  assert.equal(await legacy.count(), 3, 'Known real runtime controls required')
  return { button: legacy.last(), layout: 'legacy-three-button' }
}

// JSON log input is kept local. Reports only receive these fixed booleans.
function structuredLogRecords(text) {
  const records = []
  for (const line of text.split(/\r?\n/)) {
    // Desktop is JSONL; the frozen Gateway prefixes its JSON with timestamp,
    // level and logger. Never return that arbitrary prefix to a report.
    const start = line.indexOf('{')
    if (start < 0) continue
    try { records.push(JSON.parse(line.slice(start))) } catch { /* Non-structured diagnostic line. */ }
  }
  return records
}
export function gatewayShutdownCountFromLog(text) {
  return structuredLogRecords(text).filter(item => item.event === 'gateway.shutdown_requested').length
}
export function gatewayFlowFailureEvidence(text, expectedStarts = 1) {
  const allowed = new Set(['response_wire_limit', 'snapshot_epoch_mismatch', 'snapshot_delivery_id_invalid',
    'snapshot_delivery_missing', 'snapshot_delivery_kind_invalid', 'snapshot_reservation_rejected',
    'frame_wire_limit', 'control_buffer_limit', 'transport_reservation_rejected', 'flow_admission_unclassified'])
  const records = structuredLogRecords(text)
  const result = { available: Number.isSafeInteger(expectedStarts) && expectedStarts > 0
      && records.filter(item => item?.event === 'gateway.started').length === expectedStarts
      && records.filter(item => item?.event === 'gateway.stopped').length === expectedStarts,
    failures: 0, reasonCodes: {}, exceptionTypes: {} }
  for (const item of records) {
    if (item?.event !== 'gateway.ws_flow_encode_or_budget_failed') continue
    result.failures++
    const reason = allowed.has(item.reason_code) ? item.reason_code : 'unclassified'
    const type = ['ValueError', '_FlowAdmissionError', 'FlowDeliveryStaleError'].includes(item.exception_type)
      ? item.exception_type : 'other-or-unavailable'
    result.reasonCodes[reason] = (result.reasonCodes[reason] || 0) + 1
    result.exceptionTypes[type] = (result.exceptionTypes[type] || 0) + 1
  }
  return result
}
export function hasNaturalGatewayExits(text, ownedPids) {
  const exits = structuredLogRecords(text).filter(item => item?.event === 'gateway_exited')
  return ownedPids.length > 0 && new Set(ownedPids).size === ownedPids.length
    && exits.length === ownedPids.length
    && ownedPids.every(pid => exits.filter(item => item.pid === pid && item.code === 0
      && item.signal === null && item.abnormalExit === false).length === 1)
}
export function hasCleanGatewayFlowEvidence(evidence) {
  // None of the supported scenarios injects flow encoding or budget failures.
  // Even a recovered connection must not hide an observed failure in `ok`.
  return evidence?.available === true && evidence.failures === 0
}
export function onboardingSaveEvidence(text) {
  let successful = 0
  for (const item of structuredLogRecords(text)) {
    if (item.event === 'onboarding_save_finished' && item.outcome === 'ok'
      && item.writerAdmitted === true && item.settingsPersistedConfirmed === true) successful += 1
  }
  return { successfulSaves: successful }
}

export function syntheticConfig(profile, providerUrl, sseUrl, { longHistory = false } = {}) {
  for (const value of [providerUrl, sseUrl].filter(Boolean)) {
    const url = new URL(value)
    assert.equal(url.protocol, 'http:'); assert.equal(url.hostname, '127.0.0.1')
    assert.ok(url.port && !url.username && !url.password && !url.search && !url.hash)
  }
  const lateReadyTimeoutSeconds = Number.parseInt(process.env.OPENSQUILLA_LATE_READY_TIMEOUT_SECONDS || '135', 10)
  assert.ok(Number.isInteger(lateReadyTimeoutSeconds) && lateReadyTimeoutSeconds > 0 && lateReadyTimeoutSeconds <= 135)
  return ['config_version = 1', `state_dir = ${JSON.stringify(join(profile, 'state'))}`,
    `workspace_dir = ${JSON.stringify(join(profile, 'workspace'))}`,
    '[llm]', 'provider = "ollama"', `model = ${JSON.stringify(MODEL)}`,
    `base_url = ${JSON.stringify(providerUrl)}`, `context_window_tokens = ${longHistory ? 1048576 : 131072}`,
    ...(longHistory ? ['max_tokens = 262144'] : []),
    '[squilla_router]', 'enabled = false', '[llm_ensemble]', 'enabled = false',
    '[naming]', 'enabled = false', '[privacy]', 'disable_network_observability = true',
    ...(sseUrl ? ['[mcp]', 'enabled = true', `connect_timeout_seconds = ${lateReadyTimeoutSeconds}`,
      '[[mcp.servers]]', 'name = "synthetic-late-ready"', 'transport = "sse"',
      `url = ${JSON.stringify(sseUrl)}`, 'tool_timeout_seconds = 180'] : []), ''].join('\n')
}

export function historyRereadReady(sockets, probe, currentSessionKey, userTexts, assistantTexts) {
  return sockets.reduce((sum, socket) => sum + (socket.targetHistoryCompleted || 0), 0) > probe.historyReadsBefore
    && currentSessionKey === probe.key
    && userTexts.some(text => text.includes(MESSAGE))
    && assistantTexts.some(text => text.includes(ANSWER))
}

function syntheticHistoryIds(messages, includeStreaming) {
  const expected = Array.from({ length: HISTORY_TURNS }, (_, index) => {
    const turn = historyTurn(index)
    return [['user', turn.message], ['assistant', turn.answer]]
  }).flat()
  if (includeStreaming) expected.push(['user', STREAM_MESSAGE], ['assistant', STREAM_ANSWER])
  if (!Array.isArray(messages) || messages.length < expected.length
    || messages.length > (HISTORY_TURNS + 1) * 2) return null
  const ids = new Map()
  for (const [index, [role, content]] of expected.entries()) {
    // Bounded v4 history deliberately carries only a preview for large
    // assistant rows.  Match the stable marker and require the ContentRef to
    // advertise the original byte length; a full-body string on this wire
    // would defeat the memory contract this probe is meant to verify.
    const marker = role === 'assistant' && index < HISTORY_TURNS * 2
      ? `Synthetic retained history answer ${Math.floor(index / 2) + 1}.`
      : content
    const matches = messages.filter(message => message?.role === role
      && typeof message.text === 'string' && message.text.includes(marker)
      && (role !== 'assistant' || !(index < HISTORY_TURNS * 2)
        || message.text.includes(content)
        || (Number.isSafeInteger(message.contentRef?.byteLength)
          && message.contentRef.byteLength >= Buffer.byteLength(content))))
    if (matches.length !== 1) return null
    const id = matches[0].message_id ?? matches[0].id
    if (typeof id !== 'string' || !id.trim() || [...ids.values()].includes(id)) return null
    ids.set(index, id)
  }
  return ids
}

// Only fixed classifications and numeric counters leave this passive observer.
// Never retain request params, URLs, tokens, error messages or response bodies.
export function observeFrame(summary, direction, payload, expectedStateDir, readProbe = {}) {
  let frame
  try { frame = JSON.parse(Buffer.isBuffer(payload) ? payload.toString('utf8') : payload) } catch { return }
  if (direction === 'sent' && frame?.type === 'req') {
    if (METHODS.has(frame.method)) summary.methods[frame.method] = (summary.methods[frame.method] || 0) + 1
    if (frame.method === 'connect') summary.requestedCaps = FLOW_CAPS.filter(cap => Array.isArray(frame.params?.caps) && frame.params.caps.includes(cap))
    if (['onboarding.provider.configure', 'sessions.list', 'sessions.subscribe', 'sessions.unsubscribe',
      'sessions.messages.subscribe', 'sessions.messages.unsubscribe', ...READ_V2_METHODS,
      'sessions.messages.snapshot.read', 'sessions.messages.resume', 'chat.history'].includes(frame.method) && typeof frame.id === 'string') {
      if (!Object.hasOwn(summary, '_requests')) Object.defineProperty(summary, '_requests', { value: new Map() })
      const targetRead = Boolean(readProbe.armed && readProbe.key
        && ['sessions.messages.snapshot.read', ...HISTORY_METHODS].includes(frame.method)
        && (frame.params?.key ?? frame.params?.sessionKey) === readProbe.key)
      const targetResume = Boolean(readProbe.armed && readProbe.key && frame.method === 'sessions.messages.resume'
        && frame.params?.key === readProbe.key)
      if (summary._requests.size < 64) summary._requests.set(frame.id, { method: frame.method, targetRead, targetResume,
        probeRevision: readProbe.revision ?? 0 })
      else summary.responseObservationOverflow = true
      if (targetRead) summary.targetReadRequests = (summary.targetReadRequests || 0) + 1
    }
  }
  if (direction === 'received' && frame?.type === 'res' && summary._requests?.has(frame.id)) {
    const request = summary._requests.get(frame.id)
    summary._requests.delete(frame.id)
    const ok = frame.ok === true
    const currentProbe = Boolean(readProbe.armed && request.probeRevision === (readProbe.revision ?? 0))
    summary.responses ??= {}
    summary.responses[request.method] ??= { ok: 0, failed: 0 }
    summary.responses[request.method][ok ? 'ok' : 'failed'] += 1
    if (!ok) {
      const code = READ_FAILURE_CODES.has(frame.error?.code) ? frame.error.code : 'OTHER'
      summary.responseErrors ??= {}
      summary.responseErrors[request.method] ??= {}
      summary.responseErrors[request.method][code] = (summary.responseErrors[request.method][code] || 0) + 1
    }
    if (request.method === 'onboarding.provider.configure') {
      const key = ok ? 'configurationSucceeded' : 'configurationFailed'
      summary[key] = (summary[key] || 0) + 1
    }
    if (request.method === 'sessions.list' && ok) {
      const rows = frame.payload?.sessions ?? frame.payload?.keys
      summary.lastListRows = Array.isArray(rows) ? rows.length : null
    }
    if (currentProbe && request.targetRead && ok && (HISTORY_METHODS.includes(request.method)
      || (Number.isInteger(frame.payload?.segment_count) && frame.payload.segment_count > 0
        && frame.payload.segment_index === frame.payload.segment_count - 1))) {
      summary.targetReadCompleted = (summary.targetReadCompleted || 0) + 1
      if (request.method === 'sessions.messages.snapshot.read' && frame.payload.segment_count > 1) {
        summary.targetMultiSegmentCompleted = (summary.targetMultiSegmentCompleted || 0) + 1
      }
    }
    if (currentProbe && request.targetRead && ok && HISTORY_METHODS.includes(request.method)) {
      const messages = request.method === 'sessions.history.page.v2'
        ? (Array.isArray(frame.payload?.items)
          ? frame.payload.items.map(item => ({ ...item.message, message_id: item.message_id })) : null)
        : frame.payload?.messages
      summary.targetHistoryCompleted = (summary.targetHistoryCompleted || 0) + 1
      summary.targetHistoryMaxWireBytes = Math.max(summary.targetHistoryMaxWireBytes || 0, Buffer.byteLength(payload))
      summary.targetHistoryMaxMessages = Math.max(summary.targetHistoryMaxMessages || 0,
        Array.isArray(messages) ? messages.length : 0)
      if (readProbe.historyPhase === 'capture' || readProbe.historyPhase === 'verify') {
        const ids = syntheticHistoryIds(messages, readProbe.historyPhase === 'verify')
        if (ids) {
          // IDs stay in the private probe; reports contain only counts and booleans.
          if (readProbe.historyPhase === 'capture' && !readProbe.historyIds) readProbe.historyIds = ids
          if (readProbe.historyIds && [...readProbe.historyIds].every(([index, id]) => ids.get(index) === id)) {
            const key = readProbe.historyPhase === 'capture' ? 'targetSyntheticHistoryCaptured' : 'targetSyntheticHistoryVerified'
            summary[key] = (summary[key] || 0) + 1
          }
        }
      }
    }
    if (currentProbe && request.targetResume && ok) {
      summary.targetResumeCompleted = (summary.targetResumeCompleted || 0) + 1
    }
    if (currentProbe && request.targetRead && ok && request.method === 'sessions.messages.snapshot.read'
      && Number.isSafeInteger(frame.payload?.segment_count) && frame.payload.segment_count > 0) {
      summary.targetReadMaxSegments = Math.max(summary.targetReadMaxSegments || 0, frame.payload.segment_count)
    }
  }
  if (direction === 'received' && frame?.type === 'event'
    && ['sessions.changed', 'transport.flow.dirty'].includes(frame.event)) {
    summary.events ??= {}
    summary.events[frame.event] = (summary.events[frame.event] || 0) + 1
  }
  if (direction === 'received' && frame?.type === 'event'
    && ['session.event.error', 'task.failed', 'task.timeout', 'task.cancelled', 'task.abandoned'].includes(frame.event)) {
    summary.conversationFailures = (summary.conversationFailures || 0) + 1
  }
  const hello = frame?.type === 'hello-ok' ? frame : frame?.payload?.type === 'hello-ok' ? frame.payload : null
  if (direction !== 'received' || !hello) return
  const flow = hello.policy?.transport_flow
  summary.hello = { protocol: Number.isSafeInteger(hello.protocol) ? hello.protocol : null,
    flow: Boolean(flow && typeof flow.delivery_epoch === 'string' && flow.window_frames > 0 && flow.window_bytes > 0),
    recoveryMethods: ['sessions.messages.resume', 'sessions.messages.snapshot.release']
      .every(method => hello.features?.methods?.includes(method)),
    v2Method: hello.features?.methods?.includes(FLOW_V2_METHOD) === true,
    sessionReadV2Methods: READ_V2_METHODS.every(method => hello.features?.methods?.includes(method)),
    stateDirMatches: typeof hello.snapshot?.state_dir === 'string'
      && resolve(hello.snapshot.state_dir).toLowerCase() === resolve(expectedStateDir).toLowerCase(),
    probeNonce: hello.policy?.transport_probe_nonce === true,
    deliveryEpochHash: typeof flow?.delivery_epoch === 'string' && flow.delivery_epoch.length <= 128
      ? createHash('sha256').update(flow.delivery_epoch).digest('hex') : null,
    windowFrames: Number.isSafeInteger(flow?.window_frames) ? flow.window_frames : null,
    windowBytes: Number.isSafeInteger(flow?.window_bytes) ? flow.window_bytes : null }
}

// Executed inside the renderer. Reads only structure and identity comparisons;
// no DOM text, session identifiers, URLs, storage, or Vue internals are returned.
export function inspectSyntheticSidebar(targetKey) {
  const rows = [...document.querySelectorAll('.sidebar-history-row')]
  const attributes = [...document.querySelectorAll('[data-session-key]')]
  const decode = value => { try { return decodeURIComponent(value) } catch { return value } }
  const match = element => Boolean(targetKey && element.getAttribute('data-session-key') === targetKey)
  const list = document.querySelector('.sidebar-history-list')
  const sidebar = document.querySelector('#sidebar-nav')
  const loaded = Number(list?.getAttribute('data-sidebar-loaded-count'))
  const current = new URL(location.href)
  const composer = document.querySelector('.chat-textarea')
  return {
    routeIsDraft: current.pathname === '/chat/new', routeIsChat: current.pathname === '/chat',
    routeHasSession: current.searchParams.has('session'),
    routeMatchesTarget: Boolean(targetKey && current.searchParams.get('session') === targetKey),
    draftLanding: Boolean(document.querySelector('.chat.chat--new-landing')),
    messageRowsEmpty: document.querySelectorAll('.msg-user-bubble, .msg-ai').length === 0,
    composerEditable: Boolean(composer && !composer.disabled && !composer.readOnly),
    composerVisible: Boolean(composer && composer.getClientRects().length > 0 && getComputedStyle(composer).visibility !== 'hidden'),
    sidebarHidden: !sidebar || sidebar.getAttribute('aria-hidden') === 'true' || sidebar.hasAttribute('inert'),
    rows: rows.length, sessionAttributes: attributes.length,
    historyButtons: document.querySelectorAll('.sidebar-history-item').length,
    targetRows: rows.filter(match).length, targetAttributes: attributes.filter(match).length,
    decodedTargetMatches: attributes.filter(element => targetKey
      && decode(element.getAttribute('data-session-key') || '') === decode(targetKey)).length,
    targetButtons: rows.filter(match).reduce((sum, element) => sum + element.querySelectorAll('.sidebar-history-item').length, 0),
    virtualized: list?.getAttribute('data-sidebar-virtualized') === 'true',
    loadedCount: list && Number.isFinite(loaded) ? loaded : null,
    collapsedGroups: document.querySelectorAll('.sidebar-group__header[aria-expanded="false"]').length,
    collapsedProjects: document.querySelectorAll('.sidebar-project-disclosure[aria-expanded="false"]').length,
    filterActive: Boolean(document.querySelector('.sidebar-agent-chip')),
    emptyStates: document.querySelectorAll('.sidebar-history-empty, .sidebar-zone-empty').length,
    retryPresent: Boolean(document.querySelector('.sidebar-history-retry')),
  }
}

export function isSettledDraft(value) {
  return value?.routeIsDraft === true && value.routeHasSession === false && value.draftLanding === true && value.messageRowsEmpty === true
    && value.composerEditable === true && value.composerVisible === true
}

async function fileHash(path) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(path)) hash.update(chunk)
  return hash.digest('hex')
}

async function readOptional(path) {
  try { return await readFile(path, 'utf8') } catch (error) {
    if (error.code === 'ENOENT') return ''
    throw error
  }
}

async function rejectLinkedPath(path) {
  assert.ok(inside(REPO_ROOT, path), 'Ordinary profile path must stay in this checkout')
  let current = REPO_ROOT
  for (const part of relative(REPO_ROOT, path).split(/[\\/]/)) {
    current = join(current, part)
    try { assert.equal((await lstat(current)).isSymbolicLink(), false, 'Ordinary profile paths must not be links') }
    catch (error) { if (error.code !== 'ENOENT') throw error }
  }
}

async function run(options) {
  assert.equal(process.platform, 'win32', 'Native Windows only')
  assert.equal(options.disableGpu, true, 'Native probes require --disable-gpu')
  assert.ok((await stat(options.executable)).isFile(), 'Missing packaged executable')
  if (options.repeatProfile) await rejectLinkedPath(options.workdir)
  await mkdir(dirname(options.workdir), { recursive: true })
  await mkdir(options.workdir) // Evidence is always new, including repeat-profile runs.
  const root = await realpath(options.workdir)
  const profileRoot = options.repeatProfile ? join(ORDINARY_PROFILE_ROOT, options.repeatProfile) : root
  let repeatMarker, repeatLock
  const markerPath = join(profileRoot, 'ordinary-performance.json')
  const lockPath = join(profileRoot, '.ordinary-performance.lock')
  if (options.repeatProfile) {
    await rejectLinkedPath(profileRoot)
    if (options.initializeRepeatProfile) {
      await mkdir(ORDINARY_PROFILE_ROOT, { recursive: true })
      await mkdir(profileRoot) // Initialization never adopts an existing directory.
    }
    repeatLock = await open(lockPath, 'wx')
    await repeatLock.writeFile(JSON.stringify({ pid: process.pid, run: root }) + '\n')
    // A failed or interrupted run keeps its lock and marker for inspection.
    if (!options.initializeRepeatProfile) {
      await rejectLinkedPath(markerPath)
      repeatMarker = validateOrdinaryProfileMarker(JSON.parse(await readFile(markerPath, 'utf8')))
      assert.equal(repeatMarker.executablePath, options.executable, 'Repeat profile belongs to another executable')
    }
  }
  const userData = join(profileRoot, 'user-data')
  const profile = join(userData, 'opensquilla')
  const longHistory = options.scenario === 'history-streaming-restart'
  const env = isolatedEnvironment(process.env, profileRoot, { ...options, longHistory })
  for (const path of [userData, profile, env.HOME, env.APPDATA, env.LOCALAPPDATA, env.TEMP, env.OPENSQUILLA_USER_STATE_DIR]) {
    assert.ok(inside(profileRoot, path))
    if (options.repeatProfile) await rejectLinkedPath(path)
    await mkdir(path, { recursive: true })
  }
  const configPath = join(profile, 'config.toml')
  const credentialPath = join(userData, 'desktop-credential.json')
  const desktopLogPath = join(userData, 'logs', 'desktop.log')
  const gatewayLogPath = join(userData, 'logs', 'gateway.log')
  if (options.repeatProfile) {
    for (const path of [configPath, credentialPath, desktopLogPath, gatewayLogPath,
      join(profile, 'state', 'sessions.db'), join(profile, 'workspace'), join(userData, 'gateway-ownership')]) await rejectLinkedPath(path)
  }
  if (repeatMarker) {
    assert.equal(await fileHash(configPath), repeatMarker.configSha256, 'Repeated profile configuration changed')
    assert.equal(await fileHash(credentialPath), repeatMarker.credentialSha256, 'Repeated profile credential changed')
  }
  const desktopLogBefore = await readOptional(desktopLogPath)
  const gatewayLogBefore = await readOptional(gatewayLogPath)
  const report = { schemaVersion: 1, scenario: options.scenario, ok: false, startedAt: new Date().toISOString(),
    disableGpu: options.disableGpu, startupTiming: options.startupTiming, executableSha256: await fileHash(options.executable),
    harnessSha256: await fileHash(fileURLToPath(import.meta.url)),
    provenanceBoundary: 'Outer EXE hash only. Require the separate Electron/WebUI/Gateway build manifest.',
    fixture: { synthetic: true, modelContextWindowTokens: longHistory ? 1048576 : 131072,
      ...(longHistory ? { modelMaxOutputTokens: 262144 } : {}), namingDisabled: true, mockKeychain: true, autoUpdateDisabled: true },
    phases: [], owners: [], sockets: [], descriptorTransitions: [], consoleClasses: {}, cleanup: { verified: false } }
  if (options.repeatProfile) {
    report.repeatProfile = { variant: options.repeatProfile, preparation: !repeatMarker,
      completedRunsBefore: repeatMarker?.completedRuns ?? 0,
      boundary: 'One synthetic session added per run; version-specific prepared profiles grow equally, not identical database snapshots.' }
    if (repeatMarker) assert.equal(report.executableSha256, repeatMarker.executableSha256, 'Repeated profile executable changed')
  }
  await mkdir(dirname(options.output), { recursive: true })
  await writeFile(options.output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' })
  const started = performance.now()
  let phase = 'setup'
  const mark = name => {
    phase = name
    const value = { phase, ms: Math.round(performance.now() - started) }
    report.phases.push(value)
    console.log(JSON.stringify({ event: 'gateway_reliability_phase', ...value }))
  }
  const persist = () => writeFile(options.output, JSON.stringify(report, null, 2) + '\n')
  const [{ _electron: electron }, smoke, cleanup, shutdown, ownership] = await Promise.all([
    import('playwright'), import('./packaged-smoke-helpers.mjs'), import('./packaged-first-send-cleanup.mjs'),
    import('./e2e-shutdown-helpers.mjs'), import('../dist/desktop-gateway-ownership.js')])
  report.harnessOwnershipHelperSha256 = await fileHash(fileURLToPath(new URL('../dist/desktop-gateway-ownership.js', import.meta.url)))
  const fingerprint = ownership.desktopProfileFingerprint(profile)
  const ownershipDir = join(userData, 'gateway-ownership', fingerprint)
  if (options.repeatProfile) {
    await rejectLinkedPath(ownershipDir)
    assert.equal(ownership.loadDesktopGatewayOwnershipRecord(ownershipDir).status, 'missing', 'Previous profile owner remains')
  }
  const records = new Map()
  let app, processIdentity, page, provider, alternateProvider, sse, monitor
  let stopped = false, monitorError = false, failure = false
  let checkpoint = ''
  let chats = 0, alternateChats = 0, sseRequests = 0, sseClosed = 0
  const expectedChats = options.scenario === 'history-streaming-restart' ? HISTORY_TURNS + 2 : 1
  let streamResponse = null, streamEnded = false
  const pages = new Set()
  let pageErrors = 0
  const readProbe = { key: null, armed: false, revision: 0 } // Never serialized.
  const bounded = async (operation, timeoutMs) => {
    let timer
    try {
      return await Promise.race([operation, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Operation deadline exceeded')), timeoutMs)
      })])
    } finally { clearTimeout(timer) }
  }
  const until = async (check, label, timeoutMs) => {
    const deadline = performance.now() + timeoutMs
    while (performance.now() < deadline) {
      assert.equal(monitorError, false, 'Ownership/descriptor observation failed')
      if (await check()) return
      await delay(100)
    }
    throw new Error(`Timed out: ${label}`)
  }
  const currentOwner = () => {
    const result = ownership.loadDesktopGatewayOwnershipRecord(ownershipDir)
    return result.status === 'valid' ? result.record : null
  }
  const ownerKey = record => `${record.pid}:${record.start_identity}`
  const observeOwner = () => {
    const record = currentOwner()
    if (!record) return
    assert.equal(record.profile_fingerprint, fingerprint)
    const key = ownerKey(record)
    if (records.has(key)) {
      assert.ok(ownership.sameDesktopGatewayOwnershipInstance(records.get(key), record), 'Ownership changed within one process')
      return
    }
    assert.equal(ownership.desktopProcessStartIdentity(record.pid), record.start_identity, 'Owner identity must be live at first observation')
    records.set(key, record) // nonce stays in memory, never in the report.
    report.owners.push({ pid: record.pid, startIdentity: record.start_identity,
      port: record.port, version: record.version, firstSeenMs: Math.round(performance.now() - started) })
  }
  function attachPage(candidate) {
    if (pages.has(candidate)) return
    pages.add(candidate)
    candidate.on('pageerror', () => { pageErrors += 1 })
    candidate.on('console', message => {
      const text = message.text()
      const kind = text.startsWith('[useSessions] session directory error:') ? 'session-directory-load-failed'
        : text.startsWith('[SessionDirectoryChanges] Session directory subscription failed') ? 'directory-subscription-failed'
          : text.startsWith('[SessionDirectoryChanges] Dropped malformed sessions.changed event') ? 'directory-event-invalid' : null
      if (kind) report.consoleClasses[kind] = (report.consoleClasses[kind] || 0) + 1
    })
    candidate.on('websocket', ws => {
      if (report.sockets.length >= 32) { monitorError = true; return }
      const summary = { index: report.sockets.length, methods: {}, requestedCaps: [], closed: false }
      report.sockets.push(summary)
      ws.on('framesent', frame => observeFrame(summary, 'sent', frame.payload, join(profile, 'state'), readProbe))
      ws.on('framereceived', frame => observeFrame(summary, 'received', frame.payload, join(profile, 'state'), readProbe))
      ws.on('close', () => { summary.closed = true })
    })
  }
  const descriptor = async () => {
    if (!page || page.isClosed()) return null
    return bounded(page.evaluate(async () => {
      const value = await window.opensquillaDesktop?.getGatewayConnection?.()
      return value ? { status: value.status, revision: value.revision, instanceId: value.instanceId,
        readinessTimeout: typeof value.error === 'string' && value.error.startsWith('Gateway did not become ready at ') } : null
    }).catch(() => null), 3_000)
  }
  async function startServer(handler, label, port = 0) {
    const server = createServer(handler)
    const sockets = shutdown.trackHttpServerConnections(server)
    await new Promise((yes, no) => { server.once('error', no); server.listen(port, '127.0.0.1', yes) })
    return { url: `http://127.0.0.1:${server.address().port}`,
      close: () => shutdown.closeHttpServerWithDeadline(server, sockets, { label, timeoutMs: 5_000 }) }
  }
  async function readinessAtPort(port, path) {
    const response = await fetch(`http://127.0.0.1:${port}${path}`, {
      signal: AbortSignal.timeout(5_000),
    })
    const payload = await response.json().catch(() => null)
    return { status: response.status, payload }
  }
  async function ready(connectedPhase, replacement) {
    await until(async () => {
      const value = await descriptor()
      return value?.status === 'ready' && (!replacement || (value.instanceId && value.instanceId !== replacement.instanceId
        && report.sockets.slice(replacement.socketCount).some(socket => !socket.closed && socket.hello?.stateDirMatches)))
    }, 'Gateway ready', 210_000)
    await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 45_000 })
    if (connectedPhase) mark(connectedPhase)
    const composer = page.locator('.chat-textarea')
    await composer.waitFor({ state: 'visible', timeout: 45_000 })
    await until(async () => await composer.isEnabled() && await composer.isEditable(), 'composer usable', 45_000)
    if (connectedPhase) mark(`${connectedPhase}-composer-usable`)
    observeOwner()
    const record = currentOwner()
    assert.ok(record && records.has(ownerKey(record)), 'Ready requires an observed owner')
    assert.ok(await ownership.verifyDesktopGatewayOwnership(record), 'Ready listener identity verification')
    assert.ok(report.sockets.some(socket => !socket.closed && socket.hello?.flow && socket.hello.recoveryMethods
      && socket.hello.v2Method
      && socket.hello.stateDirMatches && FLOW_CAPS.every(cap => socket.requestedCaps.includes(cap))),
    'The actual renderer must negotiate flow/recovery with this profile; a missed Hello is not a pass')
    return record
  }
  async function verifySingleRenderedTurn() {
    assert.equal((await page.locator('.msg-user-bubble').allTextContents()).filter(text => text.includes(MESSAGE)).length, 1,
      'Exactly one synthetic user message must render')
    assert.equal((await page.locator('.msg-ai-text').allTextContents()).filter(text => text.includes(ANSWER)).length, 1,
      'Exactly one synthetic answer must render')
  }
  const desktopLogText = async () => appendOnlyLogSuffix(desktopLogBefore, await readFile(desktopLogPath, 'utf8'))
  const gatewayLogText = async () => appendOnlyLogSuffix(gatewayLogBefore, await readFile(gatewayLogPath, 'utf8'))
  async function gatewayShutdownCount() {
    const text = await gatewayLogText()
    return gatewayShutdownCountFromLog(text)
  }
  let restartConnection
  async function beginRuntimeRestart() {
    await page.locator('.sidebar-foot button[data-icon="settings"]').click()
    await page.locator('#settings-rail-gateway').click()
    const control = await runtimeRestartControl(page)
    report.runtimeControlLayout = control.layout
    if (options.repeatProfile) {
      restartConnection = { instanceId: (await descriptor())?.instanceId, socketCount: report.sockets.length }
      assert.ok(restartConnection.instanceId, 'Restart must begin from an identified connection')
    }
    mark('runtime-restart-click')
    await control.button.click()
  }
  async function finishRuntimeRestart(before, sentinel) {
    if (!options.repeatProfile) {
      await until(() => { observeOwner(); const next = currentOwner(); return next && !ownership.sameDesktopGatewayOwnershipInstance(before, next) }, 'replacement owner', 180_000)
    }
    await page.locator('.settings-modal__close').click()
    const after = await ready('runtime-restart-connected', restartConnection)
    assert.ok(!ownership.sameDesktopGatewayOwnershipInstance(before, after), 'Exactly one runtime replacement')
    assert.equal(records.size, 2, 'Exactly one runtime replacement')
    assert.equal(shutdown.gatewayProcessSnapshot(before).alive, false, 'Old Gateway must actually exit')
    assert.equal(await bounded(page.evaluate(() => window.__gatewayReliabilityDocument), 5_000), sentinel, 'Restart must preserve the original renderer document')
    assert.notEqual(before.instance_nonce, after.instance_nonce, 'Each launch needs new control authority')
    report.originalDocumentPreserved = true
    return after
  }
  async function submitUiTurn(message) {
    await page.locator('.chat-textarea').fill(message)
    const send = page.locator('.chat-send-btn.btn--primary')
    await until(async () => await send.count() === 1 && !await send.isDisabled(), 'send enabled', 45_000)
    await send.click()
  }
  async function waitCompletedUiTurn() {
    // A stream stop button must disappear; finding an old answer is not proof
    // that the newest turn reached terminal state.
    await until(async () => await page.locator('.chat-stop-btn').count() === 0
      && await page.locator('.chat-send-btn.btn--primary').count() === 1, 'completed UI turn', 45_000)
    await page.locator('.msg-ai').last().locator('.msg-meta__more-btn').waitFor({ state: 'visible', timeout: 45_000 })
  }
  async function enterSettledDraft() {
    await page.locator('.sidebar-new-session').click()
    await until(async () => isSettledDraft(await bounded(page.evaluate(inspectSyntheticSidebar, readProbe.key), 5_000)),
      'settled new draft', 15_000)
  }
  async function persistedHistoryRow(key) {
    // CSS.escape keeps the identity literal; the locator resolves it again at click time.
    const escapedKey = await bounded(page.evaluate(value => CSS.escape(value), key), 5_000)
    const row = page.locator(`.sidebar-history-row[data-session-key=${escapedKey}]`)
    await until(async () => await row.count() === 1, 'persisted sidebar row', 30_000)
    return row
  }
  try {
    const providerHandler = alternate => (request, response) => {
      const path = new URL(request.url || '/', 'http://127.0.0.1').pathname
      if (request.method === 'GET' && ['/api/tags', '/api/version'].includes(path)) {
        response.setHeader('content-type', 'application/json')
        response.end(JSON.stringify(path === '/api/tags' ? { models: [{ name: MODEL, model: MODEL, size: 1,
          digest: 'synthetic', modified_at: '2026-01-01T00:00:00Z', details: {} }] } : { version: '0.0.0-synthetic' }))
      } else if (request.method === 'POST' && path === '/api/chat') {
        chats += 1; if (alternate) alternateChats += 1; request.resume()
        const ordinal = chats
        request.once('end', () => {
          response.setHeader('content-type', 'application/x-ndjson')
          const history = options.scenario === 'history-streaming-restart'
          const content = history && ordinal <= HISTORY_TURNS ? historyTurn(ordinal - 1).answer
            : history && ordinal === HISTORY_TURNS + 1 ? STREAM_CONTENT : ANSWER
          response.write(JSON.stringify({ model: MODEL, created_at: '2026-01-01T00:00:00Z',
            message: { role: 'assistant', content }, done: false }) + '\n')
          const finish = () => response.end(JSON.stringify({ model: MODEL, created_at: '2026-01-01T00:00:00Z',
            message: { role: 'assistant', content: '' }, done: true, done_reason: 'stop',
            prompt_eval_count: 8, eval_count: 3 }) + '\n')
          if (history && ordinal === HISTORY_TURNS + 1) {
            streamResponse = { response, finish: () => { streamEnded = true; finish() } }
          } else finish()
        })
      } else { request.resume(); response.writeHead(404); response.end() }
    }
    provider = await startServer(providerHandler(false), 'synthetic provider', repeatMarker?.providerPort ?? 0)
    if (options.scenario === 'configuration') alternateProvider = await startServer(providerHandler(true), 'alternate synthetic provider')
    if (options.scenario === 'late-ready') {
      sse = await startServer((request, response) => {
        if (request.method !== 'GET' || request.url !== '/sse') { response.writeHead(404); response.end(); return }
        sseRequests += 1
        response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' })
        response.write(': synthetic handshake intentionally pending\n\n')
        response.on('close', () => { sseClosed += 1 })
        // No endpoint event: product discovery's own 135s budget must expire.
      }, 'synthetic delayed MCP')
    }
    if (options.scenario === 'fresh-onboarding') {
      report.fixture.freshUnconfiguredProfile = true
      report.fixture.onboardingEndpointInjection = 'Existing hidden baseUrl field only; provider/model/save use real UI; no IPC replacement.'
    } else if (!repeatMarker) {
      await writeFile(configPath, syntheticConfig(profile, provider.url, sse && `${sse.url}/sse`, { longHistory }), { flag: 'wx' })
      await smoke.writeSyntheticCredential(userData, { baseUrl: provider.url, model: MODEL, disableNetworkObservability: true })
    }
    if (options.repeatProfile) {
      repeatMarker = { kind: ORDINARY_PROFILE_MARKER, clean: false, completedRuns: repeatMarker?.completedRuns ?? 0,
        providerPort: Number(new URL(provider.url).port), executablePath: options.executable, executableSha256: report.executableSha256,
        configSha256: await fileHash(configPath), credentialSha256: await fileHash(credentialPath) }
      await writeFile(markerPath, JSON.stringify(repeatMarker, null, 2) + '\n', { flag: options.initializeRepeatProfile ? 'wx' : 'w' })
    }
    monitor = (async () => {
      while (!stopped) {
        try {
          observeOwner()
          const value = await descriptor()
          if (value && (report.descriptorTransitions.at(-1)?.status !== value.status
            || report.descriptorTransitions.at(-1)?.instanceId !== value.instanceId)) {
            assert.ok(['starting', 'ready', 'error', 'stopped'].includes(value.status), 'Unknown descriptor state')
            report.descriptorTransitions.push({ ...value, ms: Math.round(performance.now() - started),
              ownerCount: records.size })
          }
        } catch (error) { monitorError = true; report.observationFailureReason = safeFailureReason(error) }
        await delay(250)
      }
    })()
    mark('launch')
    app = await electron.launch({ executablePath: options.executable,
      args: ['--use-mock-keychain', `--user-data-dir=${userData}`, ...(options.disableGpu ? ['--disable-gpu'] : [])],
      env, timeout: 150_000 })
    app.context().on('page', attachPage)
    for (const existing of app.context().pages()) attachPage(existing)
    processIdentity = await cleanup.captureElectronProcessIdentity(app)
    const actual = await bounded(app.evaluate(({ app }) => ({ version: app.getVersion(), userData: app.getPath('userData') })), 5_000)
    assert.equal(resolve(actual.userData).toLowerCase(), userData.toLowerCase())
    report.version = actual.version
    if (options.scenario === 'fresh-onboarding') {
      await until(async () => {
        page = app.windows().find(candidate => candidate.url().startsWith('opensquilla-app://desktop/'))
        return Boolean(page)
      }, 'main renderer document', 165_000)
    } else page = await app.firstWindow({ timeout: 30_000 })
    attachPage(page)
    page.setDefaultTimeout(30_000)
    await page.setViewportSize({ width: 1440, height: 900 })
    if (options.scenario === 'fresh-onboarding') {
      let wizard
      await until(async () => {
        for (const candidate of app.windows()) {
          if (!candidate.isClosed() && await candidate.locator('#setup-form').count() === 1) { wizard = candidate; return true }
        }
        return false
      }, 'native onboarding window', 165_000)
      assert.ok(wizard !== page && wizard.url().startsWith('data:text/html'), 'Fresh onboarding must be an actual trusted setup window')
      report.onboardingEntry = { trustedDataDocument: true, separateWindow: true, automaticFreshProfile: true }
      // A fresh unconfigured profile intentionally has no Gateway owner yet.
      // Capture that pre-save state without waiting for aggregate readiness;
      // the real onboarding save is what creates the first configured owner.
      const before = currentOwner()
      const sentinel = randomUUID()
      await bounded(page.evaluate(value => { window.__gatewayReliabilityDocument = value }, sentinel), 5_000)
      await wizard.locator('#providerSelectToggle').click()
      await wizard.locator('[data-provider-option="ollama"]').click()
      // The native wizard hides the endpoint. Inject only fixture routing into
      // its existing input; the real finish event creates the IPC payload.
      await wizard.locator('#baseUrl').evaluate((input, value) => { input.value = value }, provider.url)
      if (!await wizard.locator('#model').isVisible()) await wizard.locator('#modelEditToggle').click()
      await wizard.locator('#model').fill(MODEL)
      assert.equal(chats, 0, 'No hidden model calls during onboarding')
      mark('native-onboarding-save')
      await wizard.locator('#finish').click()
      await until(() => wizard.isClosed(), 'native onboarding save', 120_000)
      const saved = JSON.parse(await readFile(join(userData, 'desktop-credential.json'), 'utf8'))
      assert.ok(saved.provider === 'ollama' && saved.model === MODEL && saved.baseUrl === provider.url,
        'Fresh onboarding must save the synthetic provider')
      assert.equal(onboardingSaveEvidence(await desktopLogText()).successfulSaves, 1,
        'Fresh onboarding must finish a real persisted save')
      const after = await ready()
      const replaced = Boolean(before && !ownership.sameDesktopGatewayOwnershipInstance(before, after))
      assert.equal(records.size, 1, 'Fresh onboarding must create exactly one configured Gateway child')
      if (replaced) assert.equal(shutdown.gatewayProcessSnapshot(before).alive, false, 'Old Gateway must actually exit')
      assert.equal(await bounded(page.evaluate(() => window.__gatewayReliabilityDocument), 5_000), sentinel,
        'Onboarding save must preserve the original renderer document')
      report.onboardingSave = { settingsPersisted: true, successfulSaves: 1, childStarted: !before,
        childReplaced: replaced,
        ownerCount: records.size, originalDocumentPreserved: true }
      report.originalDocumentPreserved = true
      mark('native-onboarding-recovered')
    } else if (options.scenario === 'late-ready') {
      // S1 deliberately separates the durable core from optional MCP startup:
      // Electron must become usable from /readyz/core while the historical
      // aggregate /readyz remains pending until the 135s MCP budget settles.
      const coreProbeStarted = performance.now()
      const coreOwner = await ready('core-ready-before-legacy')
      const core = await readinessAtPort(coreOwner.port, '/readyz/core')
      const legacyPending = await readinessAtPort(coreOwner.port, '/readyz')
      const coreProbeElapsedMs = Math.round(performance.now() - coreProbeStarted)
      const optionalServices = core.payload?.services && typeof core.payload.services === 'object'
        ? core.payload.services : {}
      const optionalStates = Object.fromEntries(Object.entries(optionalServices)
        .map(([name, descriptor]) => [name, descriptor && typeof descriptor === 'object'
          ? descriptor.status : null]))
      report.readinessProbe = {
        coreStatus: core.status,
        coreReady: core.payload?.ready === true,
        coreProbeElapsedMs,
        coreServices: optionalServices,
        optionalStates,
        legacyPendingStatus: legacyPending.status,
        legacyPending: legacyPending.payload?.ready === false,
      }
      assert.equal(core.status, 200, 'Core readiness must be published before MCP settles')
      assert.equal(core.payload?.ready, true, 'Core readiness payload must be true')
      assert.equal(legacyPending.status, 503, 'Legacy readiness must retain aggregate semantics')
      assert.equal(legacyPending.payload?.ready, false, 'Legacy readiness must remain pending')
      assert.ok(Object.values(optionalStates).some(status => status === 'starting' || status === 'degraded'),
        'Delayed optional integration must be observable as starting or degraded at core readiness')
      report.coreReadyBeforeLegacy = true
      // Deferred warmups start after core publication and are intentionally
      // scheduled independently.  Wait for the synthetic MCP connection to
      // be observed before sampling its in-flight state; otherwise a slow
      // Windows scheduler can make this probe report a fixture race.
      await until(() => sseRequests === 1, 'delayed MCP fixture request', 20_000)
      const coreWhileMcpPending = await readinessAtPort(coreOwner.port, '/readyz/core')
      report.readinessProbe.coreWhileMcpPendingStatus = coreWhileMcpPending.status
      report.readinessProbe.coreWhileMcpPending = coreWhileMcpPending.payload?.ready === true
      report.readinessProbe.coreWhileMcpServices = coreWhileMcpPending.payload?.services || {}
      assert.equal(coreWhileMcpPending.status, 200, 'Core readiness must remain available while MCP is pending')
      assert.equal(coreWhileMcpPending.payload?.ready, true, 'Core readiness must remain true while MCP is pending')
      assert.equal(sseRequests, 1, 'Delayed MCP fixture was actually reached exactly once')
      assert.equal(records.size, 1, 'The pre-readiness child must be identified')
      const ownerAtCoreReady = [...records.values()][0]
      // Avoid turning the intentionally pending SSE into a high-rate
      // control-plane polling test. Sample once shortly before the configured
      // deadline, then once after a bounded drain margin.
      const lateReadyTimeoutSeconds = Number.parseInt(process.env.OPENSQUILLA_LATE_READY_TIMEOUT_SECONDS || '135', 10)
      await delay(Math.max(0, lateReadyTimeoutSeconds - 15) * 1_000)
      const legacyAfterWait = await readinessAtPort(ownerAtCoreReady.port, '/readyz')
      report.readinessProbe.legacyAfterWaitStatus = legacyAfterWait.status
      report.readinessProbe.legacyAfterWait = legacyAfterWait.payload?.ready === true
      await delay(20_000)
      const legacyTerminal = await readinessAtPort(ownerAtCoreReady.port, '/readyz')
      report.readinessProbe.legacyTerminalStatus = legacyTerminal.status
      report.readinessProbe.legacyTerminal = legacyTerminal.payload?.ready === true
      assert.equal(legacyTerminal.status, 200, 'Legacy readiness must converge after optional drain')
      const recovered = currentOwner()
      assert.ok(recovered && ownership.sameDesktopGatewayOwnershipInstance(ownerAtCoreReady, recovered), 'Late optional recovery must use the original child')
      assert.equal(records.size, 1, 'Optional recovery must not spawn a replacement')
      assert.equal(sseClosed, 1, 'Timed-out MCP connection must converge before ready')
      report.legacyReadyAfterOptionalDrain = true
      mark('late-ready-recovered')
    } else if (options.scenario === 'renderer-crash-reload' || options.scenario === 'renderer-hard-kill') {
      await ready('initial-ui-connected')
    } else if (options.scenario === 'restart') {
      const before = await ready('initial-ui-connected')
      if (options.repeatProfile) {
        await until(() => report.sockets.some(socket => socket.responses?.['sessions.list']?.ok > 0), 'initial session directory', 30_000)
        report.repeatProfile.sidebarRowsBefore = (await bounded(page.evaluate(inspectSyntheticSidebar, null), 5_000)).loadedCount
      }
      const sentinel = randomUUID()
      await bounded(page.evaluate(value => { window.__gatewayReliabilityDocument = value }, sentinel), 5_000)
      mark('runtime-restart')
      await beginRuntimeRestart()
      await finishRuntimeRestart(before, sentinel)
      mark('runtime-restart-recovered')
    } else if (options.scenario === 'history-streaming-restart') {
      const before = await ready()
      const sentinel = randomUUID()
      await bounded(page.evaluate(value => { window.__gatewayReliabilityDocument = value }, sentinel), 5_000)
      await enterSettledDraft()
      mark('build-long-history-through-ui')
      for (let index = 0; index < HISTORY_TURNS; index += 1) {
        const turn = historyTurn(index)
        await submitUiTurn(turn.message)
        await until(async () => (await page.locator('.msg-ai-text').allTextContents()).some(text => text.includes(`Synthetic retained history answer ${index + 1}.`)),
          'synthetic history answer', 45_000)
        await waitCompletedUiTurn()
        assert.equal(report.sockets.reduce((sum, socket) => sum + (socket.conversationFailures || 0), 0), 0,
          'Synthetic turns must not fail after displaying partial text')
        assert.equal(chats, index + 1, 'Exactly one provider request for each submitted UI turn')
      }
      const historicalKey = new URL(page.url()).searchParams.get('session')
      assert.ok(historicalKey, 'The submitted session must materialize')
      report.longHistory = { turns: HISTORY_TURNS, answerBytes: HISTORY_TURNS * HISTORY_ANSWER_BYTES,
        generatedThroughRealUi: true, boundary: 'Synthetic bounded history, not the 2.25M-file profile or an interrupted snapshot transfer.' }
      assert.equal(await gatewayShutdownCount(), 0, 'No prior Gateway shutdown in the streaming fixture')
      await submitUiTurn(STREAM_MESSAGE)
      await until(async () => streamResponse && !streamResponse.response.destroyed
        && (await page.locator('.msg-ai-text').allTextContents()).some(text => text.includes(STREAM_ANSWER)), 'synthetic stream started', 45_000)
      await page.locator('.chat-stop-btn').waitFor({ state: 'visible', timeout: 15_000 })
      assert.equal(streamEnded, false, 'Synthetic stream must remain active until shutdown begins')
      // Durable history and live snapshot are different read paths. Reopening
      // the still-streaming session creates an actual multi-segment active base;
      // its large history is independently checked through chat.history below.
      mark('reopen-live-multisegment-snapshot')
      await enterSettledDraft()
      const historyRow = await persistedHistoryRow(historicalKey)
      readProbe.key = historicalKey
      readProbe.armed = true
      readProbe.revision++
      readProbe.historyPhase = 'capture'
      readProbe.historyIds = null
      await historyRow.locator('.sidebar-history-item').click()
      await until(async () => report.sockets.some(socket => socket.targetMultiSegmentCompleted > 0 && socket.targetResumeCompleted > 0
        && socket.targetSyntheticHistoryCaptured > 0)
        && (await page.locator('.msg-ai-text').allTextContents()).some(text => text.includes(STREAM_ANSWER)), 'long history recovery', 60_000)
      await page.locator('.chat-stop-btn').waitFor({ state: 'visible', timeout: 15_000 })
      report.longHistory.multiSegmentActiveSnapshotBeforeRestart = true
      report.longHistory.maxSnapshotSegments = Math.max(...report.sockets.map(socket => socket.targetReadMaxSegments || 0))
      const socketCountBeforeRestart = report.sockets.length
      readProbe.revision++ // Pre-restart in-flight replies cannot satisfy recovery.
      readProbe.historyPhase = 'verify'
      mark('restart-during-provider-stream')
      await beginRuntimeRestart()
      await until(async () => await gatewayShutdownCount() === 1, 'Gateway shutdown request', 20_000)
      assert.ok(!streamEnded && !streamResponse.response.destroyed, 'Synthetic stream must remain active until shutdown begins')
      report.streamingCrossedShutdown = true
      mark('provider-finished-after-shutdown-request')
      streamResponse.finish()
      assert.equal(streamEnded, true, 'Stream must finish only after shutdown was observed')
      await finishRuntimeRestart(before, sentinel)
      await until(async () => report.sockets.slice(socketCountBeforeRestart)
        .some(socket => socket.targetSyntheticHistoryVerified > 0 && socket.targetResumeCompleted > 0)
        && (await page.locator('.msg-ai-text').allTextContents()).some(text => text.includes(STREAM_ANSWER)), 'long history recovery', 60_000)
      assert.equal(new URL(page.url()).searchParams.get('session'), historicalKey, 'Long history recovery must use the original session')
      assert.equal(chats, HISTORY_TURNS + 1, 'Exactly one provider request for each submitted UI turn')
      report.longHistory.recoveredAfterRestart = true
      report.longHistory.recoveredHistoryWireBytes = Math.max(...report.sockets.slice(socketCountBeforeRestart).map(socket => socket.targetHistoryMaxWireBytes || 0))
      report.longHistory.recoveredHistoryMessages = Math.max(...report.sockets.slice(socketCountBeforeRestart).map(socket => socket.targetHistoryMaxMessages || 0))
      report.longHistory.preservedHistoricalMessages = HISTORY_TURNS * 2
      report.longHistory.messageIdsPreserved = true
      readProbe.armed = false
      readProbe.revision++
      readProbe.historyPhase = null
      readProbe.historyIds = null
      mark('long-history-streaming-recovered')
    } else {
      const before = await ready()
      const sentinel = randomUUID()
      await bounded(page.evaluate(value => { window.__gatewayReliabilityDocument = value }, sentinel), 5_000)
      mark('configuration-editor')
      await page.locator('.sidebar-foot button[data-icon="settings"]').click()
      await page.locator('#settings-rail-provider').click()
      await page.locator('[data-testid="configured-provider-list"] [data-provider-id="ollama"] .setup-provider-card__select').click()
      const editor = page.locator('#setup-provider-editor-dialog')
      const endpoint = editor.locator('[data-name="base_url"] input')
      await endpoint.waitFor({ state: 'visible', timeout: 30_000 })
      assert.equal(await endpoint.inputValue(), provider.url, 'Synthetic primary endpoint must be selected')
      await endpoint.fill(alternateProvider.url)
      mark('configuration-save')
      await editor.locator('.setup-provider-modal__footer .btn--primary').click()
      await until(() => report.sockets.reduce((sum, socket) => sum + (socket.configurationSucceeded || 0), 0) === 1,
        'real provider.configure success', 45_000)
      await editor.waitFor({ state: 'hidden', timeout: 30_000 })
      await page.locator('.settings-modal__close').click()
      const after = await ready()
      assert.ok(ownership.sameDesktopGatewayOwnershipInstance(before, after), 'WebUI provider save should hot-apply without a child replacement')
      assert.equal(records.size, 1, 'Provider configure must preserve the current owner')
      assert.equal(await bounded(page.evaluate(() => window.__gatewayReliabilityDocument), 5_000), sentinel, 'Configuration save must preserve the original document')
      assert.equal(report.sockets.reduce((sum, socket) => sum + (socket.methods['onboarding.provider.configure'] || 0), 0), 1,
        'One real configuration submission')
      report.configurationHotApplied = true
      report.originalDocumentPreserved = true
      mark('configuration-saved')
    }
    mark('single-ui-send')
    assert.equal(chats, expectedChats - 1, 'Fixture must not make hidden model requests before user submission')
    // /chat may be the default main session, which is not necessarily a
    // normal recents row. Use the real New task action to create the webchat
    // this probe will reopen later, preserving the current document.
    await enterSettledDraft()
    await page.locator('.chat-textarea').fill(MESSAGE)
    const send = page.locator('.chat-send-btn.btn--primary')
    await until(async () => await send.count() === 1 && !await send.isDisabled(), 'send enabled', 45_000)
    mark('single-ui-send-click')
    await send.click()
    await until(async () => (await page.locator('.msg-ai-text').allTextContents()).some(text => text.includes(ANSWER)), 'visible answer', 45_000)
    mark('single-ui-answer-visible')
    await until(async () => !await send.isDisabled(), 'turn finished', 45_000)
    await page.locator('.msg-ai').last().locator('.msg-meta__more-btn').waitFor({ state: 'visible', timeout: 45_000 })
    mark('single-ui-send-complete')
    await verifySingleRenderedTurn()
    assert.equal(chats, expectedChats, 'One UI submission must produce exactly one provider chat request')
    if (options.scenario === 'configuration') assert.equal(alternateChats, 1, 'The edited endpoint must serve the actual user turn')
    assert.equal(report.sockets.reduce((sum, socket) => sum + (socket.methods['chat.send'] || 0), 0), expectedChats, 'No repeated chat.send')
    assert.equal(pageErrors, 0, 'Renderer must not raise page errors')
    assert.equal(report.sockets.reduce((sum, socket) => sum + (socket.conversationFailures || 0), 0), 0,
      'Synthetic turns must not fail after displaying partial text')
    report.singleSendVerified = true
    mark('history-reread')
    await until(() => Boolean(new URL(page.url()).searchParams.get('session')), 'materialized session route', 15_000)
    const sessionKey = new URL(page.url()).searchParams.get('session')
    assert.ok(sessionKey, 'The submitted session must materialize')
    readProbe.key = sessionKey
    readProbe.revision++
    if (options.scenario === 'renderer-crash-reload' || options.scenario === 'renderer-hard-kill') {
      const before = currentOwner()
      const socketsBefore = report.sockets.length
      const readsBeforeCrash = report.sockets.reduce((sum, socket) => sum + (socket.targetReadCompleted || 0), 0)
      readProbe.armed = true
      readProbe.revision++
      mark('renderer-crash-injection')
      const routeBeforeCrash = page.url()
      // Windows taskkill can invalidate an in-flight CDP command before
      // Playwright dispatches its page.crash event. Drain the observer first
      // so the hard-kill probe measures the product's recovery logs rather
      // than an unrelated monitor command racing the process teardown.
      if (options.scenario === 'renderer-hard-kill') {
        stopped = true
        if (monitor) await monitor
      }
      const recoverRenderer = options.scenario === 'renderer-hard-kill' ? hardKillAndReloadRenderer : crashAndReloadRenderer
      const rendererRecovery = await recoverRenderer(app, page, async () => {
        let recovery
        await until(async () => {
          const records = structuredLogRecords(await desktopLogText())
          const ready = records.find(item => item.event === 'renderer_recovery_ready')
          const gone = records.find(item => item.event === 'renderer_process_gone'
            && item.reason === (options.scenario === 'renderer-hard-kill' ? 'killed' : item.reason))
          const interactive = records.filter(item => item.event === 'renderer_interactive'
            && item.at && ready?.generation !== undefined).at(-1)
          if (!ready || !interactive) return false
          const recoveredSessionKey = new URL(interactive.url).searchParams.get('session')
          const sameRoute = typeof interactive.url === 'string'
            && interactive.url.startsWith('opensquilla-app://desktop/chat')
            && Boolean(recoveredSessionKey)
            && (!sessionKey || sessionKey === recoveredSessionKey)
          recovery = { generation: ready.generation, processGoneReason: gone?.reason || null,
            newRendererPid: ready.rendererPid, sameWindow: true, sameRoute, documentRebuilt: true,
            routeBeforeCrash, recoveredRoute: interactive.url, sessionKey: sessionKey || null,
            recoveredSessionKey: recoveredSessionKey || null }
          return true
        }, 'renderer recovery log', 45_000)
        return recovery
      })
      report.rendererFault = rendererRecovery.evidence
      mark('renderer-reload-connected')
      const after = currentOwner()
      assert.ok(after, 'Renderer recovery must retain a live Gateway owner')
      assert.ok(ownership.sameDesktopGatewayOwnershipInstance(before, after), 'Renderer crash must preserve the Gateway owner')
      assert.equal(records.size, 1, 'Renderer crash must not spawn duplicate Gateway owners')
      await until(() => report.sockets.slice(socketsBefore).some(socket => !socket.closed && socket.hello?.stateDirMatches)
        || report.sockets.length === socketsBefore,
      'renderer authority recovery', 45_000)
      assert.equal(chats, 1, 'Renderer recovery must not replay an accepted turn')
      assert.equal(report.sockets.reduce((sum, socket) => sum + (socket.methods['chat.send'] || 0), 0), 1,
        'Renderer recovery must not resend chat.send')
      Object.assign(report.rendererFault, { ownerPreserved: true, authorityReadAfterReload: true,
        singleSendPreserved: true, gatewayOwnerCount: records.size })
      readProbe.armed = false
      readProbe.revision++
      mark('renderer-authority-recovered')
    }
    if (options.scenario !== 'renderer-crash-reload' && options.scenario !== 'renderer-hard-kill') {
    report.sidebarBeforeLeave = await bounded(page.evaluate(inspectSyntheticSidebar, sessionKey), 5_000)
    mark('history-session-captured')
    await enterSettledDraft()
    report.sidebarAfterLeave = await bounded(page.evaluate(inspectSyntheticSidebar, sessionKey), 5_000)
    mark('history-new-draft')
    const row = await persistedHistoryRow(sessionKey)
    mark('history-sidebar-ready')
    // Arm only after the draft navigation finishes. Unrelated draft reads,
    // earlier pending responses and cached text cannot satisfy this proof.
    const readsBefore = report.sockets.reduce((sum, socket) => sum + (socket.targetReadCompleted || 0), 0)
    readProbe.armed = true
    readProbe.revision++
    readProbe.readsBefore = readsBefore
    readProbe.historyReadsBefore = report.sockets.reduce((sum, socket) => sum + (socket.targetHistoryCompleted || 0), 0)
    readProbe.requestsBefore = report.sockets.reduce((sum, socket) => sum + (socket.targetReadRequests || 0), 0)
    mark('history-reread-click')
    await row.locator('.sidebar-history-item').click()
    await until(async () => historyRereadReady(report.sockets, readProbe,
      new URL(page.url()).searchParams.get('session'),
      await page.locator('.msg-user-bubble').allTextContents(),
      await page.locator('.msg-ai-text').allTextContents()), 'history re-read and visible answer', 45_000)
    mark('history-reread-complete')
    await verifySingleRenderedTurn()
    assert.equal(chats, expectedChats, 'Reopening history must not replay the turn')
    report.historyRereadVerified = true
    if (options.repeatProfile) {
      report.repeatProfile.sidebarRowsAfter = (await bounded(page.evaluate(inspectSyntheticSidebar, null), 5_000)).loadedCount
      assert.ok(Number.isSafeInteger(report.repeatProfile.sidebarRowsBefore)
        && report.repeatProfile.sidebarRowsAfter === report.repeatProfile.sidebarRowsBefore + 1,
      'Ordinary run must add exactly one sidebar session')
      report.performance = ordinaryPerformanceMetrics(report.phases)
      report.performanceBoundary = 'Host monotonic observations include Playwright and 100ms polling latency. First send follows Restart; local synthetic provider and warm OS cache, not model/network latency.'
    }
    checkpoint = await desktopLogText()
    mark('verified-before-cleanup')
    }
  } catch (error) {
    failure = true
    // Assertion text is controlled by this script. Do not serialize arbitrary
    // Playwright errors (they can include URLs, tokens, inputs and DOM text).
    report.failure = { phase, kind: error?.name === 'AssertionError' ? 'assertion' : 'operation-failed', reason: safeFailureReason(error) }
    if (process.env.OPENSQUILLA_DEBUG_NATIVE === '1') {
      report.failure.debug = error?.message || String(error)
    }
    if (page && readProbe.key) {
      report.sidebarAtFailure = await bounded(page.evaluate(inspectSyntheticSidebar, readProbe.key), 5_000).catch(() => ({ unavailable: true }))
      report.historyAtFailure = {
        targetReadRequestedSinceArm: Number.isSafeInteger(readProbe.requestsBefore)
          ? report.sockets.reduce((sum, socket) => sum + (socket.targetReadRequests || 0), 0) > readProbe.requestsBefore : null,
        targetReadCompletedSinceArm: Number.isSafeInteger(readProbe.readsBefore)
          ? report.sockets.reduce((sum, socket) => sum + (socket.targetReadCompleted || 0), 0) > readProbe.readsBefore : null,
        answerVisible: await bounded(page.locator('.msg-ai-text').allTextContents()
          .then(values => values.some(text => text.includes(ANSWER))), 5_000).catch(() => null),
      }
    }
  } finally {
    mark('cleanup')
    if (streamResponse && !streamEnded && !streamResponse.response.destroyed) {
      streamResponse.response.destroy()
      report.fixture.pendingProviderClosedForCleanup = true
    }
    try {
      await cleanup.cleanupPackagedFirstSend({ app, processIdentity,
        diagnostics: () => ({ scenario: options.scenario, ownerCount: records.size, pageErrors }),
        emit: () => {} })
      observeOwner()
      assert.ok(records.size > 0, 'No child identity captured; cleanup remains unproven')
      for (const record of records.values()) assert.equal(shutdown.gatewayProcessSnapshot(record).alive, false, 'Owned child still present')
      assert.equal(ownership.loadDesktopGatewayOwnershipRecord(ownershipDir).status, 'missing')
      const log = await desktopLogText()
      const spawned = []
      for (const line of log.split(/\r?\n/)) {
        let item
        try { item = JSON.parse(line) } catch { continue }
        if (item.event === 'gateway_spawned') spawned.push({ pid: item.pid, port: item.port })
      }
      report.cleanup.spawnCount = spawned.length
      report.cleanup.everySpawnIdentified = spawned.length === records.size && spawned.every(spawn =>
        [...records.values()].some(record => record.pid === spawn.pid && record.port === spawn.port))
      assert.ok(report.cleanup.everySpawnIdentified, 'A child missed by ownership observation leaves cleanup unproven')
      assert.ok(hasNaturalGatewayExits(log, [...records.values()].map(record => record.pid)),
        'Every owned Gateway must exit naturally')
      if (checkpoint) assert.deepEqual(shutdown.desktopShutdownEvidenceSince(checkpoint,
        log),
      { gatewayExitLogged: true, committedExitLogged: true })
      report.cleanup.verified = true
    } catch (error) { failure = true; report.cleanup.verified = false; report.cleanup.failureReason = safeFailureReason(error) }
    stopped = true
    if (monitor) await monitor
    for (const fixture of [sse, alternateProvider, provider]) {
      if (fixture) try { await fixture.close() } catch { failure = true; report.cleanup.fixtureFailure = true }
    }
    report.providerChatRequests = chats
    report.expectedProviderChatRequests = expectedChats
    report.alternateProviderChatRequests = alternateChats
    report.sse = { requests: sseRequests, closed: sseClosed }
    report.pageErrors = pageErrors
    report.observationFailed = monitorError
    report.conversationFailures = report.sockets.reduce((sum, socket) => sum + (socket.conversationFailures || 0), 0)
    try {
      const log = await gatewayLogText()
      report.gatewayFlowFailures = gatewayFlowFailureEvidence(log, records.size)
      if (options.repeatProfile) {
        report.repeatProfile.migrations = ordinaryMigrationEvidence(log, records.size)
        if (!report.repeatProfile.preparation) assert.equal(report.repeatProfile.migrations.alreadyMigrated, true,
          'Repeated profile must not perform first-run migrations')
      }
    } catch { failure = true; report.gatewayFlowFailures = { available: false } }
    report.ok = !failure && !monitorError && pageErrors === 0 && report.conversationFailures === 0 && chats === expectedChats && report.cleanup.verified
      && hasCleanGatewayFlowEvidence(report.gatewayFlowFailures)
      && report.sockets.every(socket => !socket.responseObservationOverflow)
    if (options.repeatProfile && report.ok) {
      try {
        const configSha256 = await fileHash(configPath)
        const credentialSha256 = await fileHash(credentialPath)
        // Preparation may normalize settings; warm samples may not rewrite them.
        if (!report.repeatProfile.preparation) {
          assert.equal(configSha256, repeatMarker.configSha256, 'Repeated profile configuration changed')
          assert.equal(credentialSha256, repeatMarker.credentialSha256, 'Repeated profile credential changed')
        }
        repeatMarker.configSha256 = configSha256
        repeatMarker.credentialSha256 = credentialSha256
        repeatMarker.clean = true
        repeatMarker.completedRuns++
        report.repeatProfile.completedRunsAfter = repeatMarker.completedRuns
        await writeFile(markerPath, JSON.stringify(repeatMarker, null, 2) + '\n')
        await repeatLock.close()
        await unlink(lockPath)
      } catch { report.ok = false; report.repeatProfile.finalizationFailed = true }
    }
    if (repeatLock) await repeatLock.close() // On failure retain the lock file, never automatically adopt dirty state.
    await persist()
  }
  console.log(JSON.stringify({ event: 'gateway_reliability_result', scenario: options.scenario, ok: report.ok,
    cleanupVerified: report.cleanup.verified, output: options.output }))
  if (!report.ok) process.exitCode = 1
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { await run(parseArguments(process.argv.slice(2))) }
  catch (error) { console.error(error?.code === 'ERR_ASSERTION' ? error.message : 'Gateway reliability harness setup failed; inspect the isolated fixture.'); process.exitCode = 1 }
}
