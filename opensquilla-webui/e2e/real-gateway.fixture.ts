import { expect, test as base } from '@playwright/test'
import { once } from 'node:events'
import { createServer as createNetServer } from 'node:net'
import { fileURLToPath } from 'node:url'
import { createServer as createViteServer, type ViteDevServer } from 'vite'
import { withIsolatedNativeDesktopPage } from './support/native-desktop-fixture'

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
export async function withIsolatedRealGateway(
  options: {
    outputDir: string
    scenario?: RealGoalGatewayScenario
    authMode?: 'none' | 'token'
    initialSessionKeys?: readonly string[]
    workspaceInitiallyMissing?: boolean
  },
  use: (gateway: IsolatedRealGateway) => Promise<void>,
): Promise<void> {
  const webuiPort = await reserveLoopbackPort()
  const webuiOrigin = `http://127.0.0.1:${webuiPort}`
  const gateway = await startRealGoalGateway({
    outputDir: options.outputDir,
    webuiOrigin,
    // This deterministic provider also supports an ordinary chat turn; the
    // release gate lets the test prove the UI is connected before it replies.
    scenario: options.scenario,
    authMode: options.authMode,
    initialSessionKeys: options.initialSessionKeys,
    workspaceInitiallyMissing: options.workspaceInitiallyMissing,
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
}

export const test = base.extend<{
  isolatedRealGateway: IsolatedRealGateway
  isolatedRealGatewayScenario: RealGoalGatewayScenario
  isolatedRealGatewayAuthMode: 'none' | 'token'
  isolatedRealGatewayInitialSessionKeys: readonly string[]
  isolatedRealGatewayNativeDesktop: boolean
  isolatedRealGatewayWorkspaceInitiallyMissing: boolean
}>({
  isolatedRealGatewayScenario: ['lifecycle', { option: true }],
  isolatedRealGatewayAuthMode: ['none', { option: true }],
  isolatedRealGatewayInitialSessionKeys: [[], { option: true }],
  isolatedRealGatewayNativeDesktop: [false, { option: true }],
  isolatedRealGatewayWorkspaceInitiallyMissing: [false, { option: true }],
  isolatedRealGateway: async ({ isolatedRealGatewayScenario, isolatedRealGatewayAuthMode, isolatedRealGatewayInitialSessionKeys, isolatedRealGatewayWorkspaceInitiallyMissing }, use, testInfo) =>
    withIsolatedRealGateway({
      outputDir: testInfo.outputPath('isolated-real-gateway'),
      scenario: isolatedRealGatewayScenario,
      authMode: isolatedRealGatewayAuthMode,
      initialSessionKeys: isolatedRealGatewayInitialSessionKeys,
      workspaceInitiallyMissing: isolatedRealGatewayWorkspaceInitiallyMissing,
    }, use),
  page: async ({ page, isolatedRealGateway, isolatedRealGatewayNativeDesktop }, use, testInfo) => {
    // Native-capable specs explicitly opt in: other tests may use the browser
    // context to create additional pages that cannot load the Desktop scheme.
    if (!isolatedRealGatewayNativeDesktop || process.env.OPENSQUILLA_E2E_NATIVE_DESKTOP !== '1') {
      await use(page)
      return
    }
    isolatedRealGateway.controlUrl = 'opensquilla-app://desktop/'
    await withIsolatedNativeDesktopPage(testInfo, use, {
      gatewayHttpUrl: isolatedRealGateway.wsUrl.replace(/^ws:/, 'http:').replace(/\/ws$/, ''),
    })
  },
})

export { expect }
