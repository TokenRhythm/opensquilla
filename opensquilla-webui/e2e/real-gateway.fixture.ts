import { expect, test as base } from '@playwright/test'
import { once } from 'node:events'
import { createServer as createNetServer } from 'node:net'
import { fileURLToPath } from 'node:url'
import { createServer as createViteServer, type ViteDevServer } from 'vite'

import {
  startRealGoalGateway,
  type RealGoalGateway,
  type RealGoalGatewayScenario,
} from './real-goal-gateway'

export type IsolatedRealGateway = RealGoalGateway & {
  /** Dedicated Vite origin whose /ws and /api paths proxy only this Gateway. */
  webuiOrigin: string
  controlUrl: string
}

async function reserveLoopbackPort(): Promise<number> {
  const server = createNetServer()
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  if (!address || typeof address === 'string') {
    server.close()
    throw new Error('Unable to reserve a loopback port for the isolated WebUI')
  }
  const port = address.port
  server.close()
  await once(server, 'close')
  return port
}

/**
 * Real browser + real Gateway fixture with disposable state and a deterministic
 * provider. The browser uses its default same-origin `/ws`; no WebSocket route,
 * constructor shim, storage override, user profile, or external LLM is used.
 */
export const test = base.extend<{
  isolatedRealGateway: IsolatedRealGateway
  isolatedRealGatewayScenario: RealGoalGatewayScenario
}>({
  isolatedRealGatewayScenario: ['lifecycle', { option: true }],
  isolatedRealGateway: async ({ isolatedRealGatewayScenario }, use, testInfo) => {
    const webuiPort = await reserveLoopbackPort()
    const webuiOrigin = `http://127.0.0.1:${webuiPort}`
    const gateway = await startRealGoalGateway({
      outputDir: testInfo.outputPath('isolated-real-gateway'),
      webuiOrigin,
      // This deterministic provider also supports an ordinary chat turn; the
      // release gate lets the test prove the UI is connected before it replies.
      scenario: isolatedRealGatewayScenario,
    })
    const gatewayHttpUrl = gateway.wsUrl.replace(/^ws:/, 'http:').replace(/\/ws$/, '')
    const webuiRoot = fileURLToPath(new URL('..', import.meta.url))
    let vite: ViteDevServer | null = null

    try {
      vite = await createViteServer({
        root: webuiRoot,
        logLevel: 'error',
        server: {
          host: '127.0.0.1',
          port: webuiPort,
          strictPort: true,
          proxy: {
            '/api': {
              target: gatewayHttpUrl,
              changeOrigin: true,
              configure(proxy) {
                proxy.on('proxyReq', proxyReq => proxyReq.removeHeader('origin'))
              },
            },
            '/ws': {
              target: gatewayHttpUrl,
              changeOrigin: true,
              ws: true,
            },
            '/control/static': {
              target: gatewayHttpUrl,
              changeOrigin: true,
            },
          },
        },
      })
      await vite.listen()
      await use(Object.assign(gateway, {
        webuiOrigin,
        controlUrl: `${webuiOrigin}/control/`,
      }))
    } finally {
      await vite?.close()
      await gateway.stop()
    }
  },
})

export { expect }
