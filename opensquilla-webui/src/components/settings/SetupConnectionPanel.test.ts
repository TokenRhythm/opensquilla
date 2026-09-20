// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import {
  GATEWAY_ACCESS_KEY,
  type GatewayAccess,
  type GatewayAvailability,
} from '@/modules/gatewayAccess'
import SetupConnectionPanel from './SetupConnectionPanel.vue'

const mounted: App[] = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

async function mountPanel(options: {
  managed?: boolean
  availability?: GatewayAvailability
  health?: 'healthy' | 'suspect'
  requiresCredential?: boolean
} = {}) {
  const gatewayAccess = reactive({
    availability: options.availability ?? 'unavailable',
    connectionHealth: options.health ?? 'healthy',
    isRuntimeStarting: false,
    connectionError: null as string | null,
    requiresCredential: options.requiresCredential ?? false,
    isAvailable: options.availability === 'available',
    isLocalOwner: false,
    isAuthenticated: false,
    guestSessionOwnerId: null,
    deliveryIdentity: null,
    canManageProjectWorkspaces: false,
    canChooseProject: false,
    runModePolicy: null,
    streamIdleTimeoutMs: null,
    concurrentHistoryReads: false,
    chatSendInitialModel: false,
    sessionsRoutingModelSelection: false,
    detachedSessionHydration: false,
    turnCommittedEvents: false,
    subscriptionEpoch: 0,
    loadConnectionEndpoint: vi.fn(() => 'ws://saved-gateway.example/ws'),
    connect: vi.fn(async () => {}),
    disconnect: vi.fn(),
    recoverSubscriptionEpoch: vi.fn(() => false),
  } satisfies GatewayAccess)
  const el = document.createElement('div')
  document.body.appendChild(el)
  i18n.global.locale.value = 'en'
  const app = createApp(SetupConnectionPanel, { managed: options.managed })
  app.use(i18n)
  app.provide(GATEWAY_ACCESS_KEY, gatewayAccess)
  app.mount(el)
  mounted.push(app)
  await nextTick()
  return { el, gatewayAccess }
}

function button(el: HTMLElement, label: string): HTMLButtonElement {
  const found = [...el.querySelectorAll<HTMLButtonElement>('button')]
    .find(candidate => candidate.textContent?.trim() === label)
  if (!found) throw new Error(`Missing button: ${label}`)
  return found
}

describe('SetupConnectionPanel', () => {
  it('shows reconnecting instead of connected while transport health is suspect', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'available', health: 'suspect' })
    expect(el.querySelector('.conn-status__pill')?.textContent).toContain(i18n.global.t('setup.connection.connecting'))
    gatewayAccess.connectionHealth = 'healthy'
    await nextTick()
    expect(el.querySelector('.conn-status__pill')?.textContent).toContain(i18n.global.t('setup.connection.connected'))
  })

  it.each([
    ['unavailable', 'setup.connection.connect'],
    ['available', 'setup.connection.reconnect'],
  ] as const)('uses managed connection controls while %s', async (availability, action) => {
    const { el, gatewayAccess } = await mountPanel({ managed: true, availability })

    expect(el.querySelectorAll('input')).toHaveLength(0)
    expect(el.textContent).toContain(i18n.global.t('setup.runtime.desc'))
    expect(gatewayAccess.loadConnectionEndpoint).not.toHaveBeenCalled()

    button(el, i18n.global.t(action)).click()
    await nextTick()

    expect(gatewayAccess.connect).toHaveBeenCalledExactlyOnceWith({
      endpoint: '',
      credential: undefined,
    })
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it('keeps the default Web form and forwards the trimmed endpoint and credential', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'available' })
    const endpoint = el.querySelector<HTMLInputElement>('#conn-ws-url')!
    const credential = el.querySelector<HTMLInputElement>('#conn-ws-token')!
    expect(endpoint.value).toBe('ws://saved-gateway.example/ws')
    expect(credential.type).toBe('password')

    endpoint.value = '  wss://replacement-gateway.example/ws  '
    endpoint.dispatchEvent(new Event('input'))
    credential.value = '  synthetic-access-token  '
    credential.dispatchEvent(new Event('input'))
    button(el, i18n.global.t('setup.connection.reconnect')).click()
    await nextTick()

    expect(gatewayAccess.connect).toHaveBeenCalledExactlyOnceWith({
      endpoint: 'wss://replacement-gateway.example/ws',
      credential: 'synthetic-access-token',
    })
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it('keeps the Web credential optional', async () => {
    const { el, gatewayAccess } = await mountPanel()
    button(el, i18n.global.t('setup.connection.connect')).click()
    await nextTick()

    expect(gatewayAccess.connect).toHaveBeenCalledExactlyOnceWith({
      endpoint: 'ws://saved-gateway.example/ws',
      credential: undefined,
    })
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it.each([false, true])('keeps explicit disconnect available with managed=%s', async (managed) => {
    const { el, gatewayAccess } = await mountPanel({ managed, availability: 'available' })
    button(el, i18n.global.t('setup.connection.disconnect')).click()
    await nextTick()

    expect(gatewayAccess.disconnect).toHaveBeenCalledTimes(1)
    expect(gatewayAccess.connect).not.toHaveBeenCalled()
  })

  it('shows a failed reconnect attempt while the existing connection remains available', async () => {
    const { el, gatewayAccess } = await mountPanel({ managed: true, availability: 'available' })
    gatewayAccess.connectionError = 'Runtime descriptor unavailable'
    await nextTick()

    expect(el.querySelector('.conn-status__pill')?.textContent)
      .toBe(i18n.global.t('setup.connection.connected'))
    expect(el.querySelector('.conn-status__reason')?.textContent)
      .toBe(i18n.global.t('setup.connection.reasonFailed', {
        error: 'Runtime descriptor unavailable',
      }))
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it('focuses credential recovery on entry and submits without a preliminary disconnect', async () => {
    const { el, gatewayAccess } = await mountPanel({ requiresCredential: true })
    const credential = el.querySelector<HTMLInputElement>('#conn-ws-token')!
    expect(el.querySelector('.conn-status__pill')?.textContent).toBe('Token required')
    expect(el.textContent).toContain('Automatic retries are paused')
    expect(document.activeElement).toBe(credential)
    expect(el.querySelector('.conn-optional')).toBeNull()

    credential.value = '  synthetic-replacement-token  '
    credential.dispatchEvent(new Event('input'))
    credential.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await nextTick()
    expect(gatewayAccess.connect).toHaveBeenCalledExactlyOnceWith({
      endpoint: 'ws://saved-gateway.example/ws',
      credential: 'synthetic-replacement-token',
    })
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it('keeps a partially entered token and focus stable while showing a failed attempt', async () => {
    const { el, gatewayAccess } = await mountPanel()
    const credential = el.querySelector<HTMLInputElement>('#conn-ws-token')!
    credential.value = 'synthetic-partial-token'
    credential.dispatchEvent(new Event('input'))
    gatewayAccess.requiresCredential = true
    await nextTick()
    expect(document.activeElement).toBe(credential)

    const endpoint = el.querySelector<HTMLInputElement>('#conn-ws-url')!
    endpoint.focus()
    gatewayAccess.connectionError = 'authentication_mismatch'
    await nextTick()
    expect(document.activeElement).toBe(endpoint)
    expect(credential.value).toBe('synthetic-partial-token')
    expect(gatewayAccess.connect).not.toHaveBeenCalled()
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it('keeps Desktop credential recovery under the managed runtime controls', async () => {
    const { el } = await mountPanel({ managed: true, requiresCredential: true })
    expect(el.querySelectorAll('input')).toHaveLength(0)
    expect(el.textContent).not.toContain('Token required')
    expect(el.textContent).not.toContain('Automatic retries are paused')
  })
})
