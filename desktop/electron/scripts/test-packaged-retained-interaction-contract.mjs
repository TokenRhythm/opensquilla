import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, join, relative, resolve, sep } from 'node:path'
import { test } from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { AUDIT_MARKER, AUDIT_PURPOSE, assertNoExistingDesktop, assertPreservedInputs, assertStopEvidence, auditMessages, parseAuditManifest, parseSyntheticCredential, sha256, verifyAuditInputs } from './fixtures/packaged-retained-interaction/contract.mjs'
import { installRetainedRpcProbe } from './fixtures/packaged-retained-interaction/browser-probe.mjs'
import { startRetainedProvider } from './fixtures/packaged-retained-interaction/provider.mjs'

// Pure fixture/loopback tests: never execute the dummy .exe, Electron, Gateway,
// an installer, or a real profile. These results are not native acceptance.
const credential = () => ({ provider: 'ollama', encryption: 'plain', apiKeyEnv: '', encryptedApiKey: '', searchApiKeyEnv: '', encryptedSearchApiKey: '', modelRoutingMode: 'direct', routerMode: 'disabled', model: 'opensquilla-release-session-recovery-smoke', baseUrl: 'http://127.0.0.1:11434' })
async function fixture(t, temporaryDirectory = tmpdir()) {
  // Hosted runners can expose TEMP through an alias or redirected parent.
  // Allocate canonical fixture paths without weakening the audit's link guard.
  const temporaryRoot = await realpath(temporaryDirectory)
  const root = await mkdtemp(join(temporaryRoot, 'opensquilla-retained-contract-'))
  t.after(async () => {
    // Delete only this call's newly allocated temporary root, using one API.
    const suffix = relative(resolve(temporaryRoot), resolve(root))
    assert.ok(suffix.startsWith('opensquilla-retained-contract-') && !suffix.includes(sep))
    await rm(root, { recursive: true, force: true })
  })
  const userDataDir = join(root, 'synthetic-profile')
  const executablePath = join(root, 'synthetic-install', 'OpenSquilla.exe')
  for (const dir of [join(userDataDir, 'opensquilla/workspace'), join(userDataDir, 'opensquilla/state'), dirname(executablePath)]) await mkdir(dir, { recursive: true })
  const credentialBytes = JSON.stringify(credential())
  await writeFile(executablePath, 'THIS IS INERT TEST DATA, NEVER EXECUTE')
  await writeFile(join(userDataDir, 'desktop-credential.json'), credentialBytes)
  await writeFile(join(userDataDir, 'opensquilla/config.toml'), 'synthetic = true\n')
  const manifest = {
    schemaVersion: 1, purpose: AUDIT_PURPOSE, auditId: 'a'.repeat(32), seedLabel: 'signed-update-audit',
    userDataDir, executablePath, expectedVersion: '0.5.6', sourceSha: 'b'.repeat(40),
    executableSha256: sha256(await readFile(executablePath)), credentialSha256: sha256(credentialBytes), configSha256: sha256('synthetic = true\n'),
  }
  const manifestPath = join(userDataDir, AUDIT_MARKER)
  await writeFile(manifestPath, JSON.stringify(manifest))
  return { root, manifest, manifestPath, outputDir: join(root, 'evidence') }
}

test('pinned synthetic inputs validate without rewriting credentials/config', async t => {
  const f = await fixture(t)
  const plan = await verifyAuditInputs(f.manifestPath, f.outputDir)
  assert.equal(plan.provider.model, credential().model)
  await assertPreservedInputs(plan)
  assert.equal(await readFile(plan.credentialPath, 'utf8'), JSON.stringify(credential()))
})
test('redirected temporary parents yield canonical fixtures while audit aliases remain rejected', async t => {
  const parent = await fixture(t)
  const target = join(parent.root, 'temporary-target')
  const alias = join(parent.root, 'temporary-alias')
  await mkdir(target)
  await symlink(target, alias, process.platform === 'win32' ? 'junction' : 'dir')
  const f = await fixture(t, alias)
  assert.equal(dirname(f.root), await realpath(target))
  await verifyAuditInputs(f.manifestPath, f.outputDir)
  const redirectedManifest = join(alias, basename(f.root), 'synthetic-profile', AUDIT_MARKER)
  await assert.rejects(verifyAuditInputs(redirectedManifest, f.outputDir), /must not traverse redirected paths/)
})
test('PowerShell UTF-8 BOM is accepted but still included in preserved byte hashes', async t => {
  const f = await fixture(t)
  const credentialPath = join(f.manifest.userDataDir, 'desktop-credential.json')
  const bytes = Buffer.from('\uFEFF' + JSON.stringify(credential()))
  await writeFile(credentialPath, bytes)
  f.manifest.credentialSha256 = sha256(bytes)
  await writeFile(f.manifestPath, '\uFEFF' + JSON.stringify(f.manifest))
  const plan = await verifyAuditInputs(f.manifestPath, f.outputDir)
  await assertPreservedInputs(plan)
  assert.equal(sha256(await readFile(credentialPath)), sha256(bytes))
})
test('marker identity, source, version and path boundaries fail closed', async t => {
  const f = await fixture(t)
  for (const change of [{ purpose: 'real-profile' }, { auditId: 'bad' }, { sourceSha: 'main' }, { expectedVersion: '0.5.6-dev' }, { executableSha256: '' }]) {
    assert.throws(() => parseAuditManifest({ ...f.manifest, ...change }, f.manifestPath, f.outputDir))
  }
  assert.throws(() => parseAuditManifest(f.manifest, join(f.root, AUDIT_MARKER), f.outputDir))
  for (const output of [f.manifest.userDataDir, join(f.manifest.userDataDir, 'evidence'), dirname(f.manifest.executablePath), f.root]) {
    assert.throws(() => parseAuditManifest(f.manifest, f.manifestPath, output))
  }
})
test('changed executable/credential/config and replaced marker cannot pass', async t => {
  const f = await fixture(t)
  const plan = await verifyAuditInputs(f.manifestPath, f.outputDir)
  for (const path of [plan.executablePath, plan.credentialPath, plan.configPath, plan.manifestPath]) {
    const original = await readFile(path)
    await writeFile(path, Buffer.concat([original, Buffer.from(' ')]))
    await assert.rejects(assertPreservedInputs(plan))
    if (path !== plan.manifestPath) await assert.rejects(verifyAuditInputs(f.manifestPath, f.outputDir))
    await writeFile(path, original)
  }
  await assertPreservedInputs(plan)
})
test('only credential-free fixed synthetic models at explicit IPv4 loopback are permitted', () => {
  assert.equal(parseSyntheticCredential(credential()).baseUrl, 'http://127.0.0.1:11434')
  for (const baseUrl of ['http://example.com:11434', 'https://127.0.0.1:11434', 'http://localhost:11434', 'http://127.0.0.1', 'http://127.0.0.1:0', 'http://user:password@127.0.0.1:11434', 'http://127.0.0.1:11434/v1', 'http://127.0.0.1:11434/?token=secret']) {
    assert.throws(() => parseSyntheticCredential({ ...credential(), baseUrl }))
  }
  for (const change of [{ encryptedApiKey: 'nonempty' }, { searchApiKeyEnv: 'SEARCH_KEY' }, { encryptedSearchApiKey: 'nonempty' }, { provider: 'openai' }, { model: 'real-model' }, { encryption: 'safeStorage' }, { routerMode: 'enabled' }]) {
    assert.throws(() => parseSyntheticCredential({ ...credential(), ...change }))
  }
  assert.throws(() => parseSyntheticCredential({ ...credential(), encryptedApiKey: 'private-marker-must-not-appear' }), error => !error.message.includes('private-marker-must-not-appear'))
})

test('Desktop process guard uses a bounded native census and parses full quoted CSV', async () => {
  const windowsDirectory = join(tmpdir(), 'Windows')
  let calls = 0
  await assertNoExistingDesktop(async (command, args, options) => {
    calls += 1
    assert.equal(command, join(windowsDirectory, 'System32', 'tasklist.exe'))
    assert.deepEqual(args, ['/FO', 'CSV', '/NH'])
    assert.deepEqual(options, { windowsHide: true, timeout: 15_000 })
    return { stdout: '\r\n"System Idle Process","0","Services","0","8 K"\r\n"Other,""quoted"".exe","42","Console","1","26,228 K"\r\n', stderr: '' }
  }, windowsDirectory)
  assert.equal(calls, 1)
})

test('Desktop process guard rejects any existing Desktop and every incomplete census', async () => {
  const windowsDirectory = join(tmpdir(), 'Windows')
  const row = '"System","4","Services","0","8 K"'
  for (const name of ['OpenSquilla.exe', 'opensquilla.exe', 'OPENSQUILLA.EXE']) {
    await assert.rejects(assertNoExistingDesktop(async () => ({ stdout: `${row}\r\n"${name}","42","Console","1","26,228 K"`, stderr: '' }), windowsDirectory), /Close existing Desktop/)
  }
  for (const stdout of ['', '\r\n', 'INFO: No tasks are running which match the specified criteria.', `${row}\r\nbroken`, `${row}\r\n\r\n${row}`, '"System","not-a-pid","Services","0","8 K"', '"System","4","Services","0"', `${row},"extra"`, '"","4","Services","0","8 K"', '"System","4","Services","0","8 K']) {
    await assert.rejects(assertNoExistingDesktop(async () => ({ stdout, stderr: '' }), windowsDirectory), /Windows process census/)
  }
  await assert.rejects(assertNoExistingDesktop(async () => ({ stdout: row, stderr: 'query failed' }), windowsDirectory), /unexpected stderr/)
  for (const failure of [Object.assign(new Error('tasklist exited unsuccessfully'), { code: 1 }), Object.assign(new Error('tasklist timed out'), { killed: true, signal: 'SIGTERM' })]) {
    let calls = 0
    await assert.rejects(assertNoExistingDesktop(async () => { calls += 1; throw failure }, windowsDirectory), error => error === failure)
    assert.equal(calls, 1, 'Process census failures must not retry or pass')
  }
  await assert.rejects(assertNoExistingDesktop(async () => assert.fail('Invalid SystemRoot must not spawn a process'), 'relative-directory'), /absolute SystemRoot/)
})

const stopSnapshot = () => ({ requests: [{ method: 'chat.abort', params: { sessionKey: 'session-a', taskId: 'task-a', source: 'webui_stop', scope: 'task' } }], events: [{ taskId: 'task-a', reason: 'aborted' }] })
test('Stop requires one real UI abort and the matching cancelled task', () => {
  assert.equal(assertStopEvidence(stopSnapshot(), 'session-a').taskId, 'task-a')
  for (const mutate of [
    s => s.requests.push(s.requests[0]),
    s => { s.requests[0].params.sessionKey = 'session-b' },
    s => { s.requests[0].params.source = 'fixture' },
    s => { s.requests[0].params.scope = 'session' },
    s => { s.requests[0].params.taskId = '' },
    s => { s.events = [] },
    s => { s.events[0].taskId = 'task-b' },
    s => { s.events[0].sessionKey = 'session-b' },
    s => { s.events[0].reason = 'completed' },
  ]) {
    const value = stopSnapshot(); mutate(value)
    assert.throws(() => assertStopEvidence(value, 'session-a'))
  }
})
test('browser observation forwards every original frame and records terminal event metadata only', () => {
  class Socket {
    sent = []; listeners = []
    send(value) { this.sent.push(value); return 'original-return' }
    addEventListener(name, callback) { assert.equal(name, 'message'); this.listeners.push(callback) }
    incoming(value) { for (const callback of this.listeners) callback({ data: JSON.stringify(value) }) }
  }
  const context = { WebSocket: Socket }
  runInNewContext(`(${installRetainedRpcProbe.toString()})()`, context)
  const socket = new Socket()
  const frame = JSON.stringify({ type: 'req', method: 'chat.abort', params: stopSnapshot().requests[0].params })
  assert.equal(socket.send(frame), 'original-return')
  assert.equal(socket.send('not json'), 'original-return')
  assert.deepEqual(socket.sent, [frame, 'not json'])
  assert.equal(socket.listeners.length, 1)
  socket.incoming({ type: 'res', payload: { task_id: 'task-a', cancelled: true } })
  assert.equal(context.__retainedAuditRpc.events.length, 0, 'An abort acknowledgement alone is not terminal evidence')
  socket.incoming({ type: 'event', event: 'session.event.done', payload: { session_key: 'session-a', turn_id: 'task-a', reason: 'aborted', content: 'do not persist arbitrary transcript' } })
  assert.equal(assertStopEvidence(context.__retainedAuditRpc, 'session-a').taskId, 'task-a')
  assert.equal(JSON.stringify(context.__retainedAuditRpc).includes('do not persist'), false)
})

async function providerFixture(t) {
  const messages = auditMessages('c'.repeat(32))
  const token = `OPENSQUILLA_RETAINED_${randomBytes(32).toString('hex')}`
  const sentinelPath = join(tmpdir(), 'inert-provider-test-sentinel.txt')
  const model = credential().model
  const provider = await startRetainedProvider({ baseUrl: 'http://127.0.0.1:0', model, messages, sentinelPath, sentinelTokenSha256: sha256(token) })
  t.after(() => provider.close())
  const post = (prompt, extra = {}, signal) => fetch(`${provider.baseUrl}/api/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, messages: [{ role: 'user', content: prompt }], ...extra }), signal })
  return { provider, messages, token, sentinelPath, model, post }
}
async function until(check) {
  for (let i = 0; i < 100; i += 1) { if (check()) return; await delay(20) }
  assert.fail('Fixture observation did not settle')
}
test('provider accepts one production time prefix on the current user turn', async t => {
  const { provider, messages, post } = await providerFixture(t)
  const response = await post(`[2026-09-10T16:00+08:00 Thu Asia/Shanghai]\n${messages.first}`)
  assert.equal(response.status, 200)
  assert.match(await response.text(), /RETAINED_FIRST_OK/)
  assert.equal(provider.snapshot().first, 1)
})
const runtimeSuffix = '\n\n[Runtime context for this turn]\nCurrent local date/time: 2026-09-10T16:13+08:00 (Thu)\nTime zone / location hint: 中国标准时间\nUse this runtime context for questions about the current date, time, or local time zone. Do not treat it as a user request.'
for (const textBlocks of [false, true]) {
  test(`provider accepts the production runtime suffix for ${textBlocks ? 'Ollama text blocks' : 'string content'}`, async t => {
    const { provider, messages, post } = await providerFixture(t)
    // Agent appends runtime context as a second text block for block content;
    // _build_ollama_messages serializes those blocks with exactly one space.
    const content = textBlocks ? [messages.first, runtimeSuffix].join(' ') : messages.first + runtimeSuffix
    const response = await post(content)
    assert.equal(response.status, 200)
    assert.match(await response.text(), /RETAINED_FIRST_OK/)
    assert.equal(provider.snapshot().first, 1)
  })
}
const executionSuffix = (model, identity = { kind: 'single_model', provider: 'ollama', model }) =>
  `\nCurrent response execution: ${JSON.stringify(identity)}`
const wrappedTurn = (prompt, model) =>
  `[2026-09-10T16:00+08:00 Thu Asia/Shanghai]\n${[prompt, runtimeSuffix + executionSuffix(model)].join(' ')}`

for (const textBlocks of [false, true]) {
  test(`provider accepts the complete direct Ollama runtime execution envelope for ${textBlocks ? 'text blocks' : 'string content'}`, async t => {
    const { provider, messages, model, post } = await providerFixture(t)
    const content = textBlocks ? wrappedTurn(messages.first, model)
      : messages.first + runtimeSuffix + executionSuffix(model)
    const response = await post(content)
    assert.equal(response.status, 200)
    assert.match(await response.text(), /RETAINED_FIRST_OK/)
    assert.equal(provider.snapshot().first, 1)
  })
}

test('provider rejects wrong, duplicate, malformed or extra execution identity', async t => {
  const { provider, messages, model, post } = await providerFixture(t)
  const expected = { kind: 'single_model', provider: 'ollama', model }
  const prefix = messages.first + ' ' + runtimeSuffix
  for (const identity of [
    { ...expected, kind: 'multi_model_fusion' },
    { ...expected, provider: 'openai' },
    { ...expected, model: 'wrong-model' },
    { ...expected, extra: true },
  ]) {
    const response = await post(prefix + executionSuffix(model, identity))
    assert.equal(response.status, 422)
    await response.text()
  }
  for (const content of [
    prefix + executionSuffix(model) + '\nextra',
    prefix + executionSuffix(model) + executionSuffix(model),
    prefix + '\nCurrent response execution: {bad json}',
    prefix + '\nCurrent response execution: ' + JSON.stringify(expected).replace('"kind":', '"kind":"wrong","kind":'),
    messages.first + executionSuffix(model),
    auditMessages('d'.repeat(32)).first + ' ' + runtimeSuffix + executionSuffix(model),
  ]) {
    const response = await post(content)
    assert.equal(response.status, 422)
    await response.text()
  }
  assert.equal(provider.snapshot().first, 0)
})

test('provider rejects quoted, repeated, malformed or historical audit prompts', async t => {
  const { provider, messages, post } = await providerFixture(t)
  const prefix = '[2026-09-10T16:00+08:00 Thu Asia/Shanghai]\n'
  for (const content of [`quoted ${messages.first}`, `${messages.first}\n${messages.tool}`,
    `${prefix}${prefix}${messages.first}`, `[bad timestamp]\n${messages.first}`, 'unrelated current turn',
    messages.first + runtimeSuffix + '\nextra', messages.first + runtimeSuffix + runtimeSuffix,
    runtimeSuffix + messages.first, messages.first + runtimeSuffix.replace('(Thu)', '(invalid)'),
    messages.first + '  ' + runtimeSuffix, messages.first + ' ',
    auditMessages('d'.repeat(32)).first + ' ' + runtimeSuffix]) {
    const response = await post(content, { messages: [
      { role: 'user', content: messages.first }, { role: 'assistant', content: messages.firstAnswer },
      { role: 'user', content },
    ] })
    assert.equal(response.status, 422)
    await response.text()
  }
  assert.equal(provider.snapshot().first, 0)
})
test('actual loopback provider validates read_file nonce rather than fabricating tool success', async t => {
  const { provider, messages, token, sentinelPath, model, post } = await providerFixture(t)
  assert.match(await (await post(wrappedTurn(messages.first, model))).text(), /RETAINED_FIRST_OK/)
  const tools = [{ type: 'function', function: { name: 'read_file' } }]
  const request = await (await post(wrappedTurn(messages.tool, model), { tools })).text()
  const toolCalls = JSON.parse(request.split('\n')[0]).message.tool_calls
  assert.equal(toolCalls[0].function.arguments.path, sentinelPath)
  const response = await post(messages.tool, { tools, messages: [
    { role: 'user', content: wrappedTurn(messages.tool, model) },
    { role: 'assistant', content: '', tool_calls: toolCalls },
    { role: 'tool', tool_name: 'read_file', content: `Fixture tool protocol only: ${token}` },
  ] })
  assert.match(await response.text(), /RETAINED_TOOL_OK/)
  assert.equal(provider.snapshot().toolResults, 1)
  assert.deepEqual(provider.snapshot().errors, [])
})
test('wrong file content cannot pass the provider tool protocol check', async t => {
  const { provider, messages, sentinelPath, post } = await providerFixture(t)
  await (await post(messages.tool, { tools: [{ function: { name: 'read_file' } }] })).text()
  const response = await post(messages.tool, { messages: [
    { role: 'user', content: messages.tool },
    { role: 'assistant', tool_calls: [{ function: { name: 'read_file', arguments: { path: sentinelPath } } }] },
    { role: 'tool', tool_name: 'read_file', content: `OPENSQUILLA_RETAINED_${'0'.repeat(64)}` },
  ] })
  assert.equal(response.status, 422)
  await response.text()
  assert.match(provider.snapshot().errors[0], /unpredictable sentinel/)
})
test('held stream has no terminal chunk; actual connection cancellation enables follow-up', async t => {
  const { provider, messages, model, post } = await providerFixture(t)
  const abort = new AbortController()
  const response = await post(wrappedTurn(messages.stop, model), {}, abort.signal)
  const reader = response.body.getReader()
  const chunk = await reader.read()
  assert.match(new TextDecoder().decode(chunk.value), /"done":false/)
  assert.equal(provider.snapshot().cancelledBeforeCleanup, 0)
  abort.abort()
  await reader.cancel().catch(() => {})
  await until(() => provider.snapshot().cancelledBeforeCleanup === 1)
  assert.match(await (await post(wrappedTurn(messages.afterStop, model))).text(), /RETAINED_AFTER_STOP_OK/)
  assert.match(await (await post(wrappedTurn(messages.restart, model))).text(), /RETAINED_RESTART_OK/)
  assert.deepEqual(provider.snapshot().errors, [])
})
test('fixture cleanup is never credited as user Stop', async t => {
  const { provider, messages, post } = await providerFixture(t)
  const response = await post(messages.stop)
  const reader = response.body.getReader()
  await reader.read()
  await provider.close()
  await reader.cancel().catch(() => {})
  assert.equal(provider.snapshot().cancelledBeforeCleanup, 0)
})
test('follow-up before a real cancellation and unadvertised tools are rejected', async t => {
  const { provider, messages, post } = await providerFixture(t)
  for (const prompt of [messages.afterStop, messages.tool]) {
    const response = await post(prompt)
    assert.equal(response.status, 422)
    await response.text()
  }
  assert.equal(provider.snapshot().errors.length, 2)
})
