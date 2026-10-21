// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { DesktopGatewayConnection, Platform } from '@/platform/types'
import { createDesktopPlatform } from '@/platform/desktop'
import { currentSessionLink } from './sessionLinks'

const key = 'agent:main:webchat:example'
const initialUrl = window.location.href
function desktop(connection: Partial<DesktopGatewayConnection> = {}) {
  return {
    capabilities: { isDesktop: true },
    gateway: { getConnection: vi.fn().mockResolvedValue({
      status: 'ready', httpUrl: 'http://127.0.0.1:18789', ...connection,
    }) },
  } as unknown as Platform
}

afterEach(() => {
  delete window.opensquillaDesktop
  localStorage.clear()
  document.getElementById('opensquilla-data')?.remove()
  window.location.href = initialUrl
})

describe('current session browser links', () => {
  it.each([
    { base: undefined, path: '/control/chat' },
    { base: '/control', path: '/control/chat' },
    { base: '', path: '/chat' },
    { base: '/', path: '/chat' },
    { base: '/custom', path: '/custom/chat' },
    { base: '/custom/ui/', path: '/custom/ui/chat' },
  ])('preserves the native Gateway mount $base through the platform adapter', async ({ base, path }) => {
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/renderer-only'
    document.body.append(data)
    window.opensquillaDesktop = {
      getGatewayConnection: vi.fn(async () => ({
        schemaVersion: 1, revision: 1, status: 'ready',
        instanceId: 'gateway-test', profileFingerprint: 'profile-test',
        httpUrl: 'http://127.0.0.1:18789/?token=private#private',
        wsUrl: 'ws://127.0.0.1:18789/ws', authToken: 'private-token', error: null,
        ...(base !== undefined ? { controlUiBasePath: base } : {}),
      })),
    } as unknown as OpenSquillaDesktopApi
    const session = 'agent:main:webchat:中文&next=bad'
    const link = new URL(await currentSessionLink(session, createDesktopPlatform()))
    expect(link.origin).toBe('http://127.0.0.1:18789')
    expect(link.pathname).toBe(path)
    expect(link.searchParams.get('session')).toBe(session)
    expect([...link.searchParams.keys()]).toEqual(['session'])
    expect(link.hash).toBe('')
  })

  it('uses the Desktop descriptor instead of stale storage or the renderer router base', async () => {
    localStorage.setItem('opensquilla.wsUrl', 'wss://previous.example/ws')
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/'
    document.body.append(data)
    expect(await currentSessionLink(key, desktop())).toBe(
      'http://127.0.0.1:18789/control/chat?session=agent%3Amain%3Awebchat%3Aexample',
    )
  })

  it('preserves HTTPS and omits credentials, query and fragment', async () => {
    expect(await currentSessionLink(key, desktop({
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
      await expect(currentSessionLink(key, desktop(connection))).rejects.toThrow()
    },
  )

  it('retains the public WebUI origin and nested base instead of exposing its separate Gateway', async () => {
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/nested/control/'
    document.body.append(data)
    window.location.href = 'https://frontend.example/nested/control/chat?session=old&token=dummy#private'
    localStorage.setItem('opensquilla.wsUrl', 'wss://gateway.example/ws?token=dummy')
    expect(await currentSessionLink(key, {
      capabilities: { isDesktop: false }, gateway: {},
    } as Platform)).toBe(
      'https://frontend.example/nested/control/chat?session=agent%3Amain%3Awebchat%3Aexample',
    )
  })

  it('uses a root-mounted WebUI and safely encodes a non-ASCII session key', async () => {
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/'
    document.body.append(data)
    window.location.href = 'http://localhost:5173/chat?session=old'
    const link = await currentSessionLink('agent:main:webchat:中文&next=bad', {
      capabilities: { isDesktop: false }, gateway: {},
    } as Platform)
    const parsed = new URL(link)
    expect(parsed.origin).toBe('http://localhost:5173')
    expect(parsed.pathname).toBe('/chat')
    expect(parsed.searchParams.get('session')).toBe('agent:main:webchat:中文&next=bad')
    expect([...parsed.searchParams.keys()]).toEqual(['session'])
  })
})
