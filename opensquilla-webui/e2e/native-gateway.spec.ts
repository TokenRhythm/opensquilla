import { request as httpRequest } from 'node:http'
import { expect } from '@playwright/test'
import { test } from './support/native-gateway-fixture'

test('serves UI assets and native WebSocket frames without forwarding gateway requests', async ({
  page, request, nativeGateway,
}) => {
  const origin = await nativeGateway(socket => {
    socket.on('message', message => {
      const frame = JSON.parse(String(message)) as { type: string; nonce?: string }
      if (frame.type === 'ping') socket.send(JSON.stringify({ type: 'pong', nonce: frame.nonce }))
    })
  })
  const response = await page.goto(origin + '/control/')
  expect(response?.status()).toBe(200)
  expect(await response?.text()).toContain('<script')
  const reply = await page.evaluate(url => new Promise<string>((resolve, reject) => {
    const socket = new WebSocket(url)
    socket.onopen = () => socket.send(JSON.stringify({ type: 'ping', nonce: 'synthetic-probe' }))
    socket.onerror = () => reject(new Error('native WebSocket connection failed'))
    socket.onmessage = event => { resolve(String(event.data)); socket.close() }
  }), origin.replace('http:', 'ws:') + '/ws')
  expect(JSON.parse(reply)).toEqual({ type: 'pong', nonce: 'synthetic-probe' })
  expect((await request.get(origin + '/api/approvals')).status()).toBe(403)
  expect((await request.post(origin + '/control/')).status()).toBe(403)
  expect((await request.get(origin + '/control/../api/approvals')).status()).toBe(403)
})

test('rejects raw request targets that escape the configured asset upstream', async ({ nativeGateway }) => {
  const origin = await nativeGateway(() => {})
  for (const path of [
    '//127.0.0.1:9/control/',
    'http://127.0.0.1:9/control/',
    '/control/%2e%2e/api/approvals',
  ]) {
    const status = await new Promise<number | undefined>((resolve, reject) => {
      const request = httpRequest(origin, { path }, response => {
        response.resume()
        resolve(response.statusCode)
      })
      request.on('error', reject)
      request.end()
    })
    expect(status).toBe(403)
  }
})
