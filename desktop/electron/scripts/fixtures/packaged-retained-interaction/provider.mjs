import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { sha256 } from './contract.mjs'

// The provider is not given the sentinel's contents. It can complete the tool
// turn only after read_file returns the nonce whose hash the driver supplied.
export async function startRetainedProvider({ baseUrl, model, messages, sentinelPath, sentinelTokenSha256 }) {
  const endpoint = new URL(baseUrl)
  assert.equal(endpoint.hostname, '127.0.0.1')
  const state = { first: 0, toolCalls: 0, toolResults: 0, held: 0, cancelledBeforeCleanup: 0, afterStop: 0, restart: 0, errors: [] }
  const sockets = new Set()
  let closing = false
  let heldResponse = null
  const send = (response, content, toolCalls) => {
    response.writeHead(200, { 'Content-Type': 'application/x-ndjson', 'Cache-Control': 'no-store' })
    response.write(JSON.stringify({ model, message: { role: 'assistant', content, ...(toolCalls ? { tool_calls: toolCalls } : {}) }, done: false }) + '\n')
    response.end(JSON.stringify({ model, message: { role: 'assistant', content: '' }, done: true, done_reason: 'stop', prompt_eval_count: 12, eval_count: 8 }) + '\n')
  }
  const server = createServer(async (request, response) => {
    try {
      if (request.method === 'GET' && request.url === '/api/tags') {
        response.writeHead(200, { 'Content-Type': 'application/json' })
        response.end(JSON.stringify({ models: [{ name: model, model, modified_at: '2026-01-01T00:00:00Z', size: 1, digest: 'synthetic-retained-audit', details: {} }] }))
        return
      }
      if (request.method === 'GET' && request.url === '/api/version') {
        response.writeHead(200, { 'Content-Type': 'application/json' })
        response.end(JSON.stringify({ version: '0.0.0-retained-audit' }))
        return
      }
      assert.ok(request.method === 'POST' && request.url === '/api/chat', 'Unexpected provider endpoint')
      let body = ''
      for await (const chunk of request) {
        body += chunk
        assert.ok(Buffer.byteLength(body) <= 4 * 1024 * 1024, 'Synthetic provider request exceeds its limit')
      }
      const payload = JSON.parse(body)
      assert.equal(payload.model, model, 'Unexpected provider model')
      assert.ok(Array.isArray(payload.messages), 'Missing provider messages')
      const userIndex = payload.messages.findLastIndex(message => message.role === 'user')
      const prompt = payload.messages[userIndex]?.content
      if (prompt === messages.first) {
        assert.equal(++state.first, 1, 'Duplicate first send')
        send(response, messages.firstAnswer)
      } else if (prompt === messages.tool) {
        const tail = payload.messages.slice(userIndex + 1)
        const toolResult = tail.findLast(message => message.role === 'tool')
        if (!toolResult) {
          assert.equal(++state.toolCalls, 1, 'Duplicate sentinel tool call')
          assert.ok(payload.tools?.some(tool => tool.function?.name === 'read_file'), 'The real Gateway must advertise read_file')
          send(response, '', [{ function: { name: 'read_file', arguments: { path: sentinelPath } } }])
        } else {
          assert.equal(state.toolCalls, 1, 'Unsolicited tool result')
          assert.equal(++state.toolResults, 1, 'Duplicate sentinel tool result')
          assert.equal(toolResult.tool_name, 'read_file', 'A different tool cannot prove sentinel access')
          assert.ok(tail.some(message => message.role === 'assistant' && message.tool_calls?.some(call =>
            call.function?.name === 'read_file' && call.function.arguments?.path === sentinelPath)), 'Tool result must follow the requested file read')
          const tokens = String(toolResult.content).match(/OPENSQUILLA_RETAINED_[0-9a-f]{64}/g) || []
          assert.ok(tokens.some(token => sha256(token) === sentinelTokenSha256), 'Tool output did not contain the unpredictable sentinel')
          send(response, messages.toolAnswer)
        }
      } else if (prompt === messages.stop) {
        assert.equal(++state.held, 1, 'Duplicate held request')
        heldResponse = response
        response.writeHead(200, { 'Content-Type': 'application/x-ndjson', 'Cache-Control': 'no-store' })
        response.write(JSON.stringify({ model, message: { role: 'assistant', content: messages.stopPartial }, done: false }) + '\n')
        response.once('close', () => {
          if (!closing && !response.writableEnded) state.cancelledBeforeCleanup += 1
          heldResponse = null
        })
        // Deliberately no terminal chunk. Only the client's actual Stop or
        // explicit fixture cleanup may close this response.
      } else if (prompt === messages.afterStop) {
        assert.equal(state.cancelledBeforeCleanup, 1, 'A real cancellation must precede follow-up')
        assert.equal(++state.afterStop, 1, 'Duplicate follow-up')
        send(response, messages.afterStopAnswer)
      } else if (prompt === messages.restart) {
        assert.equal(++state.restart, 1, 'Duplicate restart send')
        send(response, messages.restartAnswer)
      } else {
        throw new Error('Request did not match a synthetic audit turn')
      }
    } catch (error) {
      state.errors.push(error.message)
      if (!response.headersSent) response.writeHead(422, { 'Content-Type': 'application/json' })
      response.end(JSON.stringify({ error: 'Synthetic audit provider rejected the request' }))
    }
  })
  server.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)) })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(Number(endpoint.port), '127.0.0.1', resolve)
  })
  return {
    baseUrl: `http://127.0.0.1:${server.address().port}`,
    snapshot: () => structuredClone(state),
    async close() {
      closing = true
      heldResponse?.destroy()
      await new Promise(resolve => { server.close(resolve); for (const socket of sockets) socket.destroy() })
    },
  }
}
