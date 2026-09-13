import assert from 'node:assert/strict'
import test from 'node:test'
import { entrypoint, model, originalCss, prompts, startWorkspaceInlinePreviewProvider } from './provider.mjs'

const tools = ['write_file', 'read_file', 'edit_file', 'open_workspace_preview'].map(name => ({ type: 'function', function: { name } }))
const previewResult = JSON.stringify({ documentId: 'doc_fixture', resourceId: 'document:doc_fixture', open: { resourceId: 'document:doc_fixture' }, entrypoint: `/workspace/${entrypoint}`, previewStatus: 'ready', bundleMode: 'directory' })
async function post(provider, messages, overrides = {}) {
  const response = await fetch(`${provider.baseUrl}/api/chat`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, messages, tools, ...overrides }) })
  const body = await response.text()
  return { status: response.status, parts: body.trim().split('\n').map(line => JSON.parse(line)) }
}
// Unit tests intentionally supply tool receipts. The Desktop journey must use
// real Gateway tool receipts; this helper is never imported by the provider.
function receipt(call) {
  if (call.name === 'write_file') return `Written ${call.arguments.content.length} bytes to /workspace/${call.arguments.path}`
  if (call.name === 'read_file') return originalCss.split(/(?<=\n)/).map((line, index) => `${index + 1}\t${line}`).join('')
  if (call.name === 'edit_file') return `Edited /workspace/${call.arguments.path}: replaced 7 chars with 7 chars`
  return previewResult
}
async function complete(provider, user) {
  const messages = [user]
  const called = []
  const toolIds = []
  while (true) {
    const result = await post(provider, messages)
    assert.equal(result.status, 200, JSON.stringify(provider.snapshot().errors))
    const assistant = result.parts[0].message
    assert.equal(result.parts.at(-1).done, true)
    const call = assistant.tool_calls?.[0].function
    if (!call) return { called, toolIds, answer: assistant.content }
    assert.match(assistant.tool_calls[0].id, /^fixture_[0-9a-f-]{36}$/)
    toolIds.push(assistant.tool_calls[0].id)
    called.push(call.name)
    messages.push(assistant, { role: 'tool', tool_name: call.name, content: receipt(call) })
  }
}

test('real HTTP fixture emits sequential tool calls and counts screenshot input', async t => {
  const provider = await startWorkspaceInlinePreviewProvider()
  t.after(() => provider.close())
  const created = await complete(provider, { role: 'user', content: prompts.create })
  assert.deepEqual(created.called, ['write_file', 'write_file', 'open_workspace_preview'])
  assert.match(created.answer, /`inline-preview\/index.html`/)
  const context = JSON.stringify({ resourceId: 'document:doc_fixture', annotations: [{ text: prompts.edit, selectionText: '正文链接测试' }] })
  const edited = await complete(provider, {
    role: 'user', images: ['aGVsbG8='],
    // _build_attachment_messages appends marker text after the prompt/image;
    // Agent then appends runtime context, and _build_ollama_messages joins text.
    content: [
      `[2026-09-11T23:30+08:00 Fri Asia/Shanghai]\n${prompts.edit}\n\n<page_context>\n${context}\n</page_context>`,
      '[attachment available: annotation.png (image/png, 1024 bytes) at /workspace/attachments/annotation.png]',
      '\n\n[Runtime context for this turn]\nCurrent local date/time: 2026-09-11T23:30+08:00 (Fri)\nTime zone / location hint: Asia/Shanghai\nUse this runtime context for questions about the current date, time, or local time zone. Do not treat it as a user request.',
    ].join(' '),
  })
  assert.deepEqual(edited.called, ['read_file', 'edit_file', 'open_workspace_preview'])
  assert.match(edited.answer, /紫色/)
  const fallback = await complete(provider, { role: 'user', content: prompts.fallback })
  assert.deepEqual(fallback.called, ['open_workspace_preview'])
  assert.ok(!fallback.answer.includes(entrypoint))
  const allToolIds = [...created.toolIds, ...edited.toolIds, ...fallback.toolIds]
  assert.equal(new Set(allToolIds).size, 7, 'Tool IDs must not collide across model completions or turns')
  const snapshot = provider.snapshot()
  assert.deepEqual(snapshot.completed, { create: 1, edit: 1, fallback: 1 })
  assert.ok(snapshot.requests.filter(request => request.scenario === 'edit').every(request => request.imageCount === 1 && request.annotationCount === 1))
  assert.deepEqual(snapshot.errors, [])
})

test('plain-text edit is supported but never reported as an annotation/screenshot', async t => {
  const provider = await startWorkspaceInlinePreviewProvider()
  t.after(() => provider.close())
  await complete(provider, { role: 'user', content: prompts.create })
  await complete(provider, { role: 'user', content: prompts.edit })
  assert.ok(provider.snapshot().requests.filter(request => request.scenario === 'edit').every(request => request.imageCount === 0 && request.annotationCount === 0))
})

test('unknown prompts, historical matches, unavailable tools and failed receipts are rejected', async t => {
  const provider = await startWorkspaceInlinePreviewProvider()
  t.after(() => provider.close())
  for (const messages of [
    [{ role: 'user', content: 'please ' + prompts.create }],
    [{ role: 'user', content: prompts.create }, { role: 'assistant', content: 'history' }, { role: 'user', content: 'unknown' }],
    [{ role: 'user', content: prompts.edit }],
  ]) assert.equal((await post(provider, messages)).status, 422)
  assert.equal((await post(provider, [{ role: 'user', content: prompts.create }], { tools: [] })).status, 422)
  assert.equal((await post(provider, [{ role: 'user', content: prompts.create }], { model: 'other-model' })).status, 422)
  const initial = await post(provider, [{ role: 'user', content: prompts.create }])
  assert.equal(initial.status, 200)
  const assistant = initial.parts[0].message
  for (const content of ['Error: permission denied', 'Written 9 bytes to /other/index.html', JSON.stringify({ status: 'blocked' })]) {
    assert.equal((await post(provider, [{ role: 'user', content: prompts.create }, assistant, { role: 'tool', tool_name: 'write_file', content }])).status, 422)
  }
})
