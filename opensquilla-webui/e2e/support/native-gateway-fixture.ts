import { createServer, request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { test as base } from '@playwright/test'
import { WebSocketServer, type WebSocket } from 'ws'

type StartNativeGateway = (onConnection: (socket: WebSocket) => void) => Promise<string>

/** Keep pressure measurements on the browser's native WebSocket path. Sending
 * tens of thousands of frames via routeWebSocket adds page-side instrumentation
 * allocations to Runtime.getHeapUsage, which is not the application's heap cost.
 * Only read-only UI assets are proxied; all gateway traffic stays synthetic.
 */
export const test = base.extend<{ nativeGateway: StartNativeGateway }>({
  nativeGateway: async ({ baseURL }, use) => {
    if (!baseURL) throw new Error('native gateway requires a WebUI baseURL')
    const upstream = new URL(baseURL)
    if (!['http:', 'https:'].includes(upstream.protocol)) throw new Error('invalid WebUI protocol')
    const closers: Array<() => Promise<void>> = []
    try {
      await use(async onConnection => {
        const server = createServer((incoming, outgoing) => {
          const asset = new URL(incoming.url || '/', upstream)
          if (!['GET', 'HEAD'].includes(incoming.method || '')
            || asset.origin !== upstream.origin || !asset.pathname.startsWith('/control/')) {
            outgoing.writeHead(403).end()
            return
          }
          const request = upstream.protocol === 'https:' ? httpsRequest : httpRequest
          // The request target may select a path, never the upstream authority.
          const proxy = request(upstream, {
            path: asset.pathname + asset.search,
            method: incoming.method,
          }, response => {
            outgoing.writeHead(response.statusCode || 502, response.headers)
            response.pipe(outgoing)
          })
          proxy.on('error', () => {
            if (!outgoing.headersSent) outgoing.writeHead(502)
            outgoing.end()
          })
          outgoing.on('close', () => proxy.destroy())
          proxy.setTimeout(10_000, () => proxy.destroy(new Error('WebUI asset timeout')))
          proxy.end()
        })
        const sockets = new WebSocketServer({ server, path: '/ws' })
        sockets.on('connection', onConnection)
        closers.push(async () => {
          for (const client of sockets.clients) client.terminate()
          await new Promise<void>(resolve => sockets.close(() => resolve()))
          server.closeAllConnections()
          await new Promise<void>(resolve => server.close(() => resolve()))
        })
        await new Promise<void>((resolve, reject) => {
          server.once('error', reject)
          server.listen(0, '127.0.0.1', () => {
            server.removeListener('error', reject)
            resolve()
          })
        })
        const address = server.address()
        if (!address || typeof address === 'string') throw new Error('native gateway failed to bind')
        return `http://127.0.0.1:${address.port}`
      })
    } finally {
      for (const close of closers) await close()
    }
  },
})
