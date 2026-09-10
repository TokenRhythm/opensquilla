import assert from 'node:assert/strict'
import { request as httpRequest } from 'node:http'
import { DesktopBrowserServer, parseDesktopBrowserRequest, DESKTOP_BROWSER_URL_ENV, DESKTOP_BROWSER_TOKEN_ENV } from '../dist/desktop-browser.js'

const audit = []
let calls = 0
let interrupted = false
const server = new DesktopBrowserServer(async (request, signal) => {
  calls++
  if (request.sessionKey === 'slow') return await new Promise(resolve => {
    signal.addEventListener('abort', () => { interrupted = true; resolve({ cancelled: true }) }, { once: true })
  })
  return { targets: [{ targetRef: 'synthetic-target', sessionKey: request.sessionKey }] }
}, entry => audit.push(entry))
const environment = await server.start()
const endpoint = environment[DESKTOP_BROWSER_URL_ENV]
const token = environment[DESKTOP_BROWSER_TOKEN_ENV]
const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
const payload = { sessionKey: 'synthetic-session', operation: 'list' }
const invoke = async (body = payload, extra = {}) => fetch(endpoint, {
  method: 'POST', headers: { ...headers, ...extra }, body: typeof body === 'string' ? body : JSON.stringify(body),
})
try {
  assert.deepEqual(await server.start(), environment)
  assert.equal((await invoke()).status, 200)
  assert.equal((await invoke(payload, { Authorization: 'Bearer invalid' })).status, 401)
  assert.equal((await invoke(payload, { Origin: 'https://untrusted.invalid' })).status, 403)
  assert.equal((await invoke(payload, { 'Sec-Fetch-Site': 'same-origin' })).status, 403)
  const invalidHostStatus = await new Promise((resolve, reject) => {
    const request = httpRequest(endpoint, { method: 'POST', headers: { ...headers, Host: 'untrusted.invalid' } }, response => {
      response.resume(); resolve(response.statusCode)
    })
    request.on('error', reject)
    request.end(JSON.stringify(payload))
  })
  assert.equal(invalidHostStatus, 403)
  assert.equal((await invoke('{')).status, 400)
  assert.equal((await invoke({ ...payload, artifactId: 'untrusted' })).status, 400)
  assert.equal((await invoke('x'.repeat(65537))).status, 413)
  assert.equal((await invoke({ sessionKey: 'slow', operation: 'list' }, {
    'x-opensquilla-deadline-at-ms': String(Date.now() + 40),
  })).status, 504)
  assert.equal(interrupted, true)
  assert.equal(calls, 2, 'rejected requests must never reach the browser')
  assert.equal(JSON.stringify(audit).includes(token), false)
  assert.equal(JSON.stringify(audit).includes('synthetic-session'), false)
  assert.throws(() => parseDesktopBrowserRequest({ operation: 'act', sessionKey: 's', targetRef: 'p', action: 'click' }))
  assert.throws(() => parseDesktopBrowserRequest({ operation: 'act', sessionKey: 's', targetRef: 'p', action: 'scroll', direction: 'down', amount: '20' }))
  assert.equal(parseDesktopBrowserRequest({ operation: 'act', sessionKey: 's', targetRef: 'p', action: 'fill', ref: 'e', text: '' }).text, '')
  // A client that disconnects while uploading must not leave an operation running.
  await new Promise(resolve => {
    const client = httpRequest(endpoint, { method: 'POST', headers: { ...headers, 'Content-Length': '100' } })
    client.on('error', resolve)
    client.write('{')
    client.destroy()
    resolve()
  })
  console.log('Desktop browser authentication, bounds and cancellation passed.')
} finally {
  await server.close()
}
