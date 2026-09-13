import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { pathToFileURL } from 'node:url'

export const model = 'opensquilla-inline-preview-fixture'
export const prompts = {
  create: '创建正文链接测试网页',
  edit: '把标题改成紫色',
  fallback: '测试无文件名回复',
}
export const entrypoint = 'inline-preview/index.html'
export const stylesheet = 'inline-preview/style.css'
export const originalCss = 'body { margin: 0; padding: 64px; background: #f7f4ed; font: 20px system-ui; }\nh1 { color: #176b87; font-size: 56px; }\np { color: #424b54; }\n'
const html = '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><title>正文链接测试</title><link rel="stylesheet" href="style.css"></head><body><h1>正文链接测试</h1><p>这是本地免费 fixture，通过真实文件工具创建的页面。</p><p>请圈选标题，输入“把标题改成紫色”。</p></body></html>\n'
const previewCall = { name: 'open_workspace_preview', arguments: { path: entrypoint } }
const plans = {
  create: [
    { name: 'write_file', arguments: { path: entrypoint, content: html } },
    { name: 'write_file', arguments: { path: stylesheet, content: originalCss } },
    { name: 'open_workspace_preview', arguments: { path: entrypoint, bundle: 'directory', bundle_root: 'inline-preview' } },
  ],
  edit: [
    { name: 'read_file', arguments: { path: stylesheet } },
    { name: 'edit_file', arguments: { path: stylesheet, old_text: '#176b87', new_text: '#7c3aed' } },
    previewCall,
  ],
  fallback: [previewCall],
}
const answers = {
  create: '本地 fixture 已通过真实文件工具生成网页，可以打开 `inline-preview/index.html` 查看。',
  edit: '本地 fixture 已通过真实文件工具把标题改成紫色，可以打开 `inline-preview/index.html` 验证。',
  fallback: '本地 fixture 已重新登记预览。下面应只显示轻量的正文链接，不应出现独立按钮。',
}

// Same production wrappers as packaged-retained-interaction/provider.mjs.
const timePrefix = /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Za-z0-9_+\-/]+\]\n/
const runtimeSuffix = / ?\n\n\[Runtime context for this turn\]\nCurrent local date\/time: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} \((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\)\nTime zone \/ location hint: [^\r\n]{1,128}\nUse this runtime context for questions about the current date, time, or local time zone\. Do not treat it as a user request\.$/
const attachmentSuffix = / \[attachment available: [^\]\r\n]+ \(image\/(?:png|jpeg|webp|gif), \d+ bytes\) at [^\]\r\n]+\]$/

function currentTurn(message) {
  assert.equal(typeof message?.content, 'string', 'Missing current user text')
  const images = message.images ?? []
  assert.ok(Array.isArray(images) && images.every(image => typeof image === 'string' && image.length > 0), 'Invalid screenshot payload')
  // Agent appends runtime context after the attachment blocks. Ollama joins
  // those text blocks with one space, including before the runtime's \n\n.
  let text = message.content.replace(timePrefix, '').replace(runtimeSuffix, '')
  for (let index = 0; index < images.length; index += 1) text = text.replace(attachmentSuffix, '')
  let annotationCount = 0
  const page = text.match(/\n\n<page_context>\n([^\n]+)\n<\/page_context>$/)
  if (page) {
    const context = JSON.parse(page[1].replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&'))
    assert.ok(typeof context.resourceId === 'string' && context.resourceId.startsWith('document:doc_'), 'Annotation must reference a real Document')
    assert.ok(Array.isArray(context.annotations) && context.annotations.length > 0, 'Missing page annotations')
    assert.ok(context.annotations.every(annotation => annotation.text === prompts.edit), 'Unknown annotation instruction')
    annotationCount = context.annotations.length
    text = text.slice(0, page.index)
  }
  const scenario = Object.keys(prompts).find(key => prompts[key] === text)
  assert.ok(scenario, 'Request did not exactly match a fixture turn')
  assert.ok(!annotationCount || scenario === 'edit', 'Annotations are supported only on the edit turn')
  return { scenario, imageCount: images.length, annotationCount }
}

function validateResult(call, result) {
  assert.equal(result.role, 'tool', 'Expected a real tool result')
  assert.equal(result.tool_name, call.name, 'Tool result name mismatch')
  assert.equal(typeof result.content, 'string', 'Missing tool result content')
  const content = result.content
  if (call.name === 'write_file') {
    assert.ok(content.startsWith(`Written ${call.arguments.content.length} bytes to `), 'File write was not successful')
    assert.ok(content.split('\n')[0].includes(`/${call.arguments.path}`), 'Wrong write target')
  } else if (call.name === 'read_file') {
    assert.equal(content.replace(/^\d+\t/gm, ''), originalCss, 'Read did not return the expected original CSS bytes')
  } else if (call.name === 'edit_file') {
    assert.ok(content.startsWith('Edited ') && content.includes(`/${stylesheet}: replaced 7 chars with 7 chars`), 'CSS edit was not successful')
  } else {
    const opened = JSON.parse(content)
    assert.ok(/^doc_[A-Za-z0-9_-]+$/.test(opened.documentId), 'Missing registered Document')
    assert.equal(opened.resourceId, `document:${opened.documentId}`)
    assert.equal(opened.open?.resourceId, opened.resourceId)
    assert.ok(['ready', 'registered'].includes(opened.previewStatus), 'Preview not ready')
    assert.ok(typeof opened.entrypoint === 'string' && opened.entrypoint.endsWith(`/${entrypoint}`), 'Wrong preview entrypoint')
    assert.equal(opened.bundleMode, 'directory', 'Preview must retain the dedicated directory scope')
    assert.ok(!opened.error, 'Preview returned an error')
  }
}

export async function startWorkspaceInlinePreviewProvider({ port = 0, onEvent = () => {} } = {}) {
  assert.ok(Number.isInteger(port) && port >= 0 && port <= 65535, 'Invalid loopback port')
  const state = { requests: [], errors: [], completed: { create: 0, edit: 0, fallback: 0 } }
  const sockets = new Set()
  const server = createServer(async (request, response) => {
    try {
      if (request.method === 'GET' && ['/api/tags', '/api/version'].includes(request.url)) {
        response.writeHead(200, { 'Content-Type': 'application/json' })
        response.end(JSON.stringify(request.url === '/api/tags'
          ? { models: [{ name: model, model, modified_at: '2026-01-01T00:00:00Z', size: 1, digest: 'synthetic-inline-preview', details: {} }] }
          : { version: '0.0.0-inline-preview-fixture' }))
        return
      }
      assert.ok(request.method === 'POST' && request.url === '/api/chat', 'Unexpected fixture endpoint')
      let body = ''
      for await (const chunk of request) {
        body += chunk
        assert.ok(Buffer.byteLength(body) <= 8 * 1024 * 1024, 'Fixture request exceeds limit')
      }
      const payload = JSON.parse(body)
      assert.equal(payload.model, model, 'Unexpected fixture model')
      assert.ok(Array.isArray(payload.messages), 'Missing provider messages')
      const userIndex = payload.messages.findLastIndex(message => message.role === 'user')
      const turn = currentTurn(payload.messages[userIndex])
      const plan = plans[turn.scenario]
      const tail = payload.messages.slice(userIndex + 1)
      assert.equal(tail.length % 2, 0, 'Incomplete tool call/result pair')
      const step = tail.length / 2
      assert.ok(step <= plan.length, 'Unexpected extra tool result')
      for (let index = 0; index < step; index += 1) {
        const assistant = tail[index * 2]
        assert.equal(assistant.role, 'assistant')
        assert.deepEqual(assistant.tool_calls?.map(call => call.function), [plan[index]], 'Tool call differs from the fixture plan')
        validateResult(plan[index], tail[index * 2 + 1])
      }
      if (turn.scenario !== 'create') assert.ok(state.completed.create > 0, 'Create must complete before a follow-up')
      const next = plan[step]
      if (next) assert.ok(payload.tools?.some(tool => tool.function?.name === next.name), `Gateway did not advertise ${next.name}`)
      const event = { ...turn, step, nextTool: next?.name ?? null }
      state.requests.push(event)
      onEvent({ fixture: true, ...event })
      if (!next) state.completed[turn.scenario] += 1
      response.writeHead(200, { 'Content-Type': 'application/x-ndjson', 'Cache-Control': 'no-store' })
      // Explicit IDs are supported by the adapter and must remain unique
      // across completions, not merely within this one NDJSON response.
      response.write(JSON.stringify({ model, message: { role: 'assistant', content: next ? '' : answers[turn.scenario], ...(next ? { tool_calls: [{ id: `fixture_${randomUUID()}`, function: next }] } : {}) }, done: false }) + '\n')
      response.end(JSON.stringify({ model, message: { role: 'assistant', content: '' }, done: true, done_reason: 'stop', prompt_eval_count: 12, eval_count: 8 }) + '\n')
    } catch (error) {
      state.errors.push(error.message)
      onEvent({ fixture: true, error: error.message })
      response.writeHead(422, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({ error: 'Local inline-preview fixture rejected the request' }))
    }
  })
  server.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)) })
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve) })
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    model,
    snapshot: () => structuredClone(state),
    async close() { await new Promise(resolve => { server.close(resolve); for (const socket of sockets) socket.destroy() }) },
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  assert.ok(process.argv.length <= 3, 'Usage: node provider.mjs [port]')
  const provider = await startWorkspaceInlinePreviewProvider({ port: Number(process.argv[2] ?? 0), onEvent: event => process.stdout.write(JSON.stringify(event) + '\n') })
  process.stdout.write(JSON.stringify({ fixture: true, ...{ baseUrl: provider.baseUrl, model, prompts } }) + '\n')
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { await provider.close(); process.exit(0) })
}
