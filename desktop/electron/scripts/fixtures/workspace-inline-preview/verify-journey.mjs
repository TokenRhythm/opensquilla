// Real, free UI acceptance. Only the Ollama model is a deterministic fixture.
// No sessions, receipts, Documents, or application stores are seeded or mutated
// by the harness. Database inspection uses SQLite mode=ro + query_only.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFile, spawn } from 'node:child_process'
import { createWriteStream } from 'node:fs'
import { cp, lstat, mkdir, readFile, realpath, symlink, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { delimiter, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { pathToFileURL } from 'node:url'
import { promisify } from 'node:util'

const exec = promisify(execFile)
if (process.argv.includes('--help')) {
  console.log('Usage: node verify-journey.mjs --source-root PATH --output PATH --surface web|desktop')
  console.log('Requires a prepared ordinary source checkout and an explicitly NEW ordinary evidence directory. No paid providers, existing profiles, system-temp paths, or .codex paths. Runs actual UI turns and retains first-failure evidence. Desktop requires a GUI session; this is not full-suite or packaged-release acceptance.')
  process.exit(0)
}
const options = {}
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index]
  assert.ok(['--source-root', '--output', '--surface'].includes(key) && process.argv[index + 1] && !options[key], `Invalid or duplicate option: ${key}`)
  options[key] = process.argv[index + 1]
}
assert.ok(isAbsolute(options['--source-root'] || '') && isAbsolute(options['--output'] || ''), 'Explicit absolute source-root and output paths are required.')
const surface = options['--surface']
assert.ok(['web', 'desktop'].includes(surface), 'Choose --surface web or desktop.')
const sourceRoot = await realpath(options['--source-root'])
const output = resolve(options['--output'])
const temporaryRoots = ['/tmp', '/private/tmp', '/var/folders', '/private/var/folders', await realpath(tmpdir())]
function inside(parent, path) { const suffix = relative(parent, path); return suffix === '' || (!suffix.startsWith(`..${sep}`) && suffix !== '..' && !isAbsolute(suffix)) }
function ordinary(path) {
  assert.ok(!path.split(sep).includes('.codex') && !temporaryRoots.some(root => inside(root, path)), `Protected or system-temp location: ${path}`)
}
async function exists(path) { try { await lstat(path); return true } catch (error) { if (error.code === 'ENOENT') return false; throw error } }
ordinary(sourceRoot)
ordinary(output)
assert.equal(await realpath(dirname(output)), dirname(output), 'Output parent must exist without symlink aliases.')
assert.ok(!await exists(output), 'Output must be new; existing directories/profiles are never reused.')
assert.ok(!inside(sourceRoot, output), 'Keep acceptance evidence outside the source checkout.')
for (let ancestor = dirname(output); dirname(ancestor) !== ancestor; ancestor = dirname(ancestor)) {
  assert.ok(!['.opensquilla', 'opensquilla-test-profiles'].includes(ancestor.split(sep).at(-1)), 'Output cannot be inside a user profile.')
  for (const name of ['desktop-credential.json', 'config.toml']) assert.ok(!await exists(join(ancestor, name)), `Existing profile ancestor: ${ancestor}`)
}
for (const name of ['.env', '.env.test']) assert.ok(!await exists(join(sourceRoot, name)), `Source ${name} could rehydrate provider credentials; use the prepared clean acceptance checkout.`)
await mkdir(output, { mode: 0o700 })

const desktopRoot = join(sourceRoot, 'desktop/electron')
const load = path => import(pathToFileURL(join(sourceRoot, path)).href)
const { waitFor, environmentWithoutProviderSecrets, writeSyntheticCredential } = await load('desktop/electron/scripts/packaged-smoke-helpers.mjs')
const { closeElectronWithDeadline } = await load('desktop/electron/scripts/e2e-shutdown-helpers.mjs')
const { requireDesktopForeground } = await load('desktop/electron/scripts/live-html-foreground.mjs')
const { desktopProfileFingerprint } = await load('desktop/electron/dist/desktop-gateway-ownership.js')
const fixture = await load('desktop/electron/scripts/fixtures/workspace-inline-preview/provider.mjs')
const hydrateValidatorPath = 'opensquilla-webui/src/contracts/generated/v4/sessionsMessagesHydrateValidators.mjs'
const { validateSessionsMessagesHydrateResult } = await load(hydrateValidatorPath)
const require = createRequire(join(desktopRoot, 'package.json'))
const { chromium, _electron: electron } = require('playwright')
const python = join(sourceRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const userData = join(output, 'electron-user-data')
const profile = surface === 'desktop' ? join(userData, 'opensquilla') : join(output, 'profile')
const configPath = join(profile, 'config.toml')
const cleanEnv = environmentWithoutProviderSecrets(process.env)
for (const name of Object.keys(cleanEnv)) {
  if (name.startsWith('OPENSQUILLA_') || name.startsWith('TOKENRHYTHM_') || /^(https?|all|no)_proxy$/i.test(name) || ['PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'ELECTRON_RUN_AS_NODE', 'NODE_OPTIONS'].includes(name)) delete cleanEnv[name]
}
const env = {
  ...cleanEnv, PYTHONPATH: `${join(sourceRoot, 'src')}${delimiter}${sourceRoot}`, PYTHONDONTWRITEBYTECODE: '1',
  PATH: `${dirname(python)}${delimiter}${cleanEnv.PATH || ''}`, UV_NO_SYNC: '1',
  OPENSQUILLA_STATE_DIR: profile, OPENSQUILLA_USER_STATE_DIR: join(output, 'user-state'),
  OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1', OPENSQUILLA_TESTING: '0',
  OPENSQUILLA_MEMORY_DREAM_DISABLED: '1',
  OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
  OPENSQUILLA_GATEWAY_CONFIG_PATH: configPath, NO_PROXY: '127.0.0.1,localhost,::1',
  OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain', OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
  OPENSQUILLA_DESKTOP_REPO_ROOT: sourceRoot,
}
const report = {
  schemaVersion: 1, surface, sourceRoot, output, startedAt: new Date().toISOString(), status: 'running',
  execution: { realGateway: true, realFileTools: true, realDatabase: true, modelFixtureOnly: true, seededSessions: false, directRpcMutations: false, automaticRetries: 0, paidCalls: false },
  checkpoints: [], turns: [], processes: [], cleanup: [],
  cleanAcceptance: true,
  hydrateContract: { validationBoundary: 'canonical-after-v3-alias', checked: 0, legacyRunModeResponses: 0, invalidResponses: 0, keyMismatches: 0, consoleWarnings: 0, failures: [] },
}
let provider, gateway, app, browser, page, port, interruption
const logStreams = []
process.on('SIGINT', () => { interruption ||= new Error('Interrupted by SIGINT') })
process.on('SIGTERM', () => { interruption ||= new Error('Interrupted by SIGTERM') })
const persist = () => writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2), { mode: 0o600 })
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
function hydrateContractErrors(errors) {
  // Only schema-defined field names and array indices can leave the inspector.
  // Neither AJV's raw error params nor any rejected response values are logged.
  const fields = new Set(['key', 'workspaceId', 'projectWorkspace', 'projectWorkspaceDeferred', 'active_task_group_ids', 'run_mode_lock', 'locked', 'runMode', 'source', 'pendingUserInputs', 'collaboration', 'routing', 'currentPlan', 'activePlanRun', 'goal', 'goalSnapshotStreamSeq', 'tasks', 'active_task', 'last_task', 'run_status', 'queued_task_ids', 'epoch', 'hydration_complete', 'deferred_fields'])
  const pointer = value => String(value || '').split('/').map(part => !part || fields.has(part) || /^\d+$/.test(part) ? part : '<redacted-property>').join('/')
  const types = new Set(['array', 'boolean', 'integer', 'null', 'number', 'object', 'string'])
  return (errors || []).slice(0, 16).map(error => {
    const missing = error.keyword === 'required' && fields.has(error.params?.missingProperty) ? `/${error.params.missingProperty}` : ''
    const declared = Array.isArray(error.params?.type) ? error.params.type : [error.params?.type]
    const expected = declared.filter(type => types.has(type))
    return {
      instancePath: pointer(error.instancePath) + missing,
      keyword: ['type', 'required', 'const', 'enum', 'anyOf', 'additionalProperties'].includes(error.keyword) ? error.keyword : 'schema-rule',
      expectedType: expected.length ? expected.join('|')
        : error.keyword === 'const' && typeof error.params?.allowedValue === 'boolean' ? `boolean (literal ${error.params.allowedValue})`
          : error.keyword === 'required' ? 'required property' : 'schema constraint',
    }
  })
}
function instrumentPage(observedPage) {
  const stream = createWriteStream(join(output, 'ui-transport.ndjson'), { flags: 'wx', mode: 0o600 })
  logStreams.push(stream)
  const diagnostic = report.uiTransport = { recorded: 0, discarded: 0, console: [], latest: [] }
  const requests = new Map()
  let socketNumber = 0
  const token = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(value) ? value : undefined
  const append = summary => {
    if (diagnostic.recorded >= 2000) { diagnostic.discarded += 1; return }
    const record = { at: new Date().toISOString(), ...summary }
    diagnostic.recorded += 1
    diagnostic.latest.push(record)
    if (diagnostic.latest.length > 100) diagnostic.latest.shift()
    stream.write(`${JSON.stringify(record)}\n`)
  }
  observedPage.on('websocket', socket => {
    const socketId = ++socketNumber
    // No URL: a connection URL can contain a credential query parameter.
    append({ socketId, direction: 'lifecycle', type: 'opened' })
    const frame = (direction, event) => {
      const bytes = event.payload
      if (Buffer.byteLength(bytes) > 4 * 1024 * 1024) { append({ socketId, direction, type: 'oversize-not-inspected' }); return }
      let value
      try { value = JSON.parse(typeof bytes === 'string' ? bytes : bytes.toString('utf8')) } catch { append({ socketId, direction, type: 'non-json' }); return }
      const id = token(value?.id)
      const method = token(value?.method)
      if (direction === 'sent' && id && method && requests.size < 1000) requests.set(`${socketId}:${id}`, { method, key: value?.params?.key })
      const request = requests.get(`${socketId}:${id}`)
      const summary = { socketId, direction, type: token(value?.type), id, method: method || request?.method, event: token(value?.event) }
      if (typeof value?.ok === 'boolean') summary.ok = value.ok
      if (token(value?.error?.code)) summary.errorCode = value.error.code
      if (Number.isSafeInteger(value?.seq)) summary.sequence = value.seq
      if (direction === 'received' && value?.type === 'res' && value.ok === true && summary.method === 'sessions.messages.hydrate') {
        // The negotiated v3 wire legitimately names Safe "trusted". Check its
        // canonical meaning with the unchanged v4 schema, not v3 bytes as v4.
        // No other alias or malformed field is repaired by this inspector.
        const legacyRunMode = value.payload?.run_mode_lock?.runMode === 'trusted'
        const canonical = legacyRunMode
          ? { ...value.payload, run_mode_lock: { ...value.payload.run_mode_lock, runMode: 'safe' } }
          : value.payload
        const valid = validateSessionsMessagesHydrateResult(canonical)
        const keyMatches = typeof request?.key === 'string' && value.payload?.key === request.key
        const errors = valid ? [] : hydrateContractErrors(validateSessionsMessagesHydrateResult.errors)
        report.hydrateContract.checked += 1
        report.hydrateContract.legacyRunModeResponses += Number(legacyRunMode)
        summary.contract = { valid, legacyRunMode, responseKeyMatchesRequest: keyMatches, errors }
        if (!valid || !keyMatches) {
          report.cleanAcceptance = false
          report.hydrateContract.invalidResponses += Number(!valid)
          report.hydrateContract.keyMismatches += Number(!keyMatches)
          if (report.hydrateContract.failures.length < 20) report.hydrateContract.failures.push({ at: new Date().toISOString(), socketId, id, ...summary.contract })
        }
      }
      for (const body of [value?.params, value?.payload, value?.payload?.runtime, value?.payload?.snapshot]) {
        if (!body || typeof body !== 'object' || Array.isArray(body)) continue
        const key = body.sessionKey || body.session_key || body.key
        if (typeof key === 'string' && /^agent:[A-Za-z0-9_-]+:(webchat|subagent):[A-Za-z0-9_-]+$/.test(key)) summary.sessionKey = key
        for (const name of ['state', 'status', 'phase']) if (token(body[name])) (summary.runtime ||= {})[name] = body[name]
        for (const name of ['authoritative', 'subscribed', 'sessionMissing', 'complete', 'fast_ack']) if (typeof body[name] === 'boolean') (summary.runtime ||= {})[name] = body[name]
        for (const name of ['generation', 'epoch', 'seq', 'eventSeq']) if (Number.isSafeInteger(body[name])) (summary.runtime ||= {})[name] = body[name]
      }
      append(summary)
      if (direction === 'received' && value?.type === 'res' && id) requests.delete(`${socketId}:${id}`)
    }
    socket.on('framesent', event => frame('sent', event))
    socket.on('framereceived', event => frame('received', event))
    socket.on('close', () => append({ socketId, direction: 'lifecycle', type: 'closed' }))
    socket.on('socketerror', () => append({ socketId, direction: 'lifecycle', type: 'socket-error' }))
  })
  const consoleSummary = (severity, text) => {
    if (/sessions\.messages\.(hydrate|subscribe)( result)? violated its generated v4 Contract\./.test(text)) {
      report.cleanAcceptance = false
      report.hydrateContract.consoleWarnings += 1
    }
    if (diagnostic.console.length >= 100) return
    const rpcTimeout = text.match(/\b((?:sessions|chat|conversation|artifact)\.[a-z_.]+) timed out after (\d+)ms\b/)
    const known = ['Session stream subscription failed', 'Session metadata hydration failed', 'Session metadata recovery failed', 'Sequence gap detected', 'Connection closed', 'No conversation consumer owns this delivery', 'Failed to fetch'].find(message => text.includes(message))
    // Arbitrary console text can contain prompts or credentials. Keep only
    // a known class, bounded RPC identifiers, and a hash of the whole message.
    const record = { at: new Date().toISOString(), severity, class: known || 'unclassified', textLength: text.length, textSha256: hash(text), ...(rpcTimeout ? { method: rpcTimeout[1], timeoutMs: Number(rpcTimeout[2]) } : {}) }
    diagnostic.console.push(record)
    append({ direction: 'console', type: severity, ...record })
  }
  observedPage.on('console', message => { if (['warning', 'error'].includes(message.type())) consoleSummary(message.type(), message.text()) })
  observedPage.on('pageerror', error => consoleSummary('pageerror', error.message))
}
async function readSessionDiagnostics() {
  if (!page || page.isClosed()) return null
  return page.evaluate(() => {
    const entries = window.OpenSquillaSessionDiag?.read?.() || []
    const fields = ['t', 'iso', 'source', 'from', 'to', 'current', 'routeSession', 'requestSession', 'responseSession', 'reason', 'rendererInstance', 'generation', 'connId', 'handoffEpoch', 'targetKeyHash', 'phase', 'closeCode', 'wasClean', 'reconnectAttempt', 'delayMs', 'recoveryMs', 'loopLagMs', 'maxLoopLagMs']
    return entries.slice(0, 100).map(entry => Object.fromEntries(fields.filter(name => typeof entry[name] === 'number' || typeof entry[name] === 'boolean' || typeof entry[name] === 'string' && entry[name].length <= 160).map(name => [name, entry[name]])))
  })
}
async function poll(check, label, timeout = 60000) {
  let answer
  await waitFor(async () => {
    if (interruption) return { error: interruption }
    if (provider?.snapshot().errors.length) return { error: new Error(`Provider rejected a real request: ${provider.snapshot().errors.join('; ')}`) }
    try { answer = await check(); return answer } catch (error) { return { error } }
  }, label, timeout).then(result => { if (result?.error) throw result.error })
  return answer
}
async function runPython(code, ...args) {
  const result = await exec(python, ['-B', '-c', code, ...args], { cwd: sourceRoot, env, maxBuffer: 8 * 1024 * 1024, timeout: 30000 })
  return JSON.parse(result.stdout)
}
const evidencePython = String.raw`
import json,sqlite3,sys
from pathlib import Path
from opensquilla.artifacts import ArtifactStore
from opensquilla.gateway.config import GatewayConfig
from opensquilla.agents.scope import resolve_agent_workspace_dir
profile=Path(sys.argv[1]); config=GatewayConfig.load(profile/'config.toml',read_only=True)
database=Path(config.state_dir)/'sessions.db'
names=['sessions','agent_tasks','turn_ingress_receipts','transcript_entries','artifact_documents','artifact_revisions','artifact_working_files','artifact_working_sources','document_publications','document_source_bindings']
result={name:[] for name in names}
result.update(database=str(database),defaultWorkspace=str(resolve_agent_workspace_dir('main',config)),deliverables=[])
if database.exists():
    connection=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2)
    connection.row_factory=sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name in names:
        if name in tables:
            result[name]=[dict(row) for row in connection.execute('SELECT * FROM '+name+' LIMIT 1001')]
            assert len(result[name])<=1000,'Evidence row limit exceeded: '+name
    connection.close()
    store=ArtifactStore(profile/'media')
    for session in result['sessions']:
        page=store.list_refs(session_id=session['session_id'],limit=1000)
        assert not page.has_more,'Artifact evidence page truncated'
        result['deliverables'].extend(ref.to_dict() for ref in page.refs)
print(json.dumps(result))
`
const evidence = () => runPython(evidencePython, profile)
async function reservePort() {
  const server = createServer()
  await new Promise((done, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', done) })
  const value = server.address().port
  await new Promise(done => server.close(done))
  return value
}
async function healthy() { try { return (await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1500) })).ok } catch { return false } }
async function startGateway() {
  assert.ok(!gateway, 'A Gateway is already owned.')
  const log = createWriteStream(join(output, `gateway-${report.processes.length + 1}.log`), { flags: 'wx', mode: 0o600 })
  logStreams.push(log)
  gateway = spawn(python, ['-B', '-m', 'opensquilla.cli.main', 'gateway', 'run', '--listen', '127.0.0.1', '--port', String(port), '--config', configPath], { cwd: sourceRoot, env, stdio: ['ignore', 'pipe', 'pipe'] })
  gateway.stdout.pipe(log, { end: false }); gateway.stderr.pipe(log, { end: false })
  const record = { kind: 'gateway', pid: gateway.pid, startedAt: new Date().toISOString() }
  report.processes.push(record)
  gateway.once('error', error => { record.error = error.message })
  gateway.once('exit', (code, signal) => { record.exitCode = code; record.signal = signal; record.exitedAt = new Date().toISOString() })
  await poll(async () => {
    assert.ok(!record.error && !record.exitedAt, `Gateway failed before readiness: ${JSON.stringify(record)}`)
    return healthy()
  }, 'real Gateway readiness', 120000)
}
async function stopGateway() {
  if (!gateway) return
  const owned = gateway
  if (owned.exitCode === null && owned.signalCode === null) {
    owned.kill('SIGTERM')
    await waitFor(() => owned.exitCode !== null || owned.signalCode !== null, 'owned Gateway shutdown', 20000).catch(async error => {
      owned.kill('SIGKILL')
      await waitFor(() => owned.exitCode !== null || owned.signalCode !== null, 'owned Gateway forced shutdown', 5000)
      throw error
    })
  }
  gateway = undefined
  await waitFor(async () => !await healthy(), 'Gateway port closed after real process exit', 5000)
}
async function screenshot(label) {
  if (page && !page.isClosed()) await page.screenshot({ path: join(output, `${label}.png`), fullPage: true })
  if (app) {
    const previews = await nativePreviews()
    for (const preview of previews) {
      const data = await app.evaluate(async ({ webContents }, id) => (await webContents.fromId(id).capturePage()).toPNG().toString('base64'), preview.id)
      await writeFile(join(output, `${label}-native-${preview.id}.png`), Buffer.from(data, 'base64'))
    }
  }
}
async function nativePreviews() {
  return app.evaluate(async ({ webContents }) => {
    const result = []
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      const owner = contents.getOwnerBrowserWindow()
      const view = owner?.contentView.children.find(item => item.webContents?.id === contents.id)
      if (!view?.getVisible?.()) continue
      try {
        const dom = await contents.executeJavaScript(`(() => { const h=document.querySelector('h1'); if(!h)return null; const r=h.getBoundingClientRect(); return {heading:h.textContent,color:getComputedStyle(h).color,point:{x:r.x+r.width/2,y:r.y+r.height/2},url:location.href,html:document.documentElement.outerHTML} })()`, true)
        if (dom) result.push({ id: contents.id, ...dom })
      } catch { /* A just-destroyed view is not a visible preview. */ }
    }
    return result
  })
}
async function renderedPreview(heading, color) {
  return poll(async () => {
    const previews = app ? await nativePreviews() : await Promise.all(page.frames().filter(frame => frame !== page.mainFrame()).map(async frame => {
      try {
        if (!await (await frame.frameElement()).isVisible()) return null
        return await frame.evaluate(() => { const h = document.querySelector('h1'); return h ? { heading: h.textContent, color: getComputedStyle(h).color, url: location.href, html: document.documentElement.outerHTML } : null })
      } catch { return null }
    }))
    return previews.find(preview => preview?.heading === heading && preview.color === color)
  }, `visible ${heading} preview with computed color ${color}`, 45000)
}
async function send(name, sessionKey) {
  report.phase = name
  await persist()
  if (app) await requireDesktopForeground(app, page)
  const before = await evidence()
  const prior = new Set(before.turn_ingress_receipts.map(row => row.receipt_id))
  await page.locator('.chat-textarea').fill(fixture.prompts[name])
  await page.locator('.chat-send-btn.btn--primary').click()
  const receipt = await poll(async () => {
    const state = await evidence()
    const added = state.turn_ingress_receipts.filter(row => !prior.has(row.receipt_id))
    assert.ok(added.length <= 1, 'One UI submit must admit exactly one turn.')
    return added[0]
  }, `UI admission receipt for ${name}`)
  if (sessionKey) assert.equal(receipt.accepted_session_key, sessionKey, 'UI sent to another task.')
  report.turns.push({ name, receipt })
  await poll(async () => {
    const state = await evidence()
    const task = state.agent_tasks.find(row => row.task_id === receipt.task_id)
    if (task && ['failed', 'cancelled', 'timeout', 'abandoned'].includes(task.status)) throw new Error(`Real task ${name} failed: ${JSON.stringify(task)}`)
    const fixtureDone = name === 'spawnA' ? fixtureChildFinished() : provider.snapshot().completed[name] === 1
    return fixtureDone && task && ['completed', 'succeeded', 'done', 'yielded'].includes(task.status)
  }, `real task and model completion for ${name}`, 120000)
  await persist()
  return receipt.accepted_session_key
}
function fixtureChildFinished() { const done = provider.snapshot().completed; return done.childWrite === 1 && done.childVerify === 1 }
async function newTask() { await page.locator('button.sidebar-new-session').click() }
async function selectTask(key) {
  await page.locator(`[data-session-key=${JSON.stringify(key)}] .sidebar-history-item`).first().click()
  await poll(async () => new URL(page.url()).searchParams.get('session') === key, 'task selected through sidebar')
}
async function openPreview() {
  const link = page.locator('.workspace-file-link').filter({ hasText: fixture.entrypoint }).last()
  await link.waitFor({ state: 'visible', timeout: 30000 })
  await link.click()
}
const stable = new Map()
async function checkpoint(label, key, css, heading, color, revisions) {
  await openPreview()
  const preview = await renderedPreview(heading, color)
  const state = await evidence()
  const session = state.sessions.find(row => row.session_key === key)
  assert.ok(session, 'UI-created session missing from DB.')
  const binding = typeof session.execution_workspace === 'string' ? JSON.parse(session.execution_workspace) : session.execution_workspace
  assert.equal(binding?.kind, 'managed', 'Ordinary new task must receive its managed execution workspace.')
  assert.ok(inside(join(profile, 'tasks'), binding.root), 'Managed workspace must belong to this new profile.')
  assert.equal(await realpath(binding.root), binding.root, 'Workspace is not a canonical real directory.')
  const documents = state.artifact_documents.filter(row => row.session_key === key)
  assert.equal(documents.length, 1, 'One working HTML preview must own one Document.')
  const document = documents[0]
  const working = state.artifact_working_files.find(row => row.document_id === document.document_id)
  assert.equal(working.workspace, binding.root, 'Document working material is in another workspace.')
  const history = state.artifact_revisions.filter(row => row.document_id === document.document_id).sort((a, b) => a.generation - b.generation)
  assert.equal(history.length, revisions, 'Unexpected working Document revision count.')
  assert.equal(document.generation, revisions)
  assert.equal(document.head_revision_id, history.at(-1).revision_id)
  const files = {}
  for (const name of [fixture.entrypoint, fixture.stylesheet]) {
    const bytes = await readFile(join(binding.root, name))
    files[name] = { bytes: bytes.length, sha256: hash(bytes), text: bytes.toString('utf8') }
  }
  assert.equal(files[fixture.stylesheet].text, css, 'Real CSS bytes do not match this turn.')
  assert.ok(files[fixture.entrypoint].text.includes(`<h1>${heading}</h1>`))
  const prior = stable.get(key)
  if (prior) {
    assert.deepEqual(binding, prior.binding, 'Persisted execution binding changed.')
    assert.equal(document.document_id, prior.documentId, 'Editing/resuming replaced the Document.')
    assert.equal(files[fixture.entrypoint].sha256, prior.htmlSha256, 'CSS-only edit changed HTML.')
  } else stable.set(key, { binding, documentId: document.document_id, htmlSha256: files[fixture.entrypoint].sha256 })
  assert.equal(state.document_publications.length, 0, 'Working HTML was implicitly published.')
  assert.equal(state.deliverables.length, 0, 'Working HTML became a listed deliverable.')
  assert.equal(state.document_source_bindings.filter(row => row.source_type === 'deliverable').length, 0)
  await assertDefaultUntouched(state)
  const record = { label, key, binding, document, working, revisions: history, files, preview, publications: state.document_publications.length, deliverables: state.deliverables.length, defaultWorkspace: state.defaultWorkspace }
  report.checkpoints.push(record)
  await screenshot(label)
  await persist()
  return record
}
async function assertDefaultUntouched(state) {
  assert.ok(inside(profile, state.defaultWorkspace), 'Default workspace escaped the isolated profile.')
  for (const root of [state.defaultWorkspace, sourceRoot]) for (const name of ['inline-preview', 'child.txt']) assert.ok(!await exists(join(root, name)), `Unexpected product in default/source root: ${join(root, name)}`)
}
async function annotateHeading() {
  await requireDesktopForeground(app, page)
  const button = page.getByRole('button', { name: /Annotate preview|批注预览/ })
  if (await button.getAttribute('aria-pressed') !== 'true') await button.click()
  const preview = (await nativePreviews()).find(item => item.heading === '正文链接测试')
  assert.ok(preview, 'The original native preview is not visible.')
  await app.evaluate(async ({ webContents }, request) => {
    const contents = webContents.fromId(request.id)
    contents.focus()
    const attachedHere = !contents.debugger.isAttached()
    if (attachedHere) contents.debugger.attach('1.3')
    try {
      for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type, ...request.point, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: 1 })
    } finally { if (attachedHere) contents.debugger.detach() }
  }, { id: preview.id, point: preview.point })
  const editorId = await poll(() => app.evaluate(async ({ webContents }) => {
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      const view = contents.getOwnerBrowserWindow()?.contentView.children.find(item => item.webContents?.id === contents.id)
      if (view?.getVisible?.() && await contents.executeJavaScript("document.activeElement?.id==='annotation-body'", true).catch(() => false)) return contents.id
    }
    return null
  }), 'native annotation editor', 30000)
  // Input happens once, outside polling. No DOM/store writes stand in for input.
  await app.evaluate(async ({ webContents }, request) => {
    const contents = webContents.fromId(request.id)
    contents.focus()
    await contents.insertText(request.text)
    if (await contents.executeJavaScript("document.getElementById('annotation-body').value", true) !== request.text) throw new Error('Native annotation input mismatch.')
    contents.sendInputEvent({ type: 'keyDown', keyCode: 'Enter' })
    contents.sendInputEvent({ type: 'keyUp', keyCode: 'Enter' })
  }, { id: editorId, text: fixture.prompts.edit })
  await page.locator('.chat-prompt-annotation-chip').filter({ hasText: fixture.prompts.edit }).waitFor({ state: 'visible', timeout: 30000 })
  await screenshot('desktop-annotation-attached')
}

try {
  await persist()
  report.source = {
    cwd: await realpath(process.cwd()), root: (await exec('git', ['rev-parse', '--show-toplevel'], { cwd: sourceRoot })).stdout.trim(),
    head: (await exec('git', ['rev-parse', 'HEAD'], { cwd: sourceRoot })).stdout.trim(),
    status: (await exec('git', ['status', '--porcelain=v1'], { cwd: sourceRoot })).stdout.trim(), node: process.version,
    python: await runPython('import json,sys,opensquilla; print(json.dumps({"executable":sys.executable,"package":opensquilla.__file__}))'),
  }
  assert.equal(await realpath(report.source.root), sourceRoot)
  assert.ok(inside(join(sourceRoot, 'src/opensquilla'), await realpath(report.source.python.package)), 'Python imported another checkout.')
  for (const path of ['opensquilla-webui/dist/index.html', 'src/opensquilla/gateway/static/dist/index.html', 'desktop/electron/dist/main.js', 'desktop/electron/dist/preload.cjs']) assert.ok(await exists(join(sourceRoot, path)), `Required prepared build missing: ${path}`)
  // Both verification commands are read-only; they check source/build provenance.
  report.source.webuiVerification = (await exec(process.execPath, ['scripts/verify-dist.mjs'], { cwd: join(sourceRoot, 'opensquilla-webui'), env })).stdout.trim()
  report.source.stagedWebuiVerification = (await exec(process.execPath, ['scripts/stage-dist.mjs', '--check'], { cwd: join(sourceRoot, 'opensquilla-webui'), env })).stdout.trim()
  report.source.desktopMainSha256 = hash(await readFile(join(desktopRoot, 'dist/main.js')))
  report.source.hydrateValidator = { path: hydrateValidatorPath, sha256: hash(await readFile(join(sourceRoot, hydrateValidatorPath))) }
  port = await reservePort()
  provider = await fixture.startWorkspaceInlinePreviewProvider()
  await mkdir(profile, { recursive: true, mode: 0o700 })
  const quote = value => JSON.stringify(value)
  await writeFile(configPath, `host = "127.0.0.1"
port = ${port}
state_dir = ${quote(join(profile, 'state'))}

[llm]
provider = "ollama"
model = ${quote(fixture.model)}
base_url = ${quote(provider.baseUrl)}

# Capacity of this deterministic test model, not a production budget override.
[models.ollama.${quote(fixture.model)}]
context_window = 131072
max_output_tokens = 8192
supports_tools = true
supports_vision = true
supports_reasoning = false

[squilla_router]
enabled = false
[llm_ensemble]
enabled = false
[naming]
enabled = false
[heartbeat]
enabled = false
[mcp]
enabled = false

[privacy]
disable_network_observability = true
reliability_diagnostics_enabled = false
product_analytics_enabled = false
[model_catalog]
refresh = "off"

[sandbox]
run_mode = "safe"
sandbox = true
security_grading = true
network_default = "proxy_allowlist"
[permissions]
default_mode = "off"

[memory]
flush_enabled = false
repair_enabled = false
auto_capture_enabled = false
retrieval_mode = "fts_only"
ttl_sweep_interval_minutes = 0
`, { flag: 'wx', mode: 0o600 })
  report.configuration = await runPython('import json,sys; from opensquilla.gateway.config import GatewayConfig; c=GatewayConfig.load(sys.argv[1],read_only=True); print(json.dumps({"provider":c.llm.provider,"model":c.llm.model,"baseUrl":c.llm.base_url,"stateDir":c.state_dir,"workspaceDir":c.workspace_dir,"explicitWorkspace":c.workspace_dir_source=="configured","memorySource":c.memory.source,"sandbox":c.sandbox.model_dump(),"routing":c.squilla_router.enabled,"models":{p:{m:o.model_dump() for m,o in v.items()} for p,v in c.models.items()}}))', configPath)
  assert.equal(report.configuration.explicitWorkspace, false, 'Fixture must not configure a shared workspace.')
  if (surface === 'web') {
    await startGateway()
    browser = await chromium.launch({ headless: false, env })
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'en-US' })
    // Locale is an ordinary persisted UI preference, not a session/product store.
    await context.addInitScript(() => localStorage.setItem('opensquilla-locale', 'en'))
    page = await context.newPage()
    instrumentPage(page)
    await page.goto(`http://127.0.0.1:${port}/control/chat`)
  } else {
    await writeSyntheticCredential(userData, { baseUrl: provider.baseUrl, model: fixture.model, disableNetworkObservability: true })
    const shell = join(output, 'source-shell')
    await mkdir(join(shell, 'src'), { recursive: true })
    for (const name of ['dist', 'assets', 'package.json']) await cp(join(desktopRoot, name), join(shell, name), { recursive: true })
    await cp(join(desktopRoot, 'src/boot.html'), join(shell, 'src/boot.html'))
    await symlink(join(desktopRoot, 'node_modules'), join(shell, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir')
    app = await electron.launch({ executablePath: require('electron'), args: ['--use-mock-keychain', `--user-data-dir=${userData}`, shell], cwd: sourceRoot, env: { ...env, OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(port) }, timeout: 180000 })
    report.processes.push({ kind: 'electron', pid: app.process().pid })
    const log = createWriteStream(join(output, 'electron.log'), { flags: 'wx', mode: 0o600 }); logStreams.push(log)
    app.process().stdout?.pipe(log, { end: false }); app.process().stderr?.pipe(log, { end: false })
    page = await app.firstWindow()
    instrumentPage(page)
    await page.waitForURL(url => url.href.startsWith('opensquilla-app://desktop/chat'), { timeout: 180000 })
    report.desktopGateway = await poll(async () => {
      const status = await page.evaluate(() => window.opensquillaDesktop.getGatewayStatus())
      const connection = await page.evaluate(async () => {
        const value = await window.opensquillaDesktop.getGatewayConnection()
        return { schemaVersion: value.schemaVersion, revision: value.revision, status: value.status, instanceId: value.instanceId, profileFingerprint: value.profileFingerprint, httpUrl: value.httpUrl, hasAuthToken: Boolean(value.authToken) }
      })
      if (status.status !== 'ready' || connection.status !== 'ready') return null
      assert.equal(status.owned, true, 'Desktop attached to an unowned Gateway.')
      assert.equal(status.port, port)
      assert.equal(connection.httpUrl, `http://127.0.0.1:${port}`)
      assert.equal(connection.profileFingerprint, desktopProfileFingerprint(profile))
      assert.ok(connection.hasAuthToken && connection.instanceId, 'Owned Desktop Gateway auth/instance proof missing.')
      return { status, connection }
    }, 'isolated owned and authenticated Desktop Gateway', 60000)
  }
  await page.locator('.chat-textarea').waitFor({ state: 'visible', timeout: 60000 })
  await screenshot('ready')
  if (surface === 'desktop') {
    await newTask()
    const key = await send('create')
    await checkpoint('desktop-created', key, fixture.originalCss, '正文链接测试', 'rgb(23, 107, 135)', 1)
    await annotateHeading()
    await send('edit', key)
    await checkpoint('desktop-edited', key, fixture.originalCss.replace('#176b87', '#7c3aed'), '正文链接测试', 'rgb(124, 58, 237)', 2)
    const requests = provider.snapshot().requests.filter(row => row.scenario === 'edit')
    assert.ok(requests.some(row => row.imageCount > 0 && row.annotationCount > 0), 'Real annotated edit did not reach the model with image and annotation context.')
    report.annotation = { imageCount: Math.max(...requests.map(row => row.imageCount)), annotationCount: Math.max(...requests.map(row => row.annotationCount)) }
  } else {
    await newTask()
    const a = await send('createA')
    await checkpoint('web-A-created', a, fixture.isolationCss.initialA, 'TASK_A 隔离任务A', 'rgb(23, 107, 135)', 1)
    await newTask()
    const b = await send('createB')
    assert.notEqual(a, b)
    const baselineB = await checkpoint('web-B-created', b, fixture.isolationCss.initialB, 'TASK_B 隔离任务B', 'rgb(23, 107, 135)', 1)
    assert.notEqual(stable.get(a).binding.root, stable.get(b).binding.root, 'New tasks share a workspace.')
    await selectTask(a)
    for (const [name, color, count] of [['editA1', 'rgb(124, 58, 237)', 2], ['editA2', 'rgb(21, 128, 61)', 3]]) {
      await send(name, a)
      await checkpoint(`web-A-${name}`, a, fixture.isolationCss[name], 'TASK_A 隔离任务A', color, count)
      assert.equal(await readFile(join(stable.get(b).binding.root, fixture.stylesheet), 'utf8'), fixture.isolationCss.initialB, 'A edit changed B bytes.')
    }
    const oldPid = gateway.pid
    await stopGateway()
    await startGateway()
    assert.notEqual(gateway.pid, oldPid, 'Gateway was not actually restarted.')
    report.restart = { oldPid, newPid: gateway.pid, sameProfile: profile }
    await page.reload()
    await page.locator('.chat-textarea').waitFor({ state: 'visible', timeout: 60000 })
    await selectTask(a)
    await checkpoint('web-A-after-restart', a, fixture.isolationCss.editA2, 'TASK_A 隔离任务A', 'rgb(21, 128, 61)', 3)
    await send('editA3', a)
    await checkpoint('web-A-editA3', a, fixture.isolationCss.editA3, 'TASK_A 隔离任务A', 'rgb(220, 38, 38)', 4)
    await send('resumeA', a)
    await checkpoint('web-A-resumed', a, fixture.isolationCss.editA3, 'TASK_A 隔离任务A', 'rgb(220, 38, 38)', 4)
    await selectTask(b)
    const finalB = await checkpoint('web-B-unchanged', b, fixture.isolationCss.initialB, 'TASK_B 隔离任务B', 'rgb(23, 107, 135)', 1)
    assert.deepEqual(finalB.files, baselineB.files, 'B files changed across A edits/restart.')
    assert.deepEqual(finalB.revisions, baselineB.revisions)
    report.coreHtmlAcceptance = 'passed: two isolated roots, A same Document/four revisions, B unchanged, restart/resume, zero publications/deliverables'
    await persist()
    // Child TXT has its own evidence boundary: normal non-HTML delivery is legal.
    await selectTask(a)
    await send('spawnA', a)
    const state = await evidence()
    const spawned = provider.snapshot().spawned
    assert.equal(spawned.length, 1)
    const child = state.sessions.find(row => row.session_key === spawned[0].session_key)
    assert.ok(child, 'Real child session is missing.')
    assert.deepEqual(JSON.parse(child.execution_workspace), stable.get(a).binding, 'Child did not inherit the parent execution workspace.')
    assert.equal(await readFile(join(stable.get(a).binding.root, 'child.txt'), 'utf8'), fixture.childFixture.content)
    assert.ok(!await exists(join(stable.get(b).binding.root, 'child.txt')), 'Child wrote into task B.')
    assert.equal(await readFile(join(stable.get(a).binding.root, fixture.stylesheet), 'utf8'), fixture.isolationCss.editA3)
    assert.equal(await readFile(join(stable.get(b).binding.root, fixture.stylesheet), 'utf8'), fixture.isolationCss.initialB)
    assert.equal(provider.snapshot().completed.spawnA, 0, 'Parent spawning turn must yield normally.')
    assert.ok(!state.deliverables.some(ref => /html/i.test(ref.mime) || /\.html?$/i.test(ref.name)), 'Child workflow implicitly delivered HTML.')
    assert.ok(!state.document_publications.some(row => [stable.get(a).documentId, stable.get(b).documentId].includes(row.document_id)), 'Child workflow published a working HTML Document.')
    await assertDefaultUntouched(state)
    const childTranscript = state.transcript_entries.filter(row => row.session_id === child.session_id)
    // Canonical transcript tool_calls contains content blocks, including the
    // result; it is not the provider's function/arguments wire representation.
    const childBlocks = childTranscript.flatMap(row => JSON.parse(row.tool_calls || '[]'))
    const childWrites = childBlocks.filter(block => block.type === 'tool_use'
      && block.name === 'write_file' && block.input?.path === 'child.txt'
      && block.input.content === fixture.childFixture.content)
    assert.equal(childWrites.length, 1, 'Persisted child transcript must attribute exactly one real child.txt write to the child.')
    const writeReceipts = childBlocks.filter(block => block.type === 'tool_result'
      && block.name === 'write_file' && block.tool_use_id === childWrites[0].tool_use_id)
    assert.equal(writeReceipts.length, 1, 'Exactly one persisted result must match the child write tool_use_id.')
    const writeReceipt = writeReceipts[0]
    assert.equal(writeReceipt.is_error, false, 'Persisted child write failed.')
    assert.equal(writeReceipt.result, `Written ${Buffer.byteLength(fixture.childFixture.content)} bytes to ${join(stable.get(a).binding.root, 'child.txt')}`, 'Child write receipt must identify the exact parent workspace file and byte count.')
    report.child = { spawned, child, childTranscript, writeReceipt, completed: provider.snapshot().completed, publications: state.document_publications, deliverables: state.deliverables, revisionsAfterChild: state.artifact_revisions, fileSha256: hash(await readFile(join(stable.get(a).binding.root, 'child.txt'))) }
    await screenshot('web-child-complete')
  }
  assert.equal(provider.snapshot().errors.length, 0)
  report.functionalAcceptance = 'passed'
  assert.ok(report.hydrateContract.checked > 0, 'No real hydrate responses were observed.')
  assert.ok(report.hydrateContract.legacyRunModeResponses > 0, 'The Safe-mode v3 compatibility path was not exercised.')
  assert.equal(report.cleanAcceptance, true, 'Functional journey completed, but hydrate Contract diagnostics prevent clean acceptance; see hydrateContract.')
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.firstFailure ||= { phase: report.phase, name: error.name, message: error.message, stack: error.stack }
  process.exitCode = 1
  report.provider = provider?.snapshot()
  await persist()
  await readSessionDiagnostics().then(value => { report.sessionDiagnostics = value }).catch(error => { report.sessionDiagnosticsError = error.name })
  await screenshot('first-failure').catch(failure => { report.failureScreenshotError = failure.message })
  await evidence().then(state => { report.failureDatabase = state }).catch(failure => { report.failureDatabaseError = failure.message })
  console.error(error.stack || error.message)
} finally {
  for (const [name, close] of [
    ['browser', () => browser?.close()],
    ['electron', async () => {
      if (!app) return
      const result = await closeElectronWithDeadline({ app, phase: 'workspace-inline-preview', diagnostics: async () => ({ output }), timeoutMs: 100000 })
      if (!result.closed) throw result.error
      await waitFor(async () => !await healthy(), 'owned Desktop Gateway shutdown', 5000)
      return result
    }],
    ['gateway', stopGateway],
    ['provider', async () => { if (provider) { report.provider = provider.snapshot(); await provider.close() } }],
  ]) {
    try { const result = await close(); report.cleanup.push({ name, ok: true, result }) } catch (error) {
      report.cleanup.push({ name, ok: false, error: error.message }); report.status = 'failed'; process.exitCode = 1
      report.firstFailure ||= { phase: 'cleanup', message: error.message }
    }
    await persist()
  }
  for (const stream of logStreams) await new Promise(done => stream.end(done))
  if (!report.cleanAcceptance && report.status === 'passed') {
    report.status = 'failed'
    process.exitCode = 1
    report.firstFailure ||= { phase: 'contract-diagnostics', message: 'Hydrate Contract diagnostics prevent clean acceptance.' }
  }
  report.finishedAt = new Date().toISOString()
  await persist()
  console.log(`${report.status}: ${join(output, 'report.json')}`)
}
