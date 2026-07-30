import { createServer } from 'node:http'
import { createServer as createSecureServer } from 'node:https'
import { once } from 'node:events'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

import { test, expect } from '@playwright/test'

test('Control UI CSP admits the isolated localhost preview and cleanup transport', async ({
  browser,
  request,
}, testInfo) => {
  const controlResponse = await request.get('/control/')
  expect(controlResponse.ok()).toBe(true)
  const csp = controlResponse.headers()['content-security-policy'] || ''
  expect(csp).toContain('frame-src \'self\' blob: http://*.localhost:*')
  expect(csp).toContain('connect-src \'self\' ws: wss: http://*.localhost:*')

  const keyPath = testInfo.outputPath('loopback-preview-key.pem')
  const certificatePath = testInfo.outputPath('loopback-preview-cert.pem')
  const certificate = spawnSync('openssl', [
    'req',
    '-x509',
    '-newkey',
    'rsa:2048',
    '-sha256',
    '-nodes',
    '-keyout',
    keyPath,
    '-out',
    certificatePath,
    '-days',
    '1',
    '-subj',
    '/CN=127.0.0.1',
    '-addext',
    'subjectAltName=IP:127.0.0.1',
  ], { encoding: 'utf8' })
  expect(certificate.status, certificate.stderr || certificate.stdout).toBe(0)

  const token = '0123456789abcdef0123456789abcdef'
  let documentRequests = 0
  let cleanupRequests = 0
  const previewServer = createServer((incoming, response) => {
    const expectedHostPrefix = `p-${token}.localhost:`
    if (!String(incoming.headers.host || '').startsWith(expectedHostPrefix)) {
      response.writeHead(404).end()
      return
    }
    if (incoming.url === '/.opensquilla/clear-site-data') {
      cleanupRequests += 1
      response.writeHead(204, {
        'Cache-Control': 'no-store',
        'Clear-Site-Data': '"cache", "cookies", "storage"',
      }).end()
      return
    }
    documentRequests += 1
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
    }).end(
      '<!doctype html><script>parent.postMessage("opensquilla-preview-loaded","*")</script>',
    )
  })
  previewServer.listen(0, '127.0.0.1')
  await once(previewServer, 'listening')

  const secureControlServer = createSecureServer({
    key: readFileSync(keyPath),
    cert: readFileSync(certificatePath),
  }, (_incoming, response) => {
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'Content-Security-Policy': csp,
    }).end('<!doctype html><title>Preview CSP probe</title>')
  })
  secureControlServer.listen(0, '127.0.0.1')
  await once(secureControlServer, 'listening')

  try {
    const address = previewServer.address()
    if (!address || typeof address === 'string') throw new Error('preview server has no TCP port')
    const previewOrigin = `http://p-${token}.localhost:${address.port}`
    const controlAddress = secureControlServer.address()
    if (!controlAddress || typeof controlAddress === 'string') {
      throw new Error('secure Control UI probe has no TCP port')
    }
    const context = await browser.newContext({ ignoreHTTPSErrors: true })
    const page = await context.newPage()
    const browserDiagnostics: string[] = []
    page.on('console', message => browserDiagnostics.push(
      `console:${message.type()}:${message.text()}`,
    ))
    page.on('requestfailed', request => browserDiagnostics.push(
      `requestfailed:${request.url()}:${request.failure()?.errorText || 'unknown'}`,
    ))
    const secureControlUrl = `https://127.0.0.1:${controlAddress.port}/control/`
    await page.goto(secureControlUrl)
    await page.evaluate((url) => {
      ;(window as typeof window & { __opensquillaPreviewLoaded?: boolean })
        .__opensquillaPreviewLoaded = false
      window.addEventListener('message', (event) => {
        if (event.data === 'opensquilla-preview-loaded') {
          ;(window as typeof window & { __opensquillaPreviewLoaded?: boolean })
            .__opensquillaPreviewLoaded = true
        }
      })
      const frame = document.createElement('iframe')
      frame.src = `${url}/index.html`
      document.body.append(frame)
    }, previewOrigin)
    try {
      await page.waitForFunction(
        () => (window as typeof window & { __opensquillaPreviewLoaded?: boolean })
          .__opensquillaPreviewLoaded === true,
        undefined,
        { timeout: 10_000 },
      )
    } catch {
      throw new Error(
        `preview iframe did not load; requests=${documentRequests}; `
        + browserDiagnostics.join('\n'),
      )
    }
    expect(documentRequests).toBe(1)

    const cleanupReached = await page.evaluate(async (url) => {
      try {
        await fetch(`${url}/.opensquilla/clear-site-data`, {
          cache: 'no-store',
          mode: 'no-cors',
        })
        return true
      } catch {
        return false
      }
    }, previewOrigin)
    expect(cleanupReached).toBe(true)
    await expect.poll(() => cleanupRequests).toBe(1)
    await context.close()
  } finally {
    const closing = [
      once(previewServer, 'close'),
      once(secureControlServer, 'close'),
    ]
    previewServer.close()
    secureControlServer.close()
    await Promise.all(closing)
  }
})
