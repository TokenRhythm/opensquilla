import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFile } from 'node:child_process'
import { cp, mkdir, readFile, realpath, rename, rm, symlink, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { createServer as createHttpServer } from 'node:http'
import { delimiter, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { promisify } from 'node:util'
import { _electron as electron } from 'playwright'
import { environmentWithoutProviderSecrets } from './packaged-smoke-helpers.mjs'
import { JOURNEY_CASES as cases, createBusinessDriver, journeyPrompts, verifyBusinessCase } from './live-html-journey-business.mjs'
import { installNativeObservation, nativeObservation } from './live-html-native-observation.mjs'
import { capturePageVisualEvidence } from './live-html-visual-evidence.mjs'
import { requireDesktopForeground, withForegroundActions } from './live-html-foreground.mjs'
import { createSupplementalClient } from './live-html-supplemental-client.mjs'
import { SUPPLEMENTAL_CASES, runSupplementalScenario, supplementalPromptHash } from './live-html-supplemental-scenarios.mjs'

const exec = promisify(execFile)

function option(name, fallback = '') {
  const index = process.argv.indexOf(name)
  return index === -1 ? fallback : process.argv[index + 1]
}
if (process.argv.includes('--help')) {
  console.log('Usage: node live-html-journey.mjs --source-root PATH --output PATH --variant baseline|new --case CASE --repetition N [--request-log PATH | --ledger PATH] [--supplemental] [--ledger-case CASE_ID] [--attempt-label LABEL] [--relay-ready PATH] [--preflight-only] [--packaged-executable PATH --proxy-ready PATH]')
  console.log('The relay supplies base_url/client_key JSON. Source launches require OPENSQUILLA_LIVE_TRANSPORT=1 and OPENSQUILLA_LIVE_TRANSPORT_DIR for the isolated fail-closed bootstrap; packaged launches require a reviewed process-only TLS proxy. Never pass a real provider key. Source dependencies serve evidence inspection only in packaged mode. No turn, model, output, router, or retry settings are overridden.')
  process.exit(0)
}
const sourceRoot = await realpath(resolve(option('--source-root')))
const outputRoot = resolve(option('--output'))
assert.ok(option('--source-root') && option('--output'), 'Explicit source and evidence roots are required.')
const variant = option('--variant')
assert.ok(['baseline', 'new'].includes(variant), 'Choose baseline or new.')
const supplemental = process.argv.includes('--supplemental')
assert.ok(!supplemental || variant === 'new', 'Supplemental scenarios exercise the new client.')
const caseId = option('--case', 'landing')
assert.ok(Object.hasOwn(supplemental ? SUPPLEMENTAL_CASES : cases, caseId), 'Unknown journey case.')
const repetition = Number(option('--repetition', '1'))
assert.ok(Number.isSafeInteger(repetition) && repetition > 0, 'Repetition must be a positive integer.')
const preflightOnly = process.argv.includes('--preflight-only')
const packagedExecutable = option('--packaged-executable')
const proxyReadyPath = option('--proxy-ready')
const reviewedNonvisionModel = option('--reviewed-nonvision-model')
const reviewedCatalogPath = option('--reviewed-catalog')
assert.ok(!reviewedNonvisionModel || supplemental && caseId === 'attachment-capability' && reviewedCatalogPath, 'A reviewed catalog is required for the nonvisual-model scenario.')
assert.ok(!packagedExecutable || variant === 'new' && proxyReadyPath, 'Packaged acceptance requires the new variant and a reviewed test proxy.')
const ledgerPath = option('--ledger')
const ledgerCase = option('--ledger-case', `${supplemental ? 'supplemental-' : ''}${caseId}-r${repetition}`)
const attemptLabel = option('--attempt-label', 'original')
assert.match(attemptLabel, /^[a-zA-Z0-9_-]{1,80}$/, 'Use a short attempt label.')
const relayReady = option('--relay-ready')
const relay = relayReady ? JSON.parse(await readFile(relayReady, 'utf8')) : {
  base_url: process.env.OPENSQUILLA_LIVE_RELAY_URL,
  client_key: process.env.OPENSQUILLA_LIVE_RELAY_CLIENT_KEY,
}
const requestLogPath = await resolveRequestLogPath(ledgerPath, option('--request-log'), relay)
assert.ok(preflightOnly || ledgerPath || requestLogPath, 'A read-only request log or budget ledger is required for a provider journey.')
const relayUrl = new URL(relay.base_url)
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(relayUrl.hostname), 'Relay must be loopback.')
assert.equal(relayUrl.protocol, 'http:', 'Relay must use loopback HTTP.')
assert.ok(typeof relay.client_key === 'string' && relay.client_key.startsWith('live-budget-placeholder-') && relay.client_key.length >= 40, 'Relay placeholder credential is missing.')
assert.ok(!process.env.TOKENRHYTHM_API_KEY || process.env.TOKENRHYTHM_API_KEY === relay.client_key, 'REAL_PROVIDER_KEY_MUST_NOT_ENTER_DRIVER')
const runRoot = join(outputRoot, `${variant}-${supplemental ? 'supplemental-' : ''}${caseId}-r${repetition}`)
await mkdir(runRoot, { recursive: true, mode: 0o700 })
await writeFile(join(runRoot, 'attempt.claim'), new Date().toISOString(), { flag: 'wx', mode: 0o600 })
const profile = join(runRoot, 'electron-user-data')
const stateRoot = join(runRoot, 'user-state')
const desktopRoot = join(sourceRoot, 'desktop', 'electron')
const shellRoot = join(runRoot, 'source-shell')
const python = join(sourceRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const filename = supplemental ? 'supplemental.html' : `journey-${caseId}.html`
const prompts = supplemental ? supplementalPromptHash(caseId).prompts : journeyPrompts(caseId, filename)
const transportDir = process.env.OPENSQUILLA_LIVE_TRANSPORT_DIR
assert.ok(packagedExecutable || process.env.OPENSQUILLA_LIVE_TRANSPORT === '1' && transportDir, 'A fail-closed isolated transport shim is required.')
let proxy
if (packagedExecutable) {
  proxy = JSON.parse(await readFile(proxyReadyPath, 'utf8'))
  const url = new URL(proxy.proxy_url)
  assert.ok(url.protocol === 'http:' && url.hostname === '127.0.0.1' && url.port && !url.username && !url.password, 'PACKAGED_PROXY_MUST_BE_LOOPBACK')
  assert.equal(proxy.client_key, relay.client_key, 'PACKAGED_PROXY_RELAY_MISMATCH')
  assert.equal(await realpath(proxy.relay_ready), await realpath(relayReady), 'PACKAGED_PROXY_LEDGER_RELAY_MISMATCH')
  assert.ok(isAbsolute(proxy.ca_file) && (await readFile(proxy.ca_file, 'utf8')).includes('BEGIN CERTIFICATE'), 'PACKAGED_PROXY_CA_REQUIRED')
}
const cleanEnv = environmentWithoutProviderSecrets(process.env)
for (const name of Object.keys(cleanEnv)) {
  if (name.startsWith('OPENSQUILLA_') || name.startsWith('TOKENRHYTHM_') || /^(https?|all|no)_proxy$/i.test(name)) delete cleanEnv[name]
}
const report = {
  schemaVersion: 1, variant, caseId, repetition, attemptLabel, preflightOnly, supplemental, packaged: Boolean(packagedExecutable),
  startedAt: new Date().toISOString(), status: 'running',
  source: {}, configuration: {}, checkpoints: [], turns: [], events: [], streamSummaries: [], errors: [], qualityFindings: [], businessChecks: [], checks: [], materialSnapshots: 0,
  prompts, promptsSha256: createHash('sha256').update(JSON.stringify(prompts)).digest('hex'),
  execution: { modelAndRouterOverrides: false, prescribedToolSequence: false, automaticRetries: 0, realProviderCredentialInClient: false },
}
let app, page
let activeTurn = null
let terminalError = null
let stderrBytesDiscarded = 0
let gatewayPort
let selectedPreviewId = null
let businessDriver
let lastDesktopProbe = 0
let closing = false
let fixtureServer
let supplementalClient
const streamSummaryByKey = new Map()
const rpcMethods = new Map()
const toolDeliveries = new Map()
let websocketSequence = 0
const started = Date.now()

function onSignal(signal) {
  terminalError = new Error(`HARNESS_INTERRUPTED_${signal}`)
  report.interruption = { signal, elapsedMs: Date.now() - started }
  void persist()
}
process.on('SIGINT', () => onSignal('SIGINT'))
process.on('SIGTERM', () => onSignal('SIGTERM'))

function safeError(error) {
  const code = String(error?.message || '').match(/^[A-Z][A-Z0-9_]{2,}(?=[:\s]|$)/)?.[0] || String(error?.code || '').match(/^[A-Z][A-Z0-9_]{2,}$/)?.[0] || 'HARNESS_ERROR'
  return { code, errorClass: String(error?.name || 'Error').slice(0, 80), ...(error?.diagnostic || {}) }
}
async function persist() {
  await writeFile(join(runRoot, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 })
}
async function waitFor(check, label, timeoutMs) {
  const until = timeoutMs === 0 ? Infinity : Date.now() + timeoutMs
  while (Date.now() < until) {
    if (terminalError) throw terminalError
    if (app && Date.now() - lastDesktopProbe > 2000) {
      lastDesktopProbe = Date.now()
      report.desktopEvents = await desktopEvents()
      if (report.desktopEvents.some(item => item.event === 'desktop_open_failed')) throw new Error('DESKTOP_STARTUP_FAILED')
    }
    const result = await check()
    if (result) return result
    await delay(250)
  }
  const error = new Error('HARNESS_WAIT_EXPIRED')
  error.diagnostic = { reason: label, timeoutMs }
  throw error
}
async function reservePort() {
  const server = createServer()
  await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done) })
  const port = server.address().port
  await new Promise(done => server.close(done))
  return port
}
function evidenceHash(value) {
  const canonical = item => Array.isArray(item)
    ? item.map(canonical)
    : item && typeof item === 'object'
      ? Object.fromEntries(Object.keys(item).sort().map(key => [key, canonical(item[key])]))
      : item
  return createHash('sha256').update(report.startedAt).update('\0').update(JSON.stringify(canonical(value))).digest('hex')
}
function sessionIdentityHash(value) {
  const key = value?.key ?? value?.session_key ?? value?.sessionKey
  return typeof key === 'string' && key ? evidenceHash(key) : undefined
}
async function resolveRequestLogPath(ledger, requestedLog, ready) {
  assert.ok(!ledger || !requestedLog, 'REQUEST_LOG_AND_LEDGER_ARE_EXCLUSIVE')
  if (ready.mode !== 'functional') {
    assert.ok(!requestedLog, 'FUNCTIONAL_RELAY_READY_REQUIRED')
    return ''
  }
  assert.ok(!ledger && typeof ready.request_log === 'string' && isAbsolute(ready.request_log), 'FUNCTIONAL_REQUEST_LOG_REQUIRED')
  const physical = await realpath(ready.request_log)
  if (requestedLog) assert.equal(await realpath(resolve(requestedLog)), physical, 'FUNCTIONAL_REQUEST_LOG_MISMATCH')
  return physical
}
function observeRendererDiagnostic(message) {
  const text = message.text()
  if (!/^Session (?:stream subscription|metadata hydration|metadata recovery) failed:/.test(text)) return
  const timeout = text.match(/\b((?:sessions|chat)\.[a-z_.]+) timed out after (\d+)ms\b/)
  const record = { elapsedMs: Date.now() - started, type: 'diagnostic', event: 'renderer.session_read_failure', code: timeout ? 'RPC_TIMEOUT' : 'SESSION_READ_DIAGNOSTIC', messageHash: evidenceHash(text) }
  if (timeout) { record.method = timeout[1]; record.timeoutMs = Number(timeout[2]) }
  if (activeTurn?.taskId) record.task_id = activeTurn.taskId
  if (report.events.length < 100000) report.events.push(record)
  else report.evidenceIncomplete = true
}
function observeConnection(event, connectionId) {
  if (report.events.length < 100000) report.events.push({ elapsedMs: Date.now() - started, type: 'connection', event, connectionId })
  else report.evidenceIncomplete = true
}
async function composerStatus() {
  const observed = await page.evaluate(() => {
    const controls = selector => [...document.querySelectorAll(selector)].map(element => {
      const rect = element.getBoundingClientRect()
      const style = getComputedStyle(element)
      return { visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden', disabled: Boolean(element.disabled) }
    })
    return { pathname: location.pathname, connected: Boolean(document.querySelector('.conn-pill.connected')), send: controls('.chat-send-btn.btn--primary'), stop: controls('.chat-send-btn.btn--danger'), textarea: controls('.chat-textarea') }
  })
  const { pathname, ...safe } = observed
  return { ...safe, routeHash: evidenceHash(pathname) }
}
function safeTurnOutcome(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const kinds = new Set(['completed', 'partial', 'budgetLimited', 'blocked', 'failed', 'interrupted'])
  const failureKinds = new Set([
    'rate_limited', 'provider_overloaded', 'auth_invalid', 'context_overflow', 'unsupported_feature',
    'insufficient_credits', 'model_not_found', 'transport_transient', 'policy_refusal', 'empty_response',
    'malformed_response', 'bad_request', 'unknown', 'reasoning_only', 'stream_incomplete', 'invalid_response',
  ])
  // These are the runtime's terminal codes, not a general token-shaped-text allowance.
  const codes = new Set([
    'done', 'error', 'agent_error', 'provider_error', 'cancelled', 'cancelled_before_start',
    'dropped_by_overflow', 'interrupted', 'timeout', 'iteration_timeout', 'llm_timeout', 'hard_deadline_exceeded',
    'context_length_exceeded', 'context_overflow', 'current_turn_context_exhausted', 'context_unsalvageable',
    'empty_response', 'ensemble_multimodal_unsupported', 'image_input_unsupported', 'incomplete_stream',
    'incomplete_tool_call', 'incomplete_tool_stream', 'invalid_json', 'invalid_response', 'invalid_response_status',
    'invalid_stream_frame', 'invalid_stream_order', 'model_repetition_loop_detected', 'provider_protocol_error',
    'provider_output_truncated', 'provider_pretext_buffer_exhausted', 'provider_retry_after_deadline',
    'provider_request_budget_exhausted', 'provider_request_too_large', 'request_error', 'response_incomplete',
    'synthetic_upstream_failure', 'usage_limit_reached', 'provider_output_limit', 'tool_run_budget_exhausted',
    'llm_budget_exhausted', 'turn_llm_call_budget_exceeded', 'turn_input_token_budget_exceeded',
    'turn_output_token_budget_exceeded', 'turn_billed_cost_budget_exceeded', 'max_iterations', 'output_truncated',
    'turn_tool_error_budget_exceeded', 'tool_failure_loop_exhausted', 'human_decision_required', 'approval_required',
    'external_dependency', 'provider_unavailable', 'usage_accounting_busy', 'usage_accounting_unavailable',
    'sandbox_threshold_exceeded', 'tool_policy_denied', 'compaction_refused_flush_timeout',
    'compaction_refused_memory_flush', 'compaction_refused_empty_summary',
    ...[...failureKinds].filter(kind => !['unknown', 'reasoning_only', 'stream_incomplete', 'invalid_response'].includes(kind)).map(kind => `provider_${kind}`),
  ])
  const safe = {}
  if (kinds.has(value.kind)) safe.kind = value.kind
  for (const key of ['reason', 'error_class']) {
    if (typeof value[key] === 'string' && (codes.has(value[key]) || /^[45][0-9]{2}$/.test(value[key]))) safe[key] = value[key]
  }
  if (failureKinds.has(value.failure_kind)) safe.failure_kind = value.failure_kind
  if (typeof value.retryable === 'boolean') safe.retryable = value.retryable
  return Object.keys(safe).length ? safe : undefined
}
function observe(frame, direction, connectionId = 0) {
  let raw
  try { raw = JSON.parse(String(frame.payload)) } catch { return }
  const payload = raw.payload || raw.result || {}
  const record = { elapsedMs: Date.now() - started, direction, connectionId, type: raw.type, event: raw.event, id: raw.id }
  const rpcKey = `${connectionId}:${raw.id}`
  const request = direction === 'received' && raw.type === 'res' ? rpcMethods.get(rpcKey) : undefined
  const sessionHash = direction === 'sent' ? sessionIdentityHash(raw.params) : sessionIdentityHash(payload.data) || sessionIdentityHash(payload) || request?.sessionHash
  if (sessionHash) record.sessionHash = sessionHash
  if (direction === 'sent') {
    if (typeof raw.method !== 'string' || !/^[a-zA-Z0-9_.]{1,160}$/.test(raw.method)) return
    record.method = raw.method
    if (rpcMethods.size < 100000) rpcMethods.set(rpcKey, { method: raw.method, sessionHash })
    else report.evidenceIncomplete = true
    if (raw.method === 'chat.send') {
      record.pageContextPresent = Boolean(raw.params?.pageContext)
      record.annotationCount = raw.params?.pageContext?.annotations?.length || raw.params?.promptAnnotationIds?.length || 0
      if (activeTurn) { activeTurn.requestId = raw.id; activeTurn.connectionId = connectionId; activeTurn.submittedAtMs = record.elapsedMs }
    }
  } else if (raw.type === 'res' && activeTurn?.requestId === raw.id && activeTurn.connectionId === connectionId) {
    record.method = request?.method
    record.ok = raw.ok !== false && !raw.error
    activeTurn.acceptedAtMs = record.elapsedMs
    activeTurn.taskId = payload.task_id || payload.taskId || null
    const errorCode = String(raw.error?.code || 'RPC_ERROR')
    activeTurn.error = raw.error ? { code: /^[A-Za-z0-9_.:-]{1,160}$/.test(errorCode) ? errorCode : 'RPC_ERROR', type: 'rpc' } : null
    if (raw.error || raw.ok === false) terminalError = new Error(`CHAT_SEND_REJECTED: ${activeTurn.error?.code || 'RPC_ERROR'}`)
    rpcMethods.delete(rpcKey)
  } else if (raw.type === 'res') {
    record.method = request?.method
    rpcMethods.delete(rpcKey)
    if (!record.method) return
    record.ok = raw.ok !== false && !raw.error
    for (const [name, value] of [['errorCode', raw.error?.code], ['errorDataCode', raw.error?.data?.code]]) {
      if (typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value)) record[name] = value
    }
  } else if (raw.type === 'event') {
    const detail = payload.data && typeof payload.data === 'object' ? payload.data : payload
    const outcome = safeTurnOutcome(detail.turn_outcome)
    if (outcome) record.turn_outcome = outcome
    if (typeof detail.error_id === 'string' && /^[a-f0-9]{8}$/.test(detail.error_id)) record.error_id = detail.error_id
    for (const name of ['task_id', 'taskId', 'type', 'state', 'status', 'code', 'reason', 'model', 'model_id', 'tool_name', 'name', 'iteration', 'tool_use_id', 'toolUseId', 'error_class', 'is_error', 'terminal_reason', 'finish_reason', 'guard_reason', 'watchdog_action', 'watchdog_mode', 'execution_status', 'effect_outcome']) {
      if (['string', 'number', 'boolean'].includes(typeof detail[name]) && /^[a-zA-Z0-9_.:/-]{1,160}$/.test(String(detail[name]))) record[name] = detail[name]
    }
    if (['session.event.tool_use_start', 'session.event.tool_use_end', 'session.event.tool_result'].includes(raw.event)) {
      if (detail.arguments !== undefined) record.argumentsHash = evidenceHash(detail.arguments)
      if (raw.event === 'session.event.tool_result' && detail.result !== undefined) record.resultHash = evidenceHash(detail.result)
      const taskId = record.task_id || record.taskId
      const toolId = record.tool_use_id || record.toolUseId
      if (taskId && toolId) {
        record.toolIdentityHash = evidenceHash([taskId, toolId])
        const deliveryKey = `${record.toolIdentityHash}:${raw.event}`
        if (toolDeliveries.has(deliveryKey) || toolDeliveries.size < 100000) {
          record.deliveryIndex = (toolDeliveries.get(deliveryKey) || 0) + 1
          record.repeatedDelivery = record.deliveryIndex > 1
          toolDeliveries.set(deliveryKey, record.deliveryIndex)
        } else report.evidenceIncomplete = true
      }
    }
    if (activeTurn && !activeTurn.firstEventAtMs && String(raw.event).startsWith('session.event.')) activeTurn.firstEventAtMs = record.elapsedMs
  } else return
  if (['session.event.thinking', 'session.event.text_delta', 'session.event.tool_use_delta'].includes(raw.event)) {
    const taskId = record.task_id || record.taskId || activeTurn?.taskId || null
    const key = JSON.stringify([taskId, record.iteration ?? null, record.tool_use_id || record.toolUseId || null, raw.event])
    let summary = streamSummaryByKey.get(key)
    if (!summary && streamSummaryByKey.size < 10000) {
      summary = { taskId, iteration: record.iteration ?? null, toolUseId: record.tool_use_id || record.toolUseId || null, event: raw.event, firstAtMs: record.elapsedMs, lastAtMs: record.elapsedMs, count: 0 }
      streamSummaryByKey.set(key, summary)
      report.streamSummaries.push(summary)
    }
    if (summary) { summary.count += 1; summary.lastAtMs = record.elapsedMs }
    else { report.evidenceIncomplete = true; report.streamEventsDropped = (report.streamEventsDropped || 0) + 1 }
    return
  }
  if (report.events.length < 100000) report.events.push(record)
  else { report.evidenceIncomplete = true; report.criticalEventsDropped = (report.criticalEventsDropped || 0) + 1 }
}
async function runPython(program, ...args) {
  const result = await exec(python, ['-c', program, ...args], {
    cwd: sourceRoot, env: { ...cleanEnv, PYTHONPATH: join(sourceRoot, 'src') }, timeout: 15000, maxBuffer: 4 * 1024 * 1024,
  })
  return JSON.parse(result.stdout)
}
async function preflightSubject() {
  let runtime
  try {
    runtime = await runPython(`import json,sys,httpx,opensquilla
from pathlib import Path
print(json.dumps({'executable':sys.executable,'source':str(Path(opensquilla.__file__).resolve()),'httpx':str(Path(httpx.__file__).resolve())}))`)
  } catch {
    throw new Error('SOURCE_DEPENDENCIES_NOT_READY')
  }
  assert.equal(runtime.source, join(sourceRoot, 'src', 'opensquilla', '__init__.py'), 'SOURCE_IMPORT_MISMATCH')
  report.source.python = runtime
}
async function sourceManifest() {
  const result = await exec('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'], { cwd: sourceRoot, env: cleanEnv, maxBuffer: 8 * 1024 * 1024 })
  const entries = []
  for (const path of new Set(result.stdout.split('\0').filter(Boolean))) {
    if (!/^(src\/|desktop\/electron\/src\/|opensquilla-webui\/src\/|migrations\/)|^(pyproject\.toml|uv\.lock|desktop\/electron\/package(?:-lock)?\.json|opensquilla-webui\/package(?:-lock)?\.json)$/.test(path)) continue
    try { entries.push([path, createHash('sha256').update(await readFile(join(sourceRoot, path))).digest('hex')]) }
    catch (error) { if (error.code !== 'ENOENT') throw error; entries.push([path, 'deleted']) }
  }
  entries.sort(([a], [b]) => a.localeCompare(b))
  await writeFile(join(runRoot, 'source-manifest.json'), JSON.stringify(entries, null, 2), { mode: 0o600 })
  return createHash('sha256').update(JSON.stringify(entries)).digest('hex')
}
async function desktopEvents() {
  let raw
  try { raw = await readFile(join(profile, 'logs', 'desktop.log'), 'utf8') }
  catch (error) { if (error.code === 'ENOENT') return []; throw error }
  const events = []
  for (const line of raw.split('\n')) {
    let row
    try { row = JSON.parse(line) } catch { continue }
    const safe = {}
    for (const key of ['event', 'elapsedMs', 'command', 'code', 'signal', 'durationMs', 'phaseId', 'stableCode', 'outcome']) {
      if (['string', 'number', 'boolean'].includes(typeof row[key]) && /^[a-zA-Z0-9_.:/-]{1,160}$/.test(String(row[key]))) safe[key] = row[key]
    }
    events.push(safe)
  }
  return events
}
async function durableState() {
  return runPython(`import hashlib,json,re,sqlite3,sys
from pathlib import Path
path=Path(sys.argv[1])
if not path.is_file(): print('{}'); raise SystemExit
conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
conn.row_factory=sqlite3.Row
result={}
for table,fields in {'agent_tasks':['task_id','status','created_at','updated_at','terminal_reason','error_class'], 'artifact_documents':['document_id','head_revision_id','generation','state_revision'], 'artifact_revisions':['revision_id','document_id','generation','artifact_id','artifact_sha256','change_set_id','copied_from_revision_id','parent_revision_id'], 'artifact_audit_events':['event_id','document_id','event_type','revision_id','actor_kind','actor_id','created_at'], 'document_publications':['publication_id','document_id','revision_id','deliverable_artifact_id','created_by_kind','created_by_id','created_at'], 'artifact_change_sets':['change_set_id','document_id','status','applied_revision_id'], 'artifact_working_files':['document_id','workspace','relative_root','entrypoint','base_revision_id']}.items():
 if not conn.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone(): continue
 cols={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
 select=','.join(f for f in fields if f in cols)
 result[table]=[dict(r) for r in conn.execute('SELECT '+select+' FROM '+table+' LIMIT 1000')]
 for row in result[table]:
  for field in ('terminal_reason','error_class'):
   if row.get(field) is not None and not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,160}',str(row[field])): row[field]='redacted'
tables={row[0] for row in conn.execute('SELECT name FROM sqlite_master WHERE type="table"')}
if {'turn_ingress_receipts','transcript_entries'} <= tables:
 result['accepted_messages']=[]
 for receipt in conn.execute('SELECT receipt_id,client_request_id,session_id,message_id,task_id,accepted_at FROM turn_ingress_receipts LIMIT 1000'):
  item=dict(receipt)
  entries=conn.execute('SELECT role,content FROM transcript_entries WHERE session_id=? AND message_id=?',(item['session_id'],item['message_id'])).fetchall()
  item['messageMatches']=len(entries)
  if len(entries)==1:
   raw=entries[0]['content'] or ''
   item['role']=entries[0]['role'];item['contentSha256']=hashlib.sha256(raw.encode()).hexdigest()
   try: envelope=json.loads(raw)
   except (ValueError,TypeError): envelope={}
   if not isinstance(envelope,dict): envelope={}
   context=envelope.get('page_context') or {}
   annotations=context.get('annotations',[]) if isinstance(context,dict) else []
   texts=[row.get('text','') for row in annotations if isinstance(row,dict)]
   item['annotationCount']=len(texts)
   item['annotationTextsSha256']=hashlib.sha256(json.dumps(texts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
   attachments=envelope.get('attachments',[])
   item['attachmentCount']=len(attachments) if isinstance(attachments,list) else 0
  result['accepted_messages'].append(item)
conn.close()
print(json.dumps(result))`, join(profile, 'opensquilla', 'state', 'sessions.db'))
}
async function visiblePreviews() {
  return app.evaluate(async ({ webContents }, expectedEntrypoint) => {
    const result = []
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      const owner = contents.getOwnerBrowserWindow()
      const view = owner?.contentView.children.find(item => item.webContents?.id === contents.id)
      if (!view?.getVisible?.() || contents === owner?.webContents) continue
      try {
        const parsed = new URL(contents.getURL())
        if (!['http:', 'https:'].includes(parsed.protocol)) continue
        if (decodeURIComponent(parsed.pathname.split('/').at(-1) || '') !== expectedEntrypoint) continue
        const dom = await contents.executeJavaScript(`(() => ({title:document.title,heading:document.querySelector('h1,[role=heading][aria-level="1"],[role=heading],h2')?.innerText||document.title,text:(document.body?.innerText||'').slice(0,18000),html:document.documentElement.outerHTML,viewport:{width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth},interactive:document.querySelectorAll('button,a,input,select').length,resources:{stylesheets:[...document.querySelectorAll('link[rel=stylesheet]')].map(e=>({path:e.getAttribute('href'),loaded:Boolean(e.sheet)})),scripts:[...document.querySelectorAll('script[src]')].map(e=>({path:e.getAttribute('src')})),inputs:[...document.querySelectorAll('input,select,textarea')].map(e=>({type:e.type,required:e.required,valid:e.validity.valid})),forms:document.forms.length}}))()`, true)
        result.push({ id: contents.id, origin: `${parsed.protocol}//[preview-origin]`, ...dom })
      } catch {}
    }
    return result
  }, filename)
}
async function foreground(stage) {
  const state = await requireDesktopForeground(app, page)
  report.foregroundChecks ??= []
  report.foregroundChecks.push({ stage, elapsedMs: Date.now() - started, ...state })
  return state
}
async function checkpoint(stage) {
  const summary = { stage, elapsedMs: Date.now() - started, durable: await durableState(), previews: [] }
  report.desktopEvents = await desktopEvents()
  summary.artifacts = await exportArtifacts(stage)
  if (!summary.artifacts.complete) report.qualityFindings.push({ stage, code: 'ARTIFACT_EVIDENCE_INCOMPLETE' })
  if (page && !page.isClosed()) {
    if (stage !== 'failure') summary.foreground = await foreground(`checkpoint:${stage}`)
    summary.surfaceEvents = await page.evaluate(() => window.__htmlJourneySurfaceEvents || { events: [], incomplete: true })
    if (summary.surfaceEvents.incomplete) report.evidenceIncomplete = true
    await page.locator('details.thinking-fold[open] > summary').evaluateAll(elements => elements.forEach(element => element.click()))
    await page.screenshot({ path: join(runRoot, `${stage}-control.png`), fullPage: true, timeout: 15000, mask: [page.locator('.thinking-block__body,.thinking-fold__body,[data-testid="reasoning-timeline"]')], maskColor: '#e8e8e8' })
    summary.tabs = await page.getByRole('tab').allTextContents()
  }
  if (app) {
    summary.native = await nativeObservation(app)
    if (summary.foreground) {
      const owner = summary.native.windows.find(row => row.id === summary.foreground.ownerId)
      assert.ok(owner?.visible && owner.focused, 'DESKTOP_FOREGROUND_LOST_DURING_CHECKPOINT')
    }
    if (summary.native.incomplete) report.evidenceIncomplete = true
    for (const preview of await visiblePreviews()) {
      const png = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'), preview.id)
      assert.ok(png.length > 100, 'Visible preview screenshot is empty.')
      await writeFile(join(runRoot, `${stage}-preview-${preview.id}.png`), Buffer.from(png, 'base64'), { mode: 0o600 })
      await writeFile(join(runRoot, `${stage}-preview-${preview.id}.html`), preview.html, { mode: 0o600 })
      const { html, text, ...geometry } = preview
      summary.previews.push({ ...geometry, isSelectedPreview: preview.id === selectedPreviewId, htmlSha256: createHash('sha256').update(html).digest('hex'), text, screenshot: `${stage}-preview-${preview.id}.png` })
    }
    if (['generation', 'annotation', 'followup'].includes(stage)) {
      summary.visualViews = await capturePageVisualEvidence(app, { pageId: selectedPreviewId, outputDirectory: runRoot, stage })
      for (const view of summary.visualViews) {
        if (!view.complete || !view.restorationMatches) {
          report.evidenceIncomplete = true
          report.qualityFindings.push({ stage, viewport: view.label, code: !view.complete ? 'VISUAL_SCROLL_COVERAGE_INCOMPLETE' : 'VISUAL_PAGE_STATE_NOT_RESTORED' })
        }
        if (view.horizontalOverflow) report.qualityFindings.push({ stage, viewport: view.label, code: 'VISUAL_HORIZONTAL_OVERFLOW' })
      }
    }
  }
  const materialFiles = snapshot => {
    const artifacts = snapshot?.artifacts || {}
    const bundles = artifacts.working?.length ? artifacts.working : artifacts.published?.filter(item => item.headDocumentIds?.length) || []
    return Object.fromEntries(bundles.flatMap(bundle => bundle.files.map(item => [`${bundle.entrypoint}:${item.path}`, item.sha256])))
  }
  const previous = report.checkpoints.filter(item => ['generation', 'annotation', 'followup'].includes(item.stage)).at(-1)
  const before = materialFiles(previous)
  const after = materialFiles(summary)
  summary.fileChanges = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter(path => before[path] !== after[path]).map(path => ({ path, beforeSha256: before[path] || null, afterSha256: after[path] || null }))
  report.checkpoints.push(summary)
  if (caseId === 'registration' && !['ready', 'failure'].includes(stage)) {
    const preview = summary.previews.find(item => item.isSelectedPreview) || summary.previews.find(item => item.heading)
    const resources = preview?.resources
    const relative = value => value && !/^(?:[a-z][a-z0-9+.-]*:|\/)/i.test(value)
    if (!resources?.stylesheets.some(item => relative(item.path) && item.loaded)) report.qualityFindings.push({ stage, code: 'REGISTRATION_RELATIVE_STYLESHEET_MISSING_OR_UNLOADED' })
    if (!resources?.scripts.some(item => relative(item.path))) report.qualityFindings.push({ stage, code: 'REGISTRATION_RELATIVE_SCRIPT_MISSING' })
    if (stage === 'followup') report.businessChecks.push({ stage, name: 'independent-css-file-changed', passed: summary.fileChanges.some(item => item.path.endsWith('.css') && item.beforeSha256 && item.afterSha256) })
  }
  await persist()
  return summary
}
async function exportArtifacts(stage) {
  const program = await readFile(new URL('./live_html_evidence.py', import.meta.url), 'utf8')
  try { return await runPython(program, profile, join(runRoot, 'artifacts', stage), runRoot) }
  catch (error) {
    const rescue = await runPython(program, profile, join(runRoot, 'artifacts', `${stage}-raw-rescue`), runRoot, '--rescue')
    return { complete: false, errors: [safeError(error)], rescue }
  }
}
async function ledgerSnapshot() {
  const path = requestLogPath || ledgerPath
  if (!path) return null
  return runPython(`import json,sqlite3,sys
from pathlib import Path
connection=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
connection.row_factory=sqlite3.Row
state=dict(connection.execute('SELECT key,value FROM state'))
functional=sys.argv[4]=='functional'
if functional and (state.get('mode')!='functional' or state.get('schema_version')!='1'): raise ValueError('FUNCTIONAL_REQUEST_LOG_REQUIRED')
if not functional and state.get('mode')=='functional': raise ValueError('BUDGET_LEDGER_REQUIRED')
phase=connection.execute("SELECT value FROM state WHERE key='phase'").fetchone()
fields=(['id','variant','case_id','model','started','ended','status','reason','response_headers_at','http_status','first_chunk_at','first_event_at','request_bytes','response_bytes'] if functional else ['id','bucket','variant','case_id','model','started','ended','reserved_nanos','charged_nanos','status','reason','input_tokens','output_tokens','response_headers_at','http_status','first_event_at'])
columns={row[1] for row in connection.execute('PRAGMA table_info(requests)')}
requests=[dict(row) for row in connection.execute('SELECT '+','.join(field for field in fields if field in columns)+' FROM requests WHERE variant=? AND case_id=?',(sys.argv[2],sys.argv[3]))]
pending=connection.execute('SELECT COUNT(*) FROM requests WHERE status=?',('in_flight' if functional else 'reserved',)).fetchone()[0]
print(json.dumps({'mode':'functional' if functional else 'budget','phase':json.loads(phase[0]) if phase else {},'pendingRequests':pending,'firstEventDefinition':'first upstream complete SSE data event or JSON object; not visible-token TTFT','requests':requests}))`, path, variant, ledgerCase, requestLogPath ? 'functional' : 'budget')
}
async function drainRelay() {
  if (!ledgerPath && !requestLogPath) return
  report.phaseBeforeDrain = report.phase
  report.phase = 'draining-provider-requests'
  const requestDeadline = (report.configuration.requestTimeoutSeconds || 120) * 1000
  const terminationGrace = (report.configuration.webuiIdleGraceSeconds || 630) * 1000
  const until = Date.now() + requestDeadline + terminationGrace
  let idleSince = null
  while (true) {
    const state = await ledgerSnapshot()
    report.relay = state
    await persist()
    if (state.pendingRequests === 0) {
      idleSince ??= Date.now()
      if (Date.now() - idleSince >= 2000) return
    } else idleSince = null
    if (report.interruption || Date.now() >= until) {
      report.errors.push({ kind: 'relay-drain', code: report.interruption ? 'EXPLICIT_INTERRUPTION_WITH_PENDING_REQUESTS' : 'RELAY_DRAIN_EXPIRED', pendingRequests: state.pendingRequests, waitBudgetMs: requestDeadline + terminationGrace })
      report.status = 'evidence_failed'
      process.exitCode = 1
      return
    }
    await delay(500)
  }
}
async function cleanupPrivateRuntime() {
  const result = { removed: [], failures: [] }
  if (report.status === 'evidence_failed' || !report.finalArtifacts?.complete) {
    const recovery = join(runRoot, 'artifacts', 'preserved-originals')
    await mkdir(recovery, { recursive: true, mode: 0o700 })
    const sources = new Map([['media', join(profile, 'opensquilla', 'media')]])
    // Working resources were rescued by their binding/manifest paths. Their root may
    // be the agent workspace, which also contains private runtime documents.
    report.boundedWorkingRescue = report.finalArtifacts?.rescue || null
    for (const [name, source] of sources) {
      try {
        const physical = await realpath(source)
        const inside = relative(await realpath(runRoot), physical)
        assert.ok(inside && !isAbsolute(inside) && !inside.split(sep).includes('..'), 'PRESERVE_MATERIAL_OUTSIDE_ATTEMPT')
        await rename(source, join(recovery, name))
      } catch (error) {
        if (error.code === 'ENOENT') continue
        result.failures.push({ kind: 'preserve-originals', ...safeError(error) })
      }
    }
    if (result.failures.length) {
      report.cleanup = result
      report.outcomeBeforeCleanup = report.status
      report.status = 'cleanup_failed'
      process.exitCode = 1
      return
    }
    report.preservedOriginals = 'artifacts/preserved-originals'
  }
  for (const directory of [profile, stateRoot, shellRoot]) {
    try {
      assert.equal(resolve(directory, '..'), resolve(runRoot), 'CLEANUP_PATH_OUTSIDE_ATTEMPT')
      await rm(directory, { recursive: true, force: true })
      result.removed.push(directory === profile ? 'electron-user-data' : directory === stateRoot ? 'user-state' : 'source-shell')
    } catch (error) { result.failures.push(safeError(error)) }
  }
  report.cleanup = result
  if (result.failures.length) { report.outcomeBeforeCleanup = report.status; report.status = 'cleanup_failed'; process.exitCode = 1 }
}
async function captureMobilePreview(stage) {
  const preview = (await visiblePreviews()).find(item => item.id === selectedPreviewId) || (await visiblePreviews()).find(item => item.heading)
  assert.ok(preview, 'MOBILE_PREVIEW_MISSING')
  const result = await app.evaluate(async ({ webContents }, id) => {
    const contents = webContents.fromId(id)
    const attachedHere = !contents.debugger.isAttached()
    if (attachedHere) contents.debugger.attach('1.3')
    try {
      await contents.debugger.sendCommand('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false })
      await contents.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
      const geometry = await contents.executeJavaScript(`(() => {
        const normalize=value=>String(value||'').replace(/\\s+/g,' ').trim();
        const name=e=>normalize(e.getAttribute('aria-label')||(e.getAttribute('aria-labelledby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' ')||[...(e.labels||[])].map(l=>l.innerText).join(' ')||e.getAttribute('placeholder')||(['INPUT','TEXTAREA','SELECT'].includes(e.tagName)?'':e.innerText)||e.value);
        return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,controls:[...document.querySelectorAll('input,select,textarea,button,[role=button]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0}).map(e=>{const r=e.getBoundingClientRect();return {name:name(e),left:r.left,top:r.top+scrollY,bottom:r.bottom+scrollY,width:r.width}})};
      })()`)
      const png = (await contents.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64')
      return { ...geometry, png }
    } finally {
      await contents.debugger.sendCommand('Emulation.clearDeviceMetricsOverride')
      if (attachedHere) contents.debugger.detach()
    }
  }, preview.id)
  await writeFile(join(runRoot, `${stage}-mobile.png`), Buffer.from(result.png, 'base64'), { mode: 0o600 })
  const { png, ...geometry } = result
  report.checkpoints.at(-1).mobile = { ...geometry, previewId: preview.id, screenshot: `${stage}-mobile.png` }
  await persist()
  if (result.scrollWidth > result.width + 1) report.qualityFindings.push({ stage, code: 'MOBILE_HORIZONTAL_OVERFLOW' })
  if (caseId === 'registration') {
    const controls = ['姓名', '邮箱', '参加场次', '提交报名'].map(name => result.controls.find(item => item.name.includes(name)))
    const ordered = controls.every(Boolean) && controls.slice(1).every((item, index) => item.top >= controls[index].bottom)
    report.businessChecks.push({ stage, name: 'mobile-form-order', passed: ordered })
  }
  await persist()
}
async function sendTurn(name, prompt) {
  await foreground(`send:${name}`)
  const ledger = await ledgerSnapshot()
  if (ledger) {
    assert.equal(ledger.phase.variant, variant, 'RELAY_PHASE_VARIANT_MISMATCH')
    assert.equal(ledger.phase.case_id, ledgerCase, 'RELAY_PHASE_CASE_MISMATCH')
  }
  activeTurn = { name, startedAtMs: Date.now() - started }
  report.phase = name
  report.turns.push(activeTurn)
  await page.locator('.chat-textarea').fill(prompt)
  const button = page.locator('.chat-send-btn.btn--primary')
  await waitFor(async () => await button.isVisible() && !await button.isDisabled() && (await button.getAttribute('class')).includes('is-ready'), 'composer ready', 30000)
  await button.click()
  await waitFor(() => activeTurn.taskId, 'durable chat acceptance', 60000)
  const duration = report.configuration.runtimeTimeoutSeconds
  const grace = report.configuration.webuiIdleGraceSeconds
  const waitBudget = duration > 0 ? (duration + grace + 30) * 1000 : 0
  let lastProbe = 0
  await waitFor(async () => {
    if (Date.now() - lastProbe < 2000) return false
    lastProbe = Date.now()
    const state = await durableState()
    const task = state.agent_tasks?.find(item => item.task_id === activeTurn.taskId)
    if (!task || ['queued', 'running', 'waiting'].includes(task.status)) { await persist(); return false }
    activeTurn.terminalStatus = task.status
    activeTurn.terminalReason = task.terminal_reason || null
    activeTurn.errorClass = task.error_class || null
    activeTurn.finishedAtMs = Date.now() - started
    activeTurn.durationMs = activeTurn.finishedAtMs - activeTurn.startedAtMs
    report.phase = task.status === 'succeeded' ? 'waiting-composer-release' : name
    await persist()
    if (task.status !== 'succeeded') {
      const error = new Error('TURN_TERMINATED')
      error.diagnostic = { turn: name, terminalStatus: task.status, terminalReason: activeTurn.terminalReason, providerErrorClass: activeTurn.errorClass }
      throw error
    }
    return true
  }, `${name} product turn terminal state`, waitBudget)
  let lastComposerPersist = 0
  await waitFor(async () => {
    activeTurn.composer = await composerStatus()
    const released = await button.count() === 1 && await button.isVisible() && !await button.isDisabled()
    if (released || Date.now() - lastComposerPersist >= 2000) {
      lastComposerPersist = Date.now()
      await persist()
    }
    return released
  }, 'composer released', Math.max(30000, grace * 1000))
  activeTurn.composerReleasedAtMs = Date.now() - started
  activeTurn.composerReleaseDelayMs = activeTurn.composerReleasedAtMs - activeTurn.finishedAtMs
  report.phase = name
  await persist()
  activeTurn = null
}
async function openPreview() {
  await foreground('open-preview')
  const visible = await visiblePreviews()
  const existing = visible.find(item => item.heading)
  if (existing) { selectedPreviewId = existing.id; return }
  const card = page.locator('.msg-artifact-chip').filter({ hasText: filename }).last()
  await card.waitFor({ state: 'visible', timeout: 30000 })
  await card.locator('.msg-artifact-body').click()
  await waitFor(async () => (await visiblePreviews()).some(item => item.heading), 'generated native preview', 30000)
  selectedPreviewId = (await visiblePreviews()).find(item => item.heading).id
}
async function annotateHeading() {
  await foreground('annotation-input')
  const button = page.getByRole('button', { name: /Annotate preview|批注预览/ })
  await button.waitFor({ state: 'visible', timeout: 30000 })
  if (await button.getAttribute('aria-pressed') !== 'true') await button.click()
  const preview = (await visiblePreviews()).find(item => item.id === selectedPreviewId)
  assert.ok(preview, 'No exact visible preview for selection.')
  const selectionPoint = await businessDriver.point(cases[caseId].selection)
  await app.evaluate(async ({ webContents }, request) => {
    const contents = webContents.fromId(request.id)
    const point = { x: request.point.x, y: request.point.y }
    contents.focus()
    const attachedHere = !contents.debugger.isAttached()
    if (attachedHere) contents.debugger.attach('1.3')
    try {
      for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type, ...point, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: 1 })
    } finally { if (attachedHere) contents.debugger.detach() }
  }, { id: preview.id, point: selectionPoint })
  const body = prompts.selection
  await waitFor(async () => app.evaluate(async ({ webContents }, text) => {
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      const owner = contents.getOwnerBrowserWindow()
      const view = owner?.contentView.children.find(item => item.webContents?.id === contents.id)
      if (!view?.getVisible?.()) continue
      try {
        if (!await contents.executeJavaScript("document.activeElement?.id==='annotation-body'", true)) continue
        contents.focus()
        contents.insertText(text)
        const actual = await contents.executeJavaScript("document.getElementById('annotation-body').value", true)
        if (actual !== text) throw new Error('Annotation keyboard input mismatch.')
        contents.sendInputEvent({ type: 'keyDown', keyCode: 'Enter' })
        contents.sendInputEvent({ type: 'keyUp', keyCode: 'Enter' })
        return true
      } catch (error) { if (String(error).includes('mismatch')) throw error }
    }
    return false
  }, body), 'native annotation editor', 30000)
  await page.locator('.chat-prompt-annotation-chip').filter({ hasText: prompts.selection.slice(0, 14) }).waitFor({ state: 'visible', timeout: 30000 })
}

async function selectReviewedNonvisionModel(controlPage, model) {
  const raw = await readFile(reviewedCatalogPath)
  const catalog = JSON.parse(raw)
  const entry = catalog.data?.find(item => item.id === model)
  assert.ok(entry && entry.supports_vision === false, 'REVIEWED_TEXT_MODEL_REQUIRED')
  const settings = () => runPython(`import json,sys,tomllib
from pathlib import Path
r=tomllib.loads(Path(sys.argv[1]).read_text())
print(json.dumps({'model':r.get('llm',{}).get('model'),'provider':r.get('llm',{}).get('provider'),'router':r.get('squilla_router',{}),'ensemble':r.get('ensemble',{})}))`, join(profile, 'opensquilla', 'config.toml'))
  const before = await settings()
  report.execution.modelAndRouterOverrides = true
  report.execution.modelOverrideReason = 'Required supplemental nonvisual capability branch, using the normal settings and session controls.'
  report.execution.overrideScope = 'Fixed model and current session mode only; global router and ensemble must remain identical.'
  await persist()
  await controlPage.locator('.sidebar-foot [data-icon="settings"]').click()
  await controlPage.locator('#settings-rail-modelStrategy').click()
  const input = controlPage.locator('[data-testid="setup-model-strategy-fixed-model"] input')
  await input.fill(model)
  await input.press('Tab')
  await controlPage.locator('.settings-dirtybar .btn--primary').click()
  await waitFor(async () => (await settings()).model === model && !await controlPage.locator('.settings-dirtybar').count(), 'fixed model saved through settings', 60000)
  const after = await settings()
  assert.equal(after.provider, before.provider, 'CAPABILITY_SCENARIO_PROVIDER_CHANGED')
  assert.deepEqual(after.router, before.router, 'CAPABILITY_SCENARIO_GLOBAL_ROUTER_CHANGED')
  assert.deepEqual(after.ensemble, before.ensemble, 'CAPABILITY_SCENARIO_ENSEMBLE_CHANGED')
  await controlPage.locator('.settings-modal__close').click()
  await controlPage.locator('.chat-model-routing-btn').click()
  await controlPage.locator('.composer-model-routing__option--off').click()
  await waitFor(async () => (await controlPage.locator('.chat-model-routing-btn').getAttribute('class')).includes('chat-model-routing-btn--off'), 'session fixed model mode', 30000)
  await controlPage.locator('.chat-textarea').click()
  return { selected: true, model, supportsVision: false, source: 'reviewed-official-catalog', catalogSha256: createHash('sha256').update(raw).digest('hex'), before, after, sessionMode: 'off', selectionMethod: 'settings-fixed-model-and-composer-session-mode', scope: 'supplemental nonvisual capability branch only' }
}

try {
  report.phase = 'source-validation'
  await persist()
  await preflightSubject()
  report.harness = {}
  for (const name of ['live-html-journey.mjs', 'live-html-journey-business.mjs', 'live-html-foreground.mjs', 'live-html-native-observation.mjs', 'live-html-visual-evidence.mjs', 'live-html-supplemental-client.mjs', 'live-html-supplemental-scenarios.mjs', 'live_html_evidence.py']) report.harness[name] = createHash('sha256').update(await readFile(new URL(name, import.meta.url))).digest('hex')
  if (ledgerPath || requestLogPath) {
    report.relay = await ledgerSnapshot()
    assert.equal(report.relay.phase.variant, variant, 'RELAY_PHASE_VARIANT_MISMATCH')
    assert.equal(report.relay.phase.case_id, ledgerCase, 'RELAY_PHASE_CASE_MISMATCH')
  }
  const mainSource = await readFile(join(desktopRoot, 'src/main.ts'), 'utf8')
  const localRenderer = mainSource.includes('DESKTOP_RENDERER_URL')
  report.source.rendererMode = localRenderer ? 'local-desktop' : 'gateway-control-ui'
  const uiArtifact = localRenderer ? 'opensquilla-webui/dist' : 'src/opensquilla/gateway/static/dist'
  const sourceFiles = ['desktop/electron/dist/main.js', 'desktop/electron/dist/preload.cjs', `${uiArtifact}/index.html`,
    ...(localRenderer ? [`${uiArtifact}/desktop.html`, `${uiArtifact}/webui-artifact-manifest.json`] : [])]
  for (const path of sourceFiles) report.source[path] = createHash('sha256').update(await readFile(join(sourceRoot, path))).digest('hex')
  report.source.gitHead = (await exec('git', ['rev-parse', 'HEAD'], { cwd: sourceRoot, env: cleanEnv })).stdout.trim()
  report.source.workingDiffSha256 = createHash('sha256').update((await exec('git', ['diff', '--binary', 'HEAD'], { cwd: sourceRoot, env: cleanEnv, maxBuffer: 30 * 1024 * 1024 })).stdout).digest('hex')
  report.source.manifestSha256 = await sourceManifest()
  report.phase = 'isolated-profile'
  await persist()
  if (!packagedExecutable) {
    await mkdir(join(shellRoot, 'src'), { recursive: true })
    for (const name of ['dist', 'assets', 'package.json']) await cp(join(desktopRoot, name), join(shellRoot, name), { recursive: true })
    await cp(join(desktopRoot, 'src', 'boot.html'), join(shellRoot, 'src', 'boot.html'))
    await symlink(join(desktopRoot, 'node_modules'), join(shellRoot, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir')
  }
  await mkdir(profile, { recursive: true, mode: 0o700 })
  await mkdir(stateRoot, { recursive: true, mode: 0o700 })
  await writeFile(join(profile, 'desktop-credential.json'), JSON.stringify({ provider: 'tokenrhythm', baseUrl: 'https://tokenrhythm.studio/v1', apiKeyEnv: 'TOKENRHYTHM_API_KEY', encryptedApiKey: Buffer.from(relay.client_key).toString('base64'), encryption: 'plain' }), { mode: 0o600 })
  gatewayPort = await reservePort()
  const env = { ...cleanEnv, OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain', OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1', OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(gatewayPort), OPENSQUILLA_USER_STATE_DIR: stateRoot, OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1' }
  if (packagedExecutable) {
    for (const key of ['PYTHONPATH', 'PYTHONHOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE']) delete env[key]
    Object.assign(env, { OPENSQUILLA_TRUST_ENV: '1', HTTPS_PROXY: proxy.proxy_url, HTTP_PROXY: proxy.proxy_url, SSL_CERT_FILE: proxy.ca_file, NO_PROXY: '127.0.0.1,localhost,::1' })
    report.execution.instrumentationDifference = 'Process-scoped HTTPS test proxy; normal product privacy logic omits the install-id header.'
  } else Object.assign(env, { PYTHONPATH: [transportDir, join(sourceRoot, 'src')].join(delimiter), OPENSQUILLA_LIVE_TRANSPORT: '1', OPENSQUILLA_LIVE_TRANSPORT_DIR: transportDir, OPENSQUILLA_LIVE_RELAY_URL: relayUrl.href.replace(/\/$/, ''), OPENSQUILLA_LIVE_RELAY_CLIENT_KEY: relay.client_key, OPENSQUILLA_DESKTOP_REPO_ROOT: sourceRoot })
  const executablePath = packagedExecutable ? await realpath(packagedExecutable) : createRequire(join(desktopRoot, 'package.json'))('electron')
  report.phase = 'electron-startup'
  await persist()
  app = await electron.launch({ executablePath, args: ['--use-mock-keychain', `--user-data-dir=${profile}`, ...packagedExecutable ? [] : [shellRoot]], env, timeout: 180000 })
  if (packagedExecutable) {
    report.package = await app.evaluate(({ app }) => ({ isPackaged: app.isPackaged, appPath: app.getAppPath(), resourcesPath: process.resourcesPath, executable: process.execPath }))
    assert.equal(report.package.isPackaged, true, 'ACTUAL_PACKAGED_APP_REQUIRED')
    assert.equal(await realpath(report.package.executable), executablePath, 'PACKAGED_EXECUTABLE_MISMATCH')
    report.package.executableSha256 = createHash('sha256').update(await readFile(executablePath)).digest('hex')
    report.package.appAsarSha256 = createHash('sha256').update(await readFile(join(report.package.resourcesPath, 'app.asar'))).digest('hex')
    const binaryName = process.platform === 'win32' ? 'opensquilla-gateway.exe' : 'opensquilla-gateway'
    const gatewayDirectory = join(report.package.resourcesPath, 'runtime', 'gateway')
    let gatewayBinary = join(gatewayDirectory, 'opensquilla-gateway', binaryName)
    try { await readFile(gatewayBinary) } catch (error) {
      if (error.code !== 'ENOENT') throw error
      gatewayBinary = join(gatewayDirectory, binaryName)
    }
    report.package.gatewayExecutable = await realpath(gatewayBinary)
    report.package.gatewaySha256 = createHash('sha256').update(await readFile(gatewayBinary)).digest('hex')
    if (localRenderer) {
      report.package.controlUi = {}
      for (const entry of ['index.html', 'desktop.html', 'webui-artifact-manifest.json']) {
        const sha256 = createHash('sha256').update(await readFile(join(gatewayDirectory, 'control-ui-dist', entry))).digest('hex')
        report.package.controlUi[entry] = sha256
        assert.equal(sha256, report.source[`${uiArtifact}/${entry}`], 'PACKAGED_UI_SOURCE_ARTIFACT_MISMATCH')
      }
    }
  }
  await installNativeObservation(app)
  app.process().stderr?.on('data', chunk => { stderrBytesDiscarded += chunk.length })
  app.process().once('exit', (code, signal) => {
    report.desktopExit = { code, signal, elapsedMs: Date.now() - started, expected: closing }
    if (!closing) terminalError = new Error('DESKTOP_PROCESS_EXITED')
  })
  page = await app.firstWindow({ timeout: 180000 })
  page.on('websocket', socket => {
    const id = ++websocketSequence
    observeConnection('websocket.opened', id)
    socket.on('framesent', frame => observe(frame, 'sent', id))
    socket.on('framereceived', frame => observe(frame, 'received', id))
    socket.on('close', () => observeConnection('websocket.closed', id))
    socket.on('socketerror', () => observeConnection('websocket.error', id))
  })
  page.on('console', observeRendererDiagnostic)
  page.on('pageerror', error => report.errors.push({ kind: 'renderer', ...safeError(error) }))
  await app.evaluate(({ app, BrowserWindow }) => { if (process.platform === 'darwin') app.focus({ steal: true }); const window=BrowserWindow.getAllWindows()[0];window.setSize(1440,900);window.show();window.focus() })
  report.phase = 'gateway-startup'
  await persist()
  await waitFor(() => {
    const url = new URL(page.url())
    return localRenderer
      ? url.protocol === 'opensquilla-app:' && url.hostname === 'desktop' && /^\/chat(?:\/new)?\/?$/.test(url.pathname)
      : ['http:', 'https:'].includes(url.protocol) && url.pathname.startsWith('/control/chat')
  }, 'owned Desktop renderer', 180000)
  await page.locator('.conn-pill.connected').waitFor({ state: 'visible', timeout: 180000 })
  report.renderer = await page.evaluate(async local => {
    const state = { events: [], incomplete: false }
    window.__htmlJourneySurfaceEvents = state
    window.opensquillaDesktop.onWorkbenchSurfaceEvent(event => {
      if (state.events.length >= 10000) { state.incomplete = true; return }
      const detail = event.detail || {}, selection = detail.selection || {}
      state.events.push({ at: Date.now(), version: event.version, surfaceId: event.surfaceId, type: event.type,
        surfaceInstanceId: detail.surfaceInstanceId, code: typeof detail.code === 'string' && /^[A-Z_]{1,80}$/.test(detail.code) ? detail.code : undefined,
        selectionPresence: Object.fromEntries(['selectionId', 'tagName', 'elementPath', 'targetRef', 'locatorHint', 'elementProofSha256'].map(key => [key, Boolean(selection[key])])) })
    })
    if (!local) return { mode: 'gateway-control-ui' }
    const connection = await window.opensquillaDesktop.getGatewayConnection()
    return { mode: 'local-desktop', gatewayStatus: connection.status, schemaVersion: connection.schemaVersion,
      instanceId: connection.instanceId, revision: connection.revision, hasAuthToken: Boolean(connection.authToken) }
  }, localRenderer)
  if (localRenderer) assert.equal(report.renderer.gatewayStatus, 'ready', 'DESKTOP_GATEWAY_NOT_READY')
  if (packagedExecutable) {
    const invocation = await page.evaluate(() => window.opensquillaDesktop.getCliInvocation())
    assert.equal(invocation.mode, 'bundled', 'PACKAGED_GATEWAY_RUNTIME_REQUIRED')
    assert.ok(JSON.stringify(invocation).includes(report.package.resourcesPath), 'PACKAGED_GATEWAY_RUNTIME_REQUIRED')
    report.package.gatewayInvocation = invocation
  }
  report.phase = 'configuration-evidence'
  report.configuration = await runPython(`import json,sys,tomllib
from pathlib import Path
from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.config import GatewayConfig
raw=tomllib.loads(Path(sys.argv[1]).read_text());defaults=GatewayConfig();agent=AgentConfig()
value=raw.get('agent_runtime_timeout_seconds',raw.get('llm_timeout_seconds'))
print(json.dumps({'runtimeTimeoutSeconds':agent.timeout if value is None else value,'requestTimeoutSeconds':raw.get('agent_request_timeout_seconds') or raw.get('llm_request_timeout_seconds',defaults.llm_request_timeout_seconds),'webuiIdleGraceSeconds':raw.get('webui_stream_idle_grace_seconds',defaults.webui_stream_idle_grace_seconds),'provider':raw.get('llm',{}).get('provider'),'model':raw.get('llm',{}).get('model'),'router':raw.get('squilla_router',{}),'ensemble':raw.get('ensemble',{})}))`, join(profile, 'opensquilla', 'config.toml'))
  report.phase = 'ready'
  await checkpoint('ready')
  if (!preflightOnly && supplemental) {
    let fixtureUrl = ''
    if (caseId === 'same-url-memory') {
      const html = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>独立页面计数</title><style>body{font:20px sans-serif;padding:32px;min-height:1800px}button,input{font:inherit;margin:12px}</style><h1>独立页面计数</h1><p>每个页面独立保存当前浏览状态。</p><label>页面备注<input aria-label="页面备注"></label><button onclick="document.querySelector(\'[role=status]\').textContent=String(++window.syntheticCount)">增加计数</button><p role="status">0</p><script>window.syntheticCount=0;window.syntheticInstanceId=crypto.randomUUID();</script></html>'
      fixtureServer = createHttpServer((request, response) => {
        if (request.method !== 'GET' || request.url !== '/independent-pages.html') { response.writeHead(404); response.end(); return }
        response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' })
        response.end(html)
      })
      await new Promise((done, reject) => { fixtureServer.once('error', reject); fixtureServer.listen(0, '127.0.0.1', done) })
      fixtureUrl = `http://127.0.0.1:${fixtureServer.address().port}/independent-pages.html`
      report.fixture = { url: fixtureUrl, sha256: createHash('sha256').update(html).digest('hex'), providerIndependent: true }
      report.prompts = supplementalPromptHash(caseId, fixtureUrl).prompts
      report.promptsSha256 = supplementalPromptHash(caseId, fixtureUrl).sha256
    }
    supplementalClient = await createSupplementalClient(app, page, {
      report, directory: runRoot, persist, readState: durableState, exportArtifacts, configuration: report.configuration,
      interrupted: () => Boolean(terminalError), fixtureUrl,
      reviewedNonvisionModel, selectReviewedNonvisionModel,
      checkLedger: async () => {
        const ledger = await ledgerSnapshot()
        assert.equal(ledger.phase.variant, variant, 'RELAY_PHASE_VARIANT_MISMATCH')
        assert.equal(ledger.phase.case_id, ledgerCase, 'RELAY_PHASE_CASE_MISMATCH')
      },
    })
    supplementalClient = withForegroundActions(supplementalClient, ['capture', 'send', 'annotate', 'openArtifact', 'activatePage', 'clickInPage', 'preparePageState', 'reenter', 'restoreOriginalVersion', 'verifyButtonInteraction', 'attachFile', 'fillComposer', 'selectReviewedNonvisionModel'], name => foreground(`supplemental:${name}`))
    await runSupplementalScenario(caseId, supplementalClient)
  } else if (!preflightOnly) {
    businessDriver = createBusinessDriver(app, () => selectedPreviewId, async label => {
      const stage = report.phase
      const png = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG().toString('base64'), selectedPreviewId)
      await writeFile(join(runRoot, `${stage}-${label}.png`), Buffer.from(png, 'base64'), { mode: 0o600 })
    })
    businessDriver = withForegroundActions(businessDriver, ['point', 'click', 'fill', 'choose'], name => foreground(`business:${report.phase}:${name}`))
    await sendTurn('generation', prompts.generation)
    await openPreview()
    report.phase = 'generation'
    await verifyBusinessCase({ caseId, stage: 'generation', driver: businessDriver, record: item => report.businessChecks.push({ stage: 'generation', ...item }) })
    await checkpoint('generation')
    await captureMobilePreview('generation')
    report.phase = 'annotation-input'
    await persist()
    await annotateHeading()
    await sendTurn('annotation', prompts.annotation)
    report.phase = 'annotation'
    await waitFor(async () => (await visiblePreviews()).some(item => item.id === selectedPreviewId), 'annotation exact preview remains available', 30000)
    await verifyBusinessCase({ caseId, stage: 'annotation', driver: businessDriver, record: item => report.businessChecks.push({ stage: 'annotation', ...item }) })
    await checkpoint('annotation')
    await captureMobilePreview('annotation')
    await sendTurn('followup', prompts.followup)
    report.phase = 'followup'
    await waitFor(async () => (await visiblePreviews()).some(item => item.id === selectedPreviewId), 'followup exact preview remains available', 30000)
    await verifyBusinessCase({ caseId, stage: 'followup', driver: businessDriver, record: item => report.businessChecks.push({ stage: 'followup', ...item }) })
    await checkpoint('followup')
    await captureMobilePreview('followup')
  }
  report.status = report.evidenceIncomplete ? 'evidence_incomplete' : report.qualityFindings.length || [...report.businessChecks, ...report.checks].some(item => !item.passed) ? 'quality_failed' : 'passed'
  if (report.status !== 'passed') process.exitCode = 1
} catch (error) {
  report.status = 'failed'
  report.failure = { ...safeError(error), stage: activeTurn?.name || report.phase || 'startup', elapsedMs: Date.now() - started }
  if (!supplemental && !preflightOnly) report.skippedTurns = ['generation', 'annotation', 'followup']
    .filter(name => !report.turns.some(turn => turn.name === name))
    .map(name => ({ name, reason: 'prerequisite-failed', blockedStage: report.failure.stage }))
  terminalError = null
  try { await checkpoint('failure') } catch (captureError) { report.errors.push({ kind: 'capture', ...safeError(captureError) }) }
  process.exitCode = 1
} finally {
  report.finishedAt = new Date().toISOString()
  report.elapsedMs = Date.now() - started
  await persist()
  report.stderrBytesDiscarded = stderrBytesDiscarded
  await persist()
  try { await drainRelay() }
  catch (error) { report.errors.push({ kind: 'relay-drain', ...safeError(error) }); report.status = 'evidence_failed'; process.exitCode = 1 }
  closing = true
  if (app) { try { await app.close() } catch (error) { report.errors.push({ kind: 'shutdown', ...safeError(error) }); await persist() } }
  if (fixtureServer) await new Promise(done => fixtureServer.close(done))
  try {
    report.finalDurableState = await durableState()
    report.finalArtifacts = await exportArtifacts('final')
    if (!report.finalArtifacts.complete) { report.status = 'evidence_failed'; process.exitCode = 1 }
  } catch (error) { report.errors.push({ kind: 'final-evidence', ...safeError(error) }); report.status = 'evidence_failed'; process.exitCode = 1 }
  await cleanupPrivateRuntime()
  report.finishedAt = new Date().toISOString()
  report.elapsedMs = Date.now() - started
  report.stderrBytesDiscarded = stderrBytesDiscarded
  await persist()
  console.log(JSON.stringify({ status: report.status, caseId, repetition, evidenceDirectory: runRoot, preflightOnly }))
}
