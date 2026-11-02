// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import i18n, { loadLocaleMessages } from './i18n'
import { installDesktopRuntimeBanner } from './desktop-runtime-banner'
import type { DesktopGatewayConnection } from './platform/types'

const platform = vi.hoisted(() => ({
  gateway: {
    getConnection: vi.fn(),
    onConnection: vi.fn(),
    retryStartup: vi.fn(async () => ({ ok: true })),
    revealLog: vi.fn(async () => true),
  },
}))
vi.mock('./platform', () => ({ getPlatform: () => platform }))

function connection(status: DesktopGatewayConnection['status'], revision = 1): DesktopGatewayConnection {
  return {
    schemaVersion: 1, revision, status, instanceId: 'synthetic-instance',
    profileFingerprint: 'synthetic-profile', httpUrl: null, wsUrl: null, error: null,
  }
}

describe('desktop runtime banner', () => {
  let receive: (state: DesktopGatewayConnection) => void
  let dispose: () => void
  const unsubscribe = vi.fn()
  const banner = () => document.getElementById('desktop-runtime-banner')!
  const message = () => document.getElementById('desktop-runtime-message')!.textContent
  const button = (name: 'retry' | 'log') => document.getElementById(`desktop-runtime-${name}`) as HTMLButtonElement

  beforeEach(async () => {
    vi.clearAllMocks()
    document.body.innerHTML = `
      <div id="desktop-runtime-banner" hidden>
        <span id="desktop-runtime-message"></span>
        <button id="desktop-runtime-retry" hidden></button>
        <button id="desktop-runtime-log" hidden></button>
      </div>`
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'en'
    platform.gateway.onConnection.mockImplementation(callback => {
      receive = callback
      return unsubscribe
    })
    platform.gateway.getConnection.mockImplementation(() => new Promise(() => {}))
    dispose = installDesktopRuntimeBanner()
  })

  afterEach(() => {
    dispose()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('keeps startup hidden until there is an explicit connection state', () => {
    expect(banner().hidden).toBe(true)
    expect(message()).toBe('')
  })

  it('does not turn a ready app into a starting app during shutdown, even with a stale upgrade warning', () => {
    receive(connection('ready'))
    expect(banner().hidden).toBe(true)
    receive({ ...connection('stopped', 2), sandboxUpgrade: { status: 'cleanup_pending' } })
    expect(banner().hidden).toBe(true)
    expect(message()).toBe('')
    expect(button('retry').hidden).toBe(true)
    expect(button('log').hidden).toBe(true)
  })

  it('uses the app locale rather than the browser language and updates while visible', async () => {
    vi.stubGlobal('navigator', { language: 'en-US' })
    i18n.global.locale.value = 'zh-Hans'
    receive(connection('starting'))
    expect(message()).toBe('正在启动 OpenSquilla…')
    expect(banner().hidden).toBe(false)
    receive(connection('error', 2))
    expect(message()).toBe('OpenSquilla 启动失败。')
    expect(button('retry').textContent).toBe('重试')
    expect(button('log').textContent).toBe('查看日志')
    i18n.global.locale.value = 'en'
    await nextTick()
    expect(message()).toBe('OpenSquilla failed to start.')
    expect(button('retry').textContent).toBe('Retry')
  })

  it('retains startup diagnostics and actions, and ready-state upgrade warnings', async () => {
    receive({ ...connection('error'), error: 'Synthetic startup failure' })
    expect(message()).toBe('Synthetic startup failure')
    expect(button('retry').hidden).toBe(false)
    expect(button('log').hidden).toBe(false)
    button('retry').click()
    button('log').click()
    await vi.waitFor(() => expect(button('retry').disabled).toBe(false))
    expect(platform.gateway.retryStartup).toHaveBeenCalledOnce()
    expect(platform.gateway.revealLog).toHaveBeenCalledOnce()
    receive({ ...connection('ready', 2), sandboxUpgrade: { status: 'retry_required' } })
    expect(banner().hidden).toBe(false)
    expect(banner().dataset.state).toBe('migration-warning')
    expect(button('retry').hidden).toBe(true)
    expect(button('log').hidden).toBe(false)
  })

  it('ignores an initial startup snapshot arriving after a shutdown notification', async () => {
    dispose()
    let resolve!: (state: DesktopGatewayConnection) => void
    platform.gateway.getConnection.mockImplementation(() => new Promise(accept => { resolve = accept }))
    dispose = installDesktopRuntimeBanner()
    receive(connection('stopped', 5))
    resolve(connection('starting', 4))
    await Promise.resolve()
    expect(banner().hidden).toBe(true)
    expect(message()).toBe('')
  })

  it('stops listeners and ignores late snapshots after disposal', async () => {
    receive(connection('stopped', 2))
    dispose()
    expect(unsubscribe).toHaveBeenCalledOnce()
    receive(connection('starting', 3))
    expect(banner().hidden).toBe(true)
  })
})
