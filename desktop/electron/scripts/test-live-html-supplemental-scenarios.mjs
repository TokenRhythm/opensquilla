import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { createContext, runInContext } from 'node:vm'
import { createSupplementalClient } from './live-html-supplemental-client.mjs'
import { runSupplementalScenario } from './live-html-supplemental-scenarios.mjs'

const snapshot = (sha = 'A', revision = 'r1', audit = [], history = [{ revision_id: revision, generation: 1, parent_revision_id: null }], generation = Math.max(...history.map(row => row.generation))) => ({
  complete: true,
  working: [{ documentId: 'doc', entrypoint: 'supplemental.html', files: [{ path: 'supplemental.html', sha256: sha }] }],
  published: [{ entrypoint: 'supplemental.html', headDocumentIds: ['doc'], files: [{ path: 'supplemental.html', sha256: sha }] }],
  documents: [{ document_id: 'doc', head_revision_id: revision, generation }],
  revisions: history.map(row => ({ document_id: 'doc', ...row })), audit, publications: [],
})
function fixture(overrides = {}) {
  const checks = [], calls = [], original = snapshot()
  const second = [...original.revisions, { revision_id: 'r2', generation: 2, parent_revision_id: 'r1' }]
  let phase = 'generation', drafts = 0
  const client = {
    fixtureUrl: 'http://127.0.0.1:18799/independent-pages.html',
    record: event => calls.push(event), check: result => checks.push(result),
    async send(name, prompt, options) {
      phase = name; calls.push({ send: name, options })
      return { taskId: name, terminalStatus: name === 'interruptible-change' ? 'cancelled' : 'succeeded', cancellation: 'stop-clicked-after-file-change', filesChangedBeforeCancel: true, annotationCount: drafts, attachmentCount: 1, previousAnswerIds: ['prior'] }
    },
    async openArtifact() {}, async capture() {}, async verifyButtonInteraction() { return true },
    async annotate() { drafts += 1 }, async annotationCount() { return drafts }, async hasButton() { return true },
    async hasFinalAnswer(turn) { assert.ok(['answer-only', 'nonvision-image-request'].includes(turn.taskId)); return true },
    async material() {
      if (phase === 'interruptible-change' || phase === 'reentered') return snapshot('B')
      if (phase === 'new-version') return snapshot('B', 'r2', [], second)
      if (phase === 'restored') return snapshot('A', 'r1', [], second)
      if (phase === 'edit-restored-version') return snapshot('C', 'r3', [], [...second, { revision_id: 'r3', generation: 3, parent_revision_id: 'r1' }])
      return original
    },
    async reenter() { phase = 'reentered'; calls.push({ reentered: true }) },
    async restoreOriginalVersion(value) {
      assert.equal(value, original); phase = 'restored'; calls.push({ restoredExactSnapshot: true })
      return { documentId: 'doc', revisionId: 'r1', revisionCount: 2, generation: 2,
        repeated: { noOp: true, publiclyListed: false, stateUnchanged: true, distinctRequest: true } }
    },
    async visibleTextIncludes(text) { return phase === 'new-version' ? text === '暮色研究' : text === '海岬研究' },
    async savePageAttachment() { return '/synthetic/reference.png' }, async attachFile() {}, async fillComposer() {},
    async selectReviewedNonvisionModel() { return { selected: true, supportsVision: false, model: 'reviewed-synthetic-model' } },
    async imageAdmission() { return { blocked: true, noticeVisible: true, sendDisabled: true } },
    async finalAnswer() { return '' },
    ...overrides,
  }
  return { client, checks, calls, original }
}
const passed = checks => checks.every(check => check.passed)

const supplementalSource = await readFile(new URL('./live-html-supplemental-client.mjs', import.meta.url), 'utf8')
const overlayStart = supplementalSource.indexOf('const overlay = await wait(async () => app.evaluate(')
const overlayBodyStart = overlayStart + 'const overlay = await wait(async () => app.evaluate('.length
const overlayEnd = supplementalSource.indexOf(', { previewId: selectedId, body: text }),', overlayBodyStart)
assert.ok(overlayStart >= 0 && overlayEnd > overlayBodyStart)
const inspectOverlay = runInContext(`(${supplementalSource.slice(overlayBodyStart, overlayEnd)})`, createContext({}))
function supplementalOverlayFixture({ script = { filePath: '/app/dist/native-workbench-annotation-overlay-preload.cjs', type: 'frame' }, getter = true, visible = true, foreign = false, url = 'data:text/html;charset=utf-8,synthetic', ready = true, duplicate = false } = {}) {
  const owner = { id: 1, isFocused: () => true, contentView: { children: [] } }
  const preview = { id: 2, isDestroyed: () => false, getOwnerBrowserWindow: () => owner }
  const writes = []
  let value = ''
  const overlay = { id: 3, isDestroyed: () => false,
    getOwnerBrowserWindow: () => foreign ? { id: 99 } : owner,
    getURL: () => url,
    getLastWebPreferences: () => { throw new Error('OBSOLETE_PREFERENCE_GETTER_USED') },
    ...(getter ? { _getPreloadScript: () => script } : {}),
    executeJavaScript: async expression => expression.includes('document.activeElement') ? ready : value,
    isFocused: () => true, focus() {}, insertText: async text => { writes.push(text); value = text }, sendInputEvent() {},
  }
  owner.contentView.children = [{ webContents: preview, getVisible: () => true }, { webContents: overlay, getVisible: () => visible }]
  if (duplicate) owner.contentView.children.push({ webContents: { ...overlay, id: 4 }, getVisible: () => true })
  return { run: () => inspectOverlay({ webContents: { fromId: id => id === 2 ? preview : null } }, { previewId: 2, body: 'Synthetic annotation' }), writes }
}
for (const filePath of ['/app/dist/native-workbench-annotation-overlay-preload.cjs', 'C:\\app\\dist\\native-workbench-annotation-overlay-preload.cjs']) test(`native overlay lookup uses actual preload identity: ${filePath[0] === 'C' ? 'Windows' : 'POSIX'}`, async () => {
  const f = supplementalOverlayFixture({ script: { filePath, type: 'frame' } })
  const result = await f.run()
  assert.equal(result.overlayId, 3); assert.equal(result.previewId, 2)
  assert.equal(result.preloadIdentity, 'native-webcontents')
  assert.deepEqual(f.writes, ['Synthetic annotation'])
})
for (const [name, options, code] of [
  ['missing native getter', { getter: false }, 'SUPPLEMENTAL_NATIVE_PRELOAD_INSPECTION_UNAVAILABLE'],
  ['invalid native metadata', { script: { filePath: 7, type: 'frame' } }, 'SUPPLEMENTAL_NATIVE_PRELOAD_METADATA_INVALID'],
  ['wrong preload type', { script: { filePath: '/app/preload.cjs', type: 'service-worker' } }, 'SUPPLEMENTAL_NATIVE_PRELOAD_METADATA_INVALID'],
  ['duplicate trusted overlay', { duplicate: true }, 'SUPPLEMENTAL_ANNOTATION_OVERLAY_AMBIGUOUS'],
]) test(`native overlay lookup rejects ${name}`, async () => {
  const f = supplementalOverlayFixture(options)
  await assert.rejects(f.run(), new RegExp(code)); assert.deepEqual(f.writes, [])
})
for (const [name, options] of [
  ['other data view preload', { script: { filePath: '/app/dist/another-preload.cjs', type: 'frame' } }],
  ['data view without preload', { script: null }],
  ['foreign owner', { foreign: true, getter: false }],
  ['hidden overlay', { visible: false }],
  ['ordinary web page', { url: 'https://example.invalid/' }],
  ['editor not ready', { ready: false }],
]) test(`native overlay lookup does not input into ${name}`, async () => {
  const f = supplementalOverlayFixture(options)
  assert.equal(await f.run(), false); assert.deepEqual(f.writes, [])
})

test('answer-only checks the answer for this accepted turn and preserves bytes and heads', async () => {
  const f = fixture(); await runSupplementalScenario('ask-without-edit', f.client)
  assert.ok(passed(f.checks)); assert.ok(f.checks.some(item => item.name === 'answer-rendered-for-current-turn'))
})
test('answer-only fails when only a previous answer exists', async () => {
  const f = fixture({ hasFinalAnswer: async () => false }); await runSupplementalScenario('ask-without-edit', f.client)
  assert.equal(passed(f.checks), false)
})
test('restore receives the exact generation-end snapshot', async () => {
  const f = fixture(); await runSupplementalScenario('restore-and-edit', f.client)
  assert.ok(passed(f.checks)); assert.ok(f.calls.some(item => item.restoredExactSnapshot))
})
for (const [label, mutate, failedCheck] of [
  ['appended restore copy', state => { state.documents[0].head_revision_id = 'copy'; state.revisions.push({ document_id: 'doc', revision_id: 'copy', generation: 3, parent_revision_id: 'r2', copied_from_revision_id: 'r1' }) }, 'restored-head-is-original-revision'],
  ['removed newer history', state => { state.revisions = state.revisions.filter(row => row.revision_id !== 'r2') }, 'restore-preserves-version-history'],
  ['lowered generation', state => { state.documents[0].generation = 1 }, 'restore-preserves-generation-highwater'],
]) test(`restore rejects ${label}`, async () => {
  const f = fixture(); const read = f.client.material; let reads = 0
  f.client.material = async () => { const state = await read(); if (++reads === 3) mutate(state); return state }
  await runSupplementalScenario('restore-and-edit', f.client)
  assert.equal(f.checks.find(item => item.name === failedCheck).passed, false)
})
for (const change of [{ noOp: false }, { publiclyListed: true }, { stateUnchanged: false }, { distinctRequest: false }]) test(`repeat restore rejects invalid receipt evidence ${JSON.stringify(change)}`, async () => {
  const f = fixture(); const restore = f.client.restoreOriginalVersion
  f.client.restoreOriginalVersion = async (...args) => { const result = await restore(...args); return { ...result, repeated: { ...result.repeated, ...change } } }
  await runSupplementalScenario('restore-and-edit', f.client)
  assert.equal(f.checks.find(item => item.name === 'repeat-current-version-is-private-noop').passed, false)
})
test('edit after restore rejects a version parented to the pre-restore head', async () => {
  const f = fixture(); const read = f.client.material; let reads = 0
  f.client.material = async () => { const state = await read(); if (++reads === 4) state.revisions.find(row => row.revision_id === 'r3').parent_revision_id = 'r2'; return state }
  await runSupplementalScenario('restore-and-edit', f.client)
  assert.equal(f.checks.find(item => item.name === 'restored-edit-creates-version-from-restored-head').passed, false)
})
test('edit after restore allows multiple ordinary saves descending from the restored head', async () => {
  const f = fixture(); const read = f.client.material; let reads = 0
  f.client.material = async () => {
    const state = await read()
    if (++reads === 4) {
      state.revisions.push({ document_id: 'doc', revision_id: 'r4', generation: 4, parent_revision_id: 'r3' })
      state.documents[0] = { document_id: 'doc', head_revision_id: 'r4', generation: 4 }
    }
    return state
  }
  await runSupplementalScenario('restore-and-edit', f.client); assert.ok(passed(f.checks))
})
for (const publiclyListed of [false, true]) test(`restore button observer checks the second receipt and hides response bodies: public no-op ${publiclyListed}`, async () => {
  const context = createContext({})
  runInContext(`globalThis.window = globalThis; class WebSocket {
    listeners = []; send() {};
    addEventListener(type, listener) { this.listeners.push(listener) }
    receive(data) { for (const listener of this.listeners) listener({ data: JSON.stringify(data) }) }
  }; globalThis.WebSocket = WebSocket; globalThis.socket = new WebSocket()`, context)
  const state = {
    artifact_documents: [{ document_id: 'doc', head_revision_id: 'r2', generation: 2, state_revision: 2 }],
    artifact_revisions: [{ document_id: 'doc', revision_id: 'r1' }, { document_id: 'doc', revision_id: 'r2' }],
  }
  let clicks = 0, previewVisits = 0
  const report = { events: [] }
  const row = {
    count: async () => 1,
    isVisible: async () => true,
    locator: selector => selector === '.artifact-document__badge' ? { count: async () => state.artifact_documents[0].head_revision_id === 'r1' ? 1 : 0 } : {
      click: async () => {
        assert.equal(selector, '[data-artifact-action="restore-revision"]'); clicks += 1
        const requestId = `wire-${clicks}`, clientRequestId = `restore-${clicks}`, changeSetId = `change-${clicks}`
        context.request = { method: 'artifacts.revisions.restore', id: requestId, params: { documentId: 'doc', revisionId: 'r1', clientRequestId } }
        context.response = { type: 'res', id: requestId, ok: true, payload: {
          document: { headRevisionId: 'r1', generation: 2, stateRevision: 3 }, revision: { id: 'r1' },
          changeSet: { validation: { no_op: clicks === 2 }, summary: 'synthetic-private-body' },
          receipt: { requestId: clientRequestId, documentId: 'doc', resultRevisionId: 'r1', changeSetId, stateRevision: 3, status: 'applied', extra: 'synthetic-private-body' },
          reasoning: 'synthetic-private-body',
        } }
        runInContext('socket.send(JSON.stringify(request)); socket.receive(response)', context)
        state.artifact_documents[0] = { document_id: 'doc', head_revision_id: 'r1', generation: 2, state_revision: 3 }
        context.list = { type: 'res', id: `list-${clicks}`, ok: true, payload: { changeSets: clicks === 1 || publiclyListed ? [{ id: changeSetId, summary: 'synthetic-private-body' }] : [] } }
        runInContext(`socket.send(JSON.stringify({method:'artifacts.changes.list',id:list.id,params:{documentId:'doc'}})); socket.receive(list)`, context)
      },
    },
  }
  const page = {
    addInitScript: async () => {},
    evaluate: async (fn, argument) => { context.argument = argument; return runInContext(`(${fn.toString()})(argument)`, context) },
    getByRole: (_role, { name }) => ({ click: async () => { if (name.test('Preview')) previewVisits += 1 } }),
    locator: selector => {
      if (selector === '.artifact-document__list-main > small') return { filter: ({ hasText }) => {
        assert.ok(hasText.test('Version 1')); assert.ok(!hasText.test('Version 12'))
        assert.ok(!hasText.test('Version 19/9/2026')); return { versionLabel: true }
      } }
      assert.equal(selector, '.artifact-document__versions > li')
      return { filter: ({ has }) => { assert.equal(has.versionLabel, true); return row } }
    },
  }
  const app = { evaluate: async () => ({ contents: [{ id: 2, visible: true, path: '/supplemental.html' }] }) }
  const client = await createSupplementalClient(app, page, { report, persist: async () => {}, readState: async () => structuredClone(state), interrupted: () => false })
  client.hasButton = async name => name === '开始体验'
  let headingReads = 0
  client.visibleTextIncludes = async text => text === '海岬研究' && ++headingReads >= 3
  if (publiclyListed) await assert.rejects(client.restoreOriginalVersion(snapshot()), /SUPPLEMENTAL_NOOP_RESTORE_PUBLIC_CHANGE/)
  else {
    const result = await client.restoreOriginalVersion(snapshot())
    assert.equal(result.repeated.noOp, true); assert.equal(result.repeated.distinctRequest, true)
  }
  assert.equal(clicks, 2); assert.equal(previewVisits, 2)
  assert.ok(headingReads >= 3, 'restore must wait for the expected heading, even when the button already exists')
  assert.equal(report.events.length, 2)
  assert.ok(!JSON.stringify(report).includes('synthetic-private-body'))
})
for (const count of [0, 1]) test(`restore keeps the existing wait deadline for ${count ? 'hidden' : 'missing'} original rows`, async t => {
  let now = 0
  t.mock.method(Date, 'now', () => now)
  const row = { count: async () => count, isVisible: async () => false }
  const page = {
    addInitScript: async () => {}, evaluate: async () => {},
    getByRole: () => ({ click: async () => {} }),
    locator: selector => selector === '.artifact-document__list-main > small'
      ? { filter: () => ({ versionLabel: true }) }
      : { filter: () => row },
  }
  const state = { artifact_documents: [{ document_id: 'doc', head_revision_id: 'r2', generation: 2 }],
    artifact_revisions: [{ document_id: 'doc', revision_id: 'r1' }, { document_id: 'doc', revision_id: 'r2' }] }
  const client = await createSupplementalClient({}, page, { report: { events: [] }, readState: async () => state,
    interrupted: () => { now += 16000; return false }, persist: async () => {} })
  await assert.rejects(client.restoreOriginalVersion(snapshot()), error =>
    error.message === 'SUPPLEMENTAL_WAIT_EXPIRED'
    && error.diagnostic.stage === 'original-version-row'
    && error.diagnostic.waitMilliseconds === 30000)
})
test('a completed task does not count as the cancellation branch', async () => {
  const f = fixture(); const send = f.client.send
  f.client.send = async (...args) => ({ ...await send(...args), terminalStatus: 'succeeded', cancellation: 'completed-before-cancel' })
  await runSupplementalScenario('cancel-and-recover', f.client)
  assert.equal(passed(f.checks), false); assert.ok(!f.calls.some(item => item.send === 'recovery'))
})
test('actual cancellation requires changed files, retains heads, reenters, then recovers', async () => {
  const f = fixture(); await runSupplementalScenario('cancel-and-recover', f.client)
  assert.ok(passed(f.checks)); assert.ok(f.calls.some(item => item.reentered))
  assert.deepEqual(f.calls.find(item => item.send === 'interruptible-change').options, { cancelWhenFilesChange: true })
})
test('a cancelled turn cannot invent a new head without a publication fact', async () => {
  let reads = 0
  const f = fixture({ material: async () => ++reads <= 2 ? snapshot() : snapshot('B', 'r2') })
  await runSupplementalScenario('cancel-and-recover', f.client)
  assert.equal(f.checks.find(item => item.name === 'cancelled-heads-match-published-facts').passed, false)
})
test('explicitly published head is allowed to survive cancellation', async () => {
  let reads = 0
  const audit = [{ event_id: 'published-1', event_type: 'document.published', document_id: 'doc', revision_id: 'r2' }]
  const f = fixture({ material: async () => ++reads <= 2 ? snapshot() : snapshot('B', 'r2', audit) })
  await runSupplementalScenario('cancel-and-recover', f.client); assert.ok(passed(f.checks))
})
for (const [label, selection, admission, valid] of [
  ['reviewed model selected and blocked', { selected: true, supportsVision: false, model: 'reviewed' }, { blocked: true, noticeVisible: true, sendDisabled: true }, true],
  ['model selection did not occur', { selected: false, supportsVision: false, model: 'reviewed' }, { blocked: true, noticeVisible: true, sendDisabled: true }, false],
  ['notice without disabled send', { selected: true, supportsVision: false, model: 'reviewed' }, { blocked: true, noticeVisible: true, sendDisabled: false }, false],
]) test(`nonvision: ${label}`, async () => {
  const f = fixture({ selectReviewedNonvisionModel: async () => selection, imageAdmission: async () => admission })
  await runSupplementalScenario('attachment-capability', f.client); assert.equal(passed(f.checks), valid)
})
test('admitted nonvision request requires an observed capability limitation', async () => {
  const f = fixture({ imageAdmission: async () => ({ blocked: false }), finalAnswer: async () => 'The button is blue.' })
  await runSupplementalScenario('attachment-capability', f.client); assert.equal(passed(f.checks), false)
})
test('admitted nonvision request can report a real capability rejection', async () => {
  const f = fixture({ imageAdmission: async () => ({ blocked: false }) }); const send = f.client.send
  f.client.send = async (...args) => ({ ...await send(...args), ...(args[0] === 'nonvision-image-request' ? { terminalStatus: 'failed', errorClass: 'image_input_unsupported' } : {}) })
  await runSupplementalScenario('attachment-capability', f.client); assert.ok(passed(f.checks))
})
for (const changedInventory of [false, true]) test(`same-URL compares full inventory and independent input/scroll state: ${changedInventory}`, async () => {
  let memoryReads = 0, inventories = 0
  const f = fixture({
    findPagesAtFixtureUrl: async () => [{ id: 1 }, { id: 2 }], activatePage: async () => {}, clickInPage: async () => {}, preparePageState: async () => {},
    pageInventory: async () => ++inventories === 2 && changedInventory ? [{ id: 1 }, { id: 2 }, { id: 3 }] : [{ id: 1 }, { id: 2 }],
    pageMemory: async () => [
      { id: 1, instanceId: 'first', note: 'first page note', count: 1, scrollY: 120 },
      { id: 2, instanceId: 'second', note: 'second page note', count: ++memoryReads === 1 ? 2 : 3, scrollY: 240 },
    ],
  })
  await runSupplementalScenario('same-url-memory', f.client); assert.equal(passed(f.checks), !changedInventory)
})

for (const [answer, valid] of [
  ['当前模型没有视觉能力，请提供图片中的文字。', true],
  ['当前模型是纯文本模型，请描述这张截图。', true],
  ['作为纯文本模型，我需要你描述截图。', true],
  ['我无法直接查看你上传的图片，请提供按钮的文字。', true],
  ['当前模型不支持图片输入，请提供截图中的文字。', true],
  ['This model is text-only. Please describe the screenshot.', true],
  ['I am a text-only model. Please describe the button.', true],
  ["I'm a text-only model. Please describe the screenshot.", true],
  ["I can't view the attached screenshot.", true],
  ['The selected model does not support image input.', true],
  ['The button is blue.', false],
  ['这个按钮是蓝色，我不支持修改页面。', false],
  ['当前模型不支持视频；图片中的按钮是蓝色。', false],
  ['I cannot click buttons in the screenshot.', false],
  ['This model is not text-only; the button is blue.', false],
  ['Text-only mode applies to exports. The button is blue.', false],
]) test(`nonvision declaration: ${valid ? 'accept' : 'reject'} ${answer}`, async () => {
  const f = fixture({ imageAdmission: async () => ({ blocked: false }), finalAnswer: async () => answer })
  const send = f.client.send
  f.client.send = async (...args) => ({ ...await send(...args), providerEventAt: 1, events: [
    { event: 'session.event.done', taskId: args[0], model: 'reviewed-synthetic-model', executionModels: ['reviewed-synthetic-model'] },
  ] })
  await runSupplementalScenario('attachment-capability', f.client)
  assert.equal(passed(f.checks), valid)
})

for (const [label, events, valid] of [
  ['selected model actually executed', [{ model: 'reviewed-synthetic-model', executionModels: ['reviewed-synthetic-model'] }], true],
  ['same model retried', [{ model: 'reviewed-synthetic-model', executionModels: ['reviewed-synthetic-model', 'reviewed-synthetic-model'] }], true],
  ['fallback final model', [{ model: 'different-model', executionModels: ['reviewed-synthetic-model', 'different-model'] }], false],
  ['another model executed before selected final model', [{ model: 'reviewed-synthetic-model', executionModels: ['different-model', 'reviewed-synthetic-model'] }], false],
  ['settings model without physical calls', [{ model: 'reviewed-synthetic-model', executionModels: [] }], false],
  ['missing execution records', [{ model: 'reviewed-synthetic-model' }], false],
  ['missing terminal model', [{ executionModels: ['reviewed-synthetic-model'] }], false],
  ['unknown execution model', [{ model: 'reviewed-synthetic-model', executionModels: [null] }], false],
  ['stale previous-turn model', [{ taskId: 'previous', model: 'reviewed-synthetic-model', executionModels: ['reviewed-synthetic-model'] }], false],
  ['unbound model event', [{ taskId: null, model: 'reviewed-synthetic-model', executionModels: ['reviewed-synthetic-model'] }], false],
]) test(`nonvision model evidence: ${label}`, async () => {
  const f = fixture({ imageAdmission: async () => ({ blocked: false }), finalAnswer: async () => '当前模型没有视觉能力。' })
  const send = f.client.send
  f.client.send = async (...args) => ({ ...await send(...args), providerEventAt: 1, events: events.map(event => ({ event: 'session.event.done', taskId: args[0], ...event })) })
  await runSupplementalScenario('attachment-capability', f.client)
  assert.equal(passed(f.checks), valid)
  assert.equal(f.calls.some(event => event.event === 'branch-not-exercised'), !valid)
})

for (const [label, providerStarted, terminalStatus, valid] of [
  ['RPC capability rejection before dispatch', false, 'rejected', true],
  ['accepted capability rejection before provider activity', false, 'failed', true],
  ['request activity with no actual model evidence', true, 'failed', false],
]) test(`nonvision rejection: ${label}`, async () => {
  const f = fixture({ imageAdmission: async () => ({ blocked: false }) }); const send = f.client.send
  f.client.send = async (...args) => ({ ...await send(...args), terminalStatus, errorClass: 'image_input_unsupported', ...(providerStarted ? { providerEventAt: 1 } : {}) })
  await runSupplementalScenario('attachment-capability', f.client)
  assert.equal(passed(f.checks), valid)
})

test('supplemental observer projects actual task-bound model events without provider bodies', async () => {
  const context = createContext({})
  runInContext(`globalThis.window = globalThis; class WebSocket {
    listeners = []; send() {};
    addEventListener(type, listener) { this.listeners.push(listener) }
    receive(data) { for (const listener of this.listeners) listener({ data: JSON.stringify(data) }) }
  }; globalThis.WebSocket = WebSocket; globalThis.socket = new WebSocket()`, context)
  const page = { addInitScript: async () => {}, evaluate: async fn => runInContext(`(${fn.toString()})()`, context) }
  await createSupplementalClient({}, page, { report: { events: [] } })
  context.frames = [
    { type: 'res', id: 'request', payload: { task_id: 'task', user_message_id: 'user' } },
    { type: 'event', event: 'session.event.done', payload: { task_id: 'previous-task', model: 'stale-model', execution_legs: [{ model: 'stale-model' }] } },
    { type: 'event', event: 'session.event.provider_activity', payload: { task_id: 'task', phase: 'requesting', message: 'private-synthetic-body' } },
    { type: 'event', event: 'session.event.provider_activity', payload: { task_id: 'task', phase: 'fallback', reasoning: 'private-synthetic-body' } },
    { type: 'event', event: 'session.event.done', payload: { task_id: 'task', model: 'selected-model', execution_legs: [
      { model: 'selected-model', kind: 'primary', reasoning: 'private-synthetic-body' },
      { model: 'fallback-model', kind: 'provider_fallback', request: { token: 'private-synthetic-body' } },
      { model: { nested: 'private-synthetic-body' } },
    ], text: 'private-synthetic-body', reasoning_content: 'private-synthetic-body' } },
  ]
  runInContext(`socket.send(JSON.stringify({method:'chat.send', id:'request', params:{sessionKey:'session',clientRequestId:'client-request'}})); for(const frame of frames)socket.receive(frame)`, context)
  const observed = JSON.parse(runInContext('JSON.stringify(window.__htmlSupplementalObservation.current)', context))
  assert.deepEqual(observed.events.map(event => event.phase).filter(Boolean), ['requesting', 'fallback'])
  assert.deepEqual(observed.events.at(-1).executionModels, ['selected-model', 'fallback-model', null])
  assert.equal(observed.events.at(-1).model, 'selected-model')
  assert.equal(observed.events.at(-1).taskId, 'task')
  assert.ok(!JSON.stringify(observed).includes('private-synthetic-body'))
  assert.ok(!JSON.stringify(observed).includes('stale-model'))
})


for (const taskId of ['nonvision-image-request', 'previous-turn', null]) test(`capability error belongs to current task: ${taskId}`, async () => {
  const f = fixture({ imageAdmission: async () => ({ blocked: false }), finalAnswer: async () => 'The button is blue.' })
  const send = f.client.send
  f.client.send = async (...args) => ({ ...await send(...args), providerEventAt: 1, events: [
    { event: 'session.event.done', taskId: args[0], model: 'reviewed-synthetic-model', executionModels: ['reviewed-synthetic-model'] },
    { event: 'session.event.error', taskId, code: 'image_input_unsupported' },
  ] })
  await runSupplementalScenario('attachment-capability', f.client)
  assert.equal(passed(f.checks), taskId === 'nonvision-image-request')
})

for (const target of [2, 3]) test(`native tab activation waits for delayed visibility for page ${target}`, async () => {
  let selected = 1, visible = 1, pending
  const tabs = { count: async () => 2, nth: index => ({
    getAttribute: async () => String(selected === index),
    click: async () => { selected = index; clearTimeout(pending); pending = setTimeout(() => { visible = index }, 50) },
  }) }
  const app = { evaluate: async () => ({ contents: [0, 1].map(index => ({ id: index + 2, visible: visible === index })) }) }
  const page = { addInitScript: async () => {}, evaluate: async () => {}, locator: () => tabs }
  const client = await createSupplementalClient(app, page, { interrupted: () => false })
  try { await client.activatePage({ id: target }); assert.equal(visible + 2, target) }
  finally { clearTimeout(pending) }
})
test('native tab activation keeps its deadline when visibility never changes', async t => {
  let now = 0
  t.mock.method(Date, 'now', () => now)
  const tabs = { count: async () => 2, nth: () => ({ getAttribute: async () => 'false', click: async () => {} }) }
  const app = { evaluate: async () => ({ contents: [{ id: 2, visible: false }, { id: 3, visible: true }] }) }
  const page = { addInitScript: async () => {}, evaluate: async () => {}, locator: () => tabs }
  const client = await createSupplementalClient(app, page, { interrupted: () => { now += 16000; return false } })
  await assert.rejects(client.activatePage({ id: 2 }), error => error.message === 'SUPPLEMENTAL_WAIT_EXPIRED'
    && error.diagnostic.stage === 'native-tab-activation' && error.diagnostic.waitMilliseconds === 30000)
})
