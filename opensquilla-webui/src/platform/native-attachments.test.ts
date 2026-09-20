// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createDesktopPlatform } from './desktop'

const connection = { schemaVersion: 1, revision: 1, status: 'ready', instanceId: 'instance-test',
  profileFingerprint: 'profile-test', httpUrl: 'http://127.0.0.1:12345',
  wsUrl: 'ws://127.0.0.1:12345/ws', authToken: 'private-test-token', error: null }
afterEach(() => { delete window.opensquillaDesktop })
describe('native attachment platform binding', () => {
  it('projects only non-secret owned instance/profile facts to product consumers', async () => {
    window.opensquillaDesktop = { getGatewayConnection: vi.fn(async () => connection) } as unknown as OpenSquillaDesktopApi
    expect(await createDesktopPlatform().gateway.getAttachmentBinding!()).toEqual({ instanceId: 'instance-test', profileFingerprint: 'profile-test' })
  })
  it('does not grant native file intake to an unowned or stopped gateway', async () => {
    for (const payload of [{ ...connection, authToken: null }, { ...connection, status: 'stopped' }]) {
      window.opensquillaDesktop = { getGatewayConnection: vi.fn(async () => payload) } as unknown as OpenSquillaDesktopApi
      expect(await createDesktopPlatform().gateway.getAttachmentBinding!()).toBeNull()
    }
  })
})
