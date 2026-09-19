// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DesktopGatewayConnection, Platform } from '@/platform/types'
import { currentSessionGatewayLink } from './sessionLinks'

const key = 'agent:main:webchat:example'
function desktop(connection: Partial<DesktopGatewayConnection> = {}) {
  return {
    capabilities: { isDesktop: true },
    gateway: { getConnection: vi.fn().mockResolvedValue({
      status: 'ready', httpUrl: 'http://127.0.0.1:18789', ...connection,
    }) },
  } as unknown as Platform
}

afterEach(() => {
  localStorage.clear()
  document.getElementById('opensquilla-data')?.remove()
})

describe('current Session Gateway links', () => {
  it('uses the Desktop descriptor instead of stale storage or the renderer router base', async () => {
    localStorage.setItem('opensquilla.wsUrl', 'wss://previous.example/ws')
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/'
    document.body.append(data)
    expect(await currentSessionGatewayLink(key, desktop())).toBe(
      'http://127.0.0.1:18789/control/chat?session=agent%3Amain%3Awebchat%3Aexample',
    )
  })

  it('preserves HTTPS and omits credentials, query and fragment', async () => {
    expect(await currentSessionGatewayLink(key, desktop({
      httpUrl: 'https://user:dummy@gateway.example/?token=dummy#dummy',
      authToken: 'dummy-ephemeral-credential',
    }))).toBe('https://gateway.example/control/chat?session=agent%3Amain%3Awebchat%3Aexample')
  })

  it.each([
    { status: 'starting', httpUrl: null },
    { status: 'ready', httpUrl: null, wsUrl: null },
    { httpUrl: 'opensquilla-app://desktop' },
  ] as Partial<DesktopGatewayConnection>[])(
    'fails instead of copying a Desktop renderer URL for %j', async connection => {
      await expect(currentSessionGatewayLink(key, desktop(connection))).rejects.toThrow()
    },
  )

  it('retains the configured base path for the browser', async () => {
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/nested/control/'
    document.body.append(data)
    localStorage.setItem('opensquilla.wsUrl', 'wss://gateway.example/ws?token=dummy')
    expect(await currentSessionGatewayLink(key, {
      capabilities: { isDesktop: false }, gateway: {},
    } as Platform)).toBe(
      'https://gateway.example/nested/control/chat?session=agent%3Amain%3Awebchat%3Aexample',
    )
  })
})
