import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'
import test from 'node:test'
import vm from 'node:vm'

// Evaluate the actual helpers without the journey module's Electron startup.
const source = await readFile(new URL('./live-html-journey.mjs', import.meta.url), 'utf8')
function section(from, until) {
  const start = source.indexOf(from)
  const end = source.indexOf(until, start + from.length)
  assert.ok(start >= 0 && end > start, `Missing driver helper: ${from}`)
  return source.slice(start, end)
}
function fixture(extra = {}) {
  const context = vm.createContext({
    assert, createHash, realpath, resolve, isAbsolute, Date,
    report: { startedAt: '2026-01-01T00:00:00.000Z', events: [], streamSummaries: [], turns: [], configuration: { runtimeTimeoutSeconds: 600, webuiIdleGraceSeconds: 630 } },
    started: Date.now() - 1000, activeTurn: null, terminalError: null,
    rpcMethods: new Map(), toolDeliveries: new Map(), streamSummaryByKey: new Map(),
    ...extra,
  })
  vm.runInContext(section('function evidenceHash(', 'async function runPython('), context)
  return context
}
const wire = (context, value, direction = 'received', connection = 1) => context.observe({ payload: JSON.stringify(value) }, direction, connection)
const clone = value => JSON.parse(JSON.stringify(value))

for (const [label, entry, valid] of [
  ['explicit official false', { id: 'text-model', supports_vision: false }, true],
  ['vision true', { id: 'text-model', supports_vision: true }, false],
  ['unknown capability', { id: 'text-model' }, false],
  ['null capability', { id: 'text-model', supports_vision: null }, false],
  ['string false', { id: 'text-model', supports_vision: 'false' }, false],
  ['legacy projected fields only', { id: 'text-model', supportsVision: false, capabilities: { vision: false } }, false],
  ['different model', { id: 'other-model', supports_vision: false }, false],
]) test(`reviewed nonvisual model selection: ${label}`, async () => {
  const catalog = JSON.stringify({ object: 'list', data: [entry] })
  let settingsRead = false
  const f = fixture({ reviewedCatalogPath: 'synthetic-catalog.json', profile: 'synthetic-profile', join,
    readFile: async () => catalog,
    runPython: async () => { settingsRead = true; throw new Error('SYNTHETIC_SETTINGS_REACHED') },
  })
  vm.runInContext(section('async function selectReviewedNonvisionModel(', '\ntry {'), f)
  await assert.rejects(f.selectReviewedNonvisionModel({}, 'text-model'),
    new RegExp(valid ? 'SYNTHETIC_SETTINGS_REACHED' : 'REVIEWED_TEXT_MODEL_REQUIRED'))
  assert.equal(settingsRead, valid, 'unknown or visual capability must fail before changing settings')
  assert.equal(catalog, JSON.stringify({ object: 'list', data: [entry] }))
})

test('RPC correlation retains connection and session identity without session text', () => {
  const f = fixture()
  wire(f, { type: 'req', id: 'same-id', method: 'sessions.subscribe', params: { key: 'private-session-one' } }, 'sent', 1)
  wire(f, { type: 'req', id: 'same-id', method: 'sessions.unsubscribe', params: { key: 'private-session-two' } }, 'sent', 2)
  wire(f, { type: 'res', id: 'same-id', ok: false, error: { code: 'SESSION_NOT_FOUND', message: 'private server detail' } }, 'received', 1)
  wire(f, { type: 'res', id: 'same-id', ok: true }, 'received', 2)
  const [first, second, rejected, closed] = f.report.events
  assert.equal(rejected.method, 'sessions.subscribe')
  assert.equal(rejected.sessionHash, first.sessionHash)
  assert.equal(closed.method, 'sessions.unsubscribe')
  assert.equal(closed.sessionHash, second.sessionHash)
  assert.notEqual(first.sessionHash, second.sessionHash)
  assert.equal(rejected.errorCode, 'SESSION_NOT_FOUND')
  wire(f, { type: 'event', event: 'session.event.done', payload: { key: 'private-session-one', data: { task_id: 'task-1' } } })
  assert.equal(f.report.events.at(-1).sessionHash, first.sessionHash)
  assert.doesNotMatch(JSON.stringify(f.report), /private-session|private server detail/)
})

test('tool hashes distinguish repeat delivery from another actual call without storing inputs', () => {
  const f = fixture()
  const event = (toolId, args) => ({ type: 'event', event: 'session.event.tool_result', payload: { data: { task_id: 'task-1', tool_use_id: toolId, tool_name: 'read_file', arguments: args, result: 'synthetic-secret-result' } } })
  wire(f, event('tool-1', { path: 'synthetic-secret-path', options: { b: 2, a: 1 } }))
  wire(f, event('tool-1', { options: { a: 1, b: 2 }, path: 'synthetic-secret-path' }))
  wire(f, event('tool-2', { path: 'synthetic-secret-path', options: { b: 2, a: 1 } }))
  const [first, duplicate, another] = f.report.events
  assert.equal(first.argumentsHash, duplicate.argumentsHash)
  assert.equal(first.resultHash, duplicate.resultHash)
  assert.equal(first.toolIdentityHash, duplicate.toolIdentityHash)
  assert.equal(duplicate.deliveryIndex, 2)
  assert.equal(duplicate.repeatedDelivery, true)
  assert.equal(another.deliveryIndex, 1)
  assert.notEqual(first.toolIdentityHash, another.toolIdentityHash)
  assert.equal(f.terminalError, null)
  assert.doesNotMatch(JSON.stringify(f.report), /synthetic-secret/)
})

test('thinking and freeform diagnostics never enter evidence; local timeout retains safe code', () => {
  const f = fixture()
  for (const event of ['thinking', 'text_delta', 'tool_use_delta']) {
    wire(f, { type: 'event', event: `session.event.${event}`, payload: { data: { task_id: 'task-1', text: 'synthetic-private-reasoning', arguments: 'synthetic-private-arguments' } } })
  }
  f.observeRendererDiagnostic({ text: () => 'unrelated console synthetic-private-secret' })
  assert.equal(f.report.events.length, 0)
  assert.equal(f.report.streamSummaries.length, 3)
  f.observeRendererDiagnostic({ text: () => 'Session stream subscription failed: sessions.snapshot timed out after 3000ms synthetic-private-detail' })
  assert.equal(f.report.events[0].code, 'RPC_TIMEOUT')
  assert.equal(f.report.events[0].method, 'sessions.snapshot')
  assert.equal(f.report.events[0].timeoutMs, 3000)
  assert.match(f.report.events[0].messageHash, /^[a-f0-9]{64}$/)
  assert.doesNotMatch(JSON.stringify(f.report), /synthetic-private/)
})

test('existing guard and terminal signals retain codes without freeform warning text', () => {
  const f = fixture()
  wire(f, { type: 'event', event: 'session.event.warning', payload: { data: { task_id: 'task-1', code: 'STREAM_GUARD_STOP', reason: 'no_progress', watchdog_action: 'stop', watchdog_mode: 'observe', message: 'synthetic-private-warning' } } })
  wire(f, { type: 'event', event: 'session.event.done', payload: { data: { task_id: 'task-1', terminal_reason: 'stream_guard', finish_reason: 'stop', message: 'synthetic-private-output' } } })
  assert.equal(f.report.events[0].reason, 'no_progress')
  assert.equal(f.report.events[0].watchdog_action, 'stop')
  assert.equal(f.report.events[1].terminal_reason, 'stream_guard')
  assert.doesNotMatch(JSON.stringify(f.report), /synthetic-private/)
})

for (const outcome of [
  { kind: 'failed', reason: 'incomplete_stream', error_class: 'incomplete_stream', failure_kind: 'transport_transient', retryable: true },
  { kind: 'failed', reason: 'provider_error', error_class: 'provider_error', failure_kind: 'unknown', retryable: false },
  { kind: 'failed', reason: '503', error_class: '503', failure_kind: 'provider_overloaded', retryable: true },
  { kind: 'budgetLimited', reason: 'provider_request_budget_exhausted', error_class: 'provider_request_budget_exhausted', retryable: true },
]) test(`Gateway terminal outcome retains structured cause: ${outcome.reason}`, () => {
  const f = fixture()
  wire(f, { type: 'event', event: 'session.event.error', payload: { key: 'synthetic-private-session', data: {
    task_id: 'task-1', code: outcome.reason, terminal_reason: 'error', error_id: '0123abcd',
    message: 'synthetic-private-terminal-message', error_message: 'synthetic-private-provider-message',
    turn_outcome: { ...outcome, error_message: 'synthetic-private-outcome-message',
      provider_body: { text: 'synthetic-private-body' }, details: { thought: 'synthetic-private-thought' } },
  } } })
  assert.deepEqual(clone(f.report.events[0].turn_outcome), outcome)
  assert.equal(f.report.events[0].error_id, '0123abcd')
  assert.doesNotMatch(JSON.stringify(f.report), /synthetic-private/)
})

test('unknown outcome strings and nested values are rejected even when they look like codes', () => {
  const f = fixture()
  wire(f, { type: 'event', event: 'session.event.error', payload: {
    error_id: 'synthetic_private_error_id',
    turn_outcome: { kind: 'synthetic_private_kind', reason: 'synthetic_private_reason', error_class: 'synthetic_private_class',
      failure_kind: 'synthetic_private_failure', retryable: 'true', cause: { code: 'synthetic_private_cause' },
      message: 'synthetic_private_message', body: { reasoning: 'synthetic_private_thought' } },
  } })
  assert.equal(f.report.events[0].turn_outcome, undefined)
  assert.equal(f.report.events[0].error_id, undefined)
  wire(f, { type: 'event', event: 'session.event.error', payload: { data: {
    turn_outcome: { kind: { value: 'failed', secret: 'synthetic_private' }, reason: ['timeout'], error_class: 503,
      failure_kind: null, retryable: { value: false } },
  } } })
  assert.equal(f.report.events[1].turn_outcome, undefined)
  assert.doesNotMatch(JSON.stringify(f.report), /synthetic_private/)
})

for (const errorId of ['ABCDEF12', '0123abcd0123abcd', '0123abcd synthetic-private', { value: '0123abcd', secret: 'synthetic-private' }]) {
  test(`terminal error reference rejects invalid shape ${typeof errorId}:${String(errorId).length}`, () => {
    const f = fixture()
    wire(f, { type: 'event', event: 'session.event.error', payload: { data: { error_id: errorId, turn_outcome: { kind: 'failed', retryable: false } } } })
    assert.equal(f.report.events[0].error_id, undefined)
    assert.deepEqual(clone(f.report.events[0].turn_outcome), { kind: 'failed', retryable: false })
    assert.doesNotMatch(JSON.stringify(f.report), /synthetic-private/)
  })
}

test('chat rejection discards freeform error codes', () => {
  const f = fixture({ activeTurn: {} })
  wire(f, { type: 'req', id: 'send-1', method: 'chat.send', params: { key: 'private-key', text: 'private-input' } }, 'sent')
  wire(f, { type: 'res', id: 'send-1', error: { code: 'private secret detail', message: 'private message' } })
  assert.equal(f.activeTurn.error.code, 'RPC_ERROR')
  assert.match(f.terminalError.message, /CHAT_SEND_REJECTED: RPC_ERROR/)
  assert.doesNotMatch(JSON.stringify(f.report), /private/)
})

function turnFixture({ released, status = 'succeeded' }) {
  const persisted = [], waits = []
  let submissions = 0
  const f = fixture({
    variant: 'new', ledgerCase: 'landing-r1',
    foreground: async () => {}, ledgerSnapshot: async () => null,
    durableState: async () => ({ agent_tasks: [{ task_id: 'task-1', status, terminal_reason: status === 'failed' ? 'stream_guard' : null }] }),
  })
  const button = { count: async () => 1, isVisible: async () => true, isDisabled: async () => submissions > 0 && !released, getAttribute: async () => 'is-ready', click: async () => { submissions += 1; f.activeTurn.taskId = 'task-1' } }
  f.page = {
    locator: selector => selector === '.chat-textarea' ? { fill: async () => {} } : button,
    evaluate: async () => ({ pathname: '/chat/private-route', connected: true, send: [{ visible: true, disabled: !released }], stop: [{ visible: !released, disabled: false }], textarea: [{ visible: true, disabled: false }] }),
  }
  f.persist = async () => persisted.push(clone(f.report))
  f.waitFor = async (check, label, timeout) => {
    waits.push({ label, timeout })
    if (!await check()) throw new Error(`WAIT_EXPIRED:${label}`)
  }
  vm.runInContext(section('async function sendTurn(', 'async function openPreview('), f)
  return { f, persisted, waits, submissions: () => submissions }
}

test('durable success is persisted before waiting for a busy composer, without resubmitting', async () => {
  const t = turnFixture({ released: false })
  await assert.rejects(t.f.sendTurn('generation', 'synthetic prompt'), /WAIT_EXPIRED:composer released/)
  const terminal = t.persisted.find(row => row.turns[0]?.terminalStatus === 'succeeded')
  assert.equal(terminal.phase, 'waiting-composer-release')
  assert.equal(terminal.turns[0].composer, undefined)
  assert.equal(t.persisted.at(-1).turns[0].composer.send[0].disabled, true)
  assert.equal(t.submissions(), 1)
  assert.equal(t.waits.at(-1).timeout, 630000)
  assert.doesNotMatch(JSON.stringify(t.persisted), /private-route|synthetic prompt/)
})

test('composer release is separately timestamped after durable success', async () => {
  const t = turnFixture({ released: true })
  await t.f.sendTurn('generation', 'synthetic prompt')
  const firstTerminal = t.persisted.find(row => row.turns[0]?.terminalStatus === 'succeeded')
  const final = t.persisted.at(-1)
  assert.equal(firstTerminal.phase, 'waiting-composer-release')
  assert.equal(final.phase, 'generation')
  assert.ok(final.turns[0].composerReleasedAtMs >= final.turns[0].finishedAtMs)
  assert.ok(final.turns[0].composerReleaseDelayMs >= 0)
  assert.equal(t.f.activeTurn, null)
  assert.equal(t.submissions(), 1)
})

test('durable failure is persisted and stops before composer release or another send', async () => {
  const t = turnFixture({ released: true, status: 'failed' })
  await assert.rejects(t.f.sendTurn('generation', 'synthetic prompt'), /TURN_TERMINATED/)
  assert.equal(t.persisted.at(-1).turns[0].terminalReason, 'stream_guard')
  assert.equal(t.waits.some(row => row.label === 'composer released'), false)
  assert.equal(t.submissions(), 1)
})

test('functional ready binds the physical request log and rejects mixed modes', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'html-request-log-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const log = join(directory, 'requests.sqlite'), other = join(directory, 'other.sqlite')
  await writeFile(log, ''); await writeFile(other, '')
  const ready = { mode: 'functional', request_log: log }
  const f = fixture()
  assert.equal(await f.resolveRequestLogPath('', '', ready), await realpath(log))
  assert.equal(await f.resolveRequestLogPath('', log, ready), await realpath(log))
  await assert.rejects(f.resolveRequestLogPath('', other, ready), /FUNCTIONAL_REQUEST_LOG_MISMATCH/)
  await assert.rejects(f.resolveRequestLogPath(other, log, ready), /REQUEST_LOG_AND_LEDGER_ARE_EXCLUSIVE/)
  await assert.rejects(f.resolveRequestLogPath(other, '', ready), /FUNCTIONAL_REQUEST_LOG_REQUIRED/)
  await assert.rejects(f.resolveRequestLogPath('', log, {}), /FUNCTIONAL_RELAY_READY_REQUIRED/)
  assert.equal(await f.resolveRequestLogPath(other, '', {}), '')
})

test('request snapshots read functional HTTP outcomes separately from legacy accounting', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'html-request-snapshot-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const python = process.platform === 'win32' ? 'python' : 'python3'
  const execute = (program, ...args) => execFileSync(python, ['-c', program, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] })
  const functional = join(directory, 'functional.sqlite'), budget = join(directory, 'budget.sqlite')
  execute(`import json,sqlite3,sys
for path,mode in [(sys.argv[1],'functional'),(sys.argv[2],'budget')]:
 with sqlite3.connect(path) as c:
  c.execute('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT)')
  c.executemany('INSERT INTO state VALUES(?,?)',[('mode',mode),('schema_version','1'),('phase',json.dumps({'variant':'new','case_id':'landing-r1'}))])
  c.execute('CREATE TABLE requests(id TEXT,variant TEXT,case_id TEXT,status TEXT,http_status INTEGER,reason TEXT,charged_nanos INTEGER)')
  c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',('one','new','landing-r1','completed' if mode=='functional' else 'confirmed',503,'http_eof',17))
  c.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?)',('other','new','other-case','in_flight' if mode=='functional' else 'reserved',None,'pending',0))
`, functional, budget)
  const f = fixture({ requestLogPath: functional, ledgerPath: '', variant: 'new', ledgerCase: 'landing-r1', runPython: async (program, ...args) => JSON.parse(execute(program, ...args)) })
  vm.runInContext(section('async function ledgerSnapshot(', 'async function drainRelay('), f)
  const snapshot = await f.ledgerSnapshot()
  assert.equal(snapshot.mode, 'functional')
  assert.equal(snapshot.pendingRequests, 1)
  assert.equal(snapshot.requests.length, 1)
  assert.equal(snapshot.requests[0].status, 'completed')
  assert.equal(snapshot.requests[0].http_status, 503)
  assert.equal(Object.hasOwn(snapshot.requests[0], 'charged_nanos'), false)
  f.requestLogPath = ''; f.ledgerPath = budget
  const legacy = await f.ledgerSnapshot()
  assert.equal(legacy.mode, 'budget')
  assert.equal(legacy.requests[0].charged_nanos, 17)
  assert.equal(legacy.pendingRequests, 1)
  f.requestLogPath = budget; f.ledgerPath = ''
  await assert.rejects(f.ledgerSnapshot(), /FUNCTIONAL_REQUEST_LOG_REQUIRED/)
  f.requestLogPath = ''; f.ledgerPath = functional
  await assert.rejects(f.ledgerSnapshot(), /BUDGET_LEDGER_REQUIRED/)
})
