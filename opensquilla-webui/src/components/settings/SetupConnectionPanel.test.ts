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
  connectedGatewayHost?: string | null
} = {}) {
  const gatewayAccess = reactive({
    availability: options.availability ?? 'unavailable',
    connectionHealth: options.health ?? 'healthy',
    isRuntimeStarting: false,
    connectionError: null as string | null,
    connectedGatewayHost: options.connectedGatewayHost,
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
    supportBundleUnavailableReason: null,
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

async function openConnectionEditor(el: HTMLElement) {
  const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
  details.open = true
  details.dispatchEvent(new Event('toggle'))
  await nextTick()
  return details
}

describe('SetupConnectionPanel', () => {
  it('shows the connected host without substituting an edited endpoint draft', async () => {
    const { el, gatewayAccess } = await mountPanel({
      availability: 'available', connectedGatewayHost: 'active-gateway.example:18791',
    })
    const reason = el.querySelector('.conn-status__reason')!
    expect(reason.textContent).toBe('active-gateway.example:18791')
    await openConnectionEditor(el)
    const endpoint = el.querySelector<HTMLInputElement>('#conn-ws-url')!
    endpoint.value = 'wss://draft-user:draft-secret@replacement.example:9443/private?token=draft-secret#private'
    endpoint.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(reason.textContent).toBe('active-gateway.example:18791')
    expect(gatewayAccess.connect).not.toHaveBeenCalled()
    gatewayAccess.connectedGatewayHost = 'replacement.example:9443'
    await nextTick()
    expect(reason.textContent).toBe('replacement.example:9443')
  })

  it.each([undefined, null])('keeps the connected fallback when no valid host is available (%s)', async connectedGatewayHost => {
    const { el } = await mountPanel({ availability: 'available', connectedGatewayHost })
    expect(el.querySelector('.conn-status__reason')?.textContent)
      .toBe(i18n.global.t('setup.connection.reasonConnected'))
  })

  it('keeps desktop connection wording instead of displaying its native endpoint', async () => {
    const { el } = await mountPanel({
      managed: true, availability: 'available', connectedGatewayHost: '127.0.0.1:18791',
    })
    expect(el.querySelector('.conn-status__reason')?.textContent)
      .toBe(i18n.global.t('setup.connection.reasonConnected'))
  })

  it.each(['disconnect', 'suspect', 'credential', 'error'] as const)('prioritizes %s recovery over the last connected host', async reason => {
    const { el, gatewayAccess } = await mountPanel({
      availability: 'available', connectedGatewayHost: 'active-gateway.example:18791',
    })
    if (reason === 'disconnect') gatewayAccess.availability = 'unavailable'
    if (reason === 'suspect') gatewayAccess.connectionHealth = 'suspect'
    if (reason === 'credential') gatewayAccess.requiresCredential = true
    if (reason === 'error') gatewayAccess.connectionError = 'authentication_mismatch'
    await nextTick()
    expect(el.querySelector('.conn-status__reason')?.textContent).not.toContain('active-gateway.example')
    if (reason === 'error') expect(el.querySelector('.conn-status__reason')?.textContent).toContain('authentication_mismatch')
  })

  it('starts healthy Web connections collapsed and preserves a user-opened editor through updates', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'available' })
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    expect(details.open).toBe(false)
    expect(details.querySelector('summary')?.textContent).toBe('Edit connection')
    expect(details.contains(el.querySelector('#conn-ws-url'))).toBe(true)
    expect(details.contains(button(el, i18n.global.t('setup.connection.disconnect')))).toBe(true)

    details.querySelector('summary')!.click()
    await nextTick()
    expect(details.open).toBe(true)
    gatewayAccess.connectionHealth = 'suspect'
    await nextTick()
    gatewayAccess.connectionHealth = 'healthy'
    await nextTick()
    expect(details.open).toBe(true)
    details.querySelector('summary')!.click()
    await nextTick()
    expect(details.open).toBe(false)
    expect(gatewayAccess.connect).not.toHaveBeenCalled()
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it.each(['unavailable', 'preparing'] as const)('starts %s connections expanded for recovery', async availability => {
    const { el } = await mountPanel({ availability })
    expect(el.querySelector<HTMLDetailsElement>('#settings-connection-details')?.open).toBe(true)
  })

  it('collapses a cold connection after it becomes healthy when the editor was untouched', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'preparing' })
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    expect(details.open).toBe(true)
    gatewayAccess.availability = 'available'
    await nextTick()
    expect(details.open).toBe(false)
  })

  it('keeps a search-focused editor open when a cold connection becomes healthy', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'preparing' })
    const details = await openConnectionEditor(el)
    const endpoint = el.querySelector<HTMLInputElement>('#conn-ws-url')!
    endpoint.focus()
    gatewayAccess.availability = 'available'
    await nextTick()
    expect(details.open).toBe(true)
    expect(document.activeElement).toBe(endpoint)
  })

  it.each(['disconnect', 'suspect', 'credential', 'error'] as const)('opens for %s and keeps drafts when health recovers', async reason => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'available' })
    const details = await openConnectionEditor(el)
    const endpoint = el.querySelector<HTMLInputElement>('#conn-ws-url')!
    const credential = el.querySelector<HTMLInputElement>('#conn-ws-token')!
    endpoint.value = 'wss://draft-gateway.example/ws'
    endpoint.dispatchEvent(new Event('input', { bubbles: true }))
    credential.value = 'synthetic-draft-token'
    credential.dispatchEvent(new Event('input', { bubbles: true }))
    details.open = false
    details.dispatchEvent(new Event('toggle'))
    await nextTick()

    if (reason === 'disconnect') gatewayAccess.availability = 'unavailable'
    if (reason === 'suspect') gatewayAccess.connectionHealth = 'suspect'
    if (reason === 'credential') gatewayAccess.requiresCredential = true
    if (reason === 'error') gatewayAccess.connectionError = 'authentication_mismatch'
    await nextTick()
    expect(details.open).toBe(true)
    if (reason === 'credential') expect(document.activeElement).toBe(credential)

    gatewayAccess.availability = 'available'
    gatewayAccess.connectionHealth = 'healthy'
    gatewayAccess.requiresCredential = false
    gatewayAccess.connectionError = null
    await nextTick()
    expect(details.open).toBe(true)
    expect(endpoint.value).toBe('wss://draft-gateway.example/ws')
    expect(credential.value).toBe('synthetic-draft-token')
    expect(gatewayAccess.loadConnectionEndpoint).toHaveBeenCalledTimes(1)
  })

  it('shows reconnecting instead of connected while transport health is suspect', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'available', health: 'suspect' })
    expect(el.querySelector('.conn-status__pill')?.textContent).toContain(i18n.global.t('chrome.connectionState.suspect'))
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
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    expect(details.open).toBe(availability !== 'available')
    expect(details.querySelector('summary')?.textContent).toBe(i18n.global.t('setup.connection.actions'))
    expect(el.querySelector('.control-section__desc')).toBeNull()
    expect(gatewayAccess.loadConnectionEndpoint).not.toHaveBeenCalled()
    expect(button(el, i18n.global.t(action)).classList.contains('btn--primary')).toBe(availability !== 'available')
    if (availability === 'unavailable') {
      expect(el.querySelector('.conn-status__reason')?.textContent)
        .toBe(i18n.global.t('setup.connection.reasonManagedDisconnected'))
    }

    if (!details.open) await openConnectionEditor(el)

    button(el, i18n.global.t(action)).click()
    await nextTick()

    expect(gatewayAccess.connect).toHaveBeenCalledExactlyOnceWith({
      endpoint: '',
      credential: undefined,
    })
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it.each(['disconnect', 'preparing', 'suspect', 'error'] as const)('opens managed controls immediately for %s', async reason => {
    const { el, gatewayAccess } = await mountPanel({ managed: true, availability: 'available' })
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    expect(details.open).toBe(false)

    if (reason === 'disconnect') gatewayAccess.availability = 'unavailable'
    if (reason === 'preparing') gatewayAccess.availability = 'preparing'
    if (reason === 'suspect') gatewayAccess.connectionHealth = 'suspect'
    if (reason === 'error') gatewayAccess.connectionError = 'Runtime descriptor unavailable'
    await nextTick()

    expect(details.open).toBe(true)
    expect(details.querySelector('.conn-actions .btn--primary')).not.toBeNull()
    expect(el.querySelectorAll('input')).toHaveLength(0)
    expect(gatewayAccess.connect).not.toHaveBeenCalled()
    expect(gatewayAccess.disconnect).not.toHaveBeenCalled()
  })

  it('collapses untouched managed controls after initial recovery', async () => {
    const { el, gatewayAccess } = await mountPanel({ managed: true, availability: 'preparing' })
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    expect(details.open).toBe(true)
    gatewayAccess.availability = 'available'
    await nextTick()
    expect(details.open).toBe(false)
  })

  it('keeps a user-opened managed section open through recovery', async () => {
    const { el, gatewayAccess } = await mountPanel({ managed: true, availability: 'available' })
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    details.querySelector('summary')!.click()
    await nextTick()
    expect(details.open).toBe(true)

    gatewayAccess.connectionHealth = 'suspect'
    await nextTick()
    gatewayAccess.connectionHealth = 'healthy'
    await nextTick()
    expect(details.open).toBe(true)
    expect(button(el, i18n.global.t('setup.connection.reconnect')).classList.contains('btn--primary')).toBe(false)
    details.querySelector('summary')!.click()
    await nextTick()
    expect(details.open).toBe(false)
  })

  it('keeps focused managed recovery controls exposed after connection recovers', async () => {
    const { el, gatewayAccess } = await mountPanel({ managed: true, availability: 'preparing' })
    const details = el.querySelector<HTMLDetailsElement>('#settings-connection-details')!
    const reconnect = button(el, i18n.global.t('setup.connection.connect'))
    reconnect.focus()

    gatewayAccess.availability = 'available'
    await nextTick()
    expect(details.open).toBe(true)
    expect(document.activeElement).toBe(reconnect)
  })

  it('keeps the default Web form and forwards the trimmed endpoint and credential', async () => {
    const { el, gatewayAccess } = await mountPanel({ availability: 'available' })
    await openConnectionEditor(el)
    const endpoint = el.querySelector<HTMLInputElement>('#conn-ws-url')!
    const credential = el.querySelector<HTMLInputElement>('#conn-ws-token')!
    expect(endpoint.value).toBe('ws://saved-gateway.example/ws')
    expect(credential.type).toBe('password')

    endpoint.value = '  wss://replacement-gateway.example/ws  '
    endpoint.dispatchEvent(new Event('input', { bubbles: true }))
    credential.value = '  synthetic-access-token  '
    credential.dispatchEvent(new Event('input', { bubbles: true }))
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
    await openConnectionEditor(el)
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
    expect(el.querySelector<HTMLDetailsElement>('#settings-connection-details')?.open).toBe(true)
    expect(el.querySelector('.conn-status__pill')?.textContent).toBe('Token required')
    expect(el.textContent).toContain('Automatic retries are paused')
    expect(document.activeElement).toBe(credential)
    expect(el.querySelector('.conn-optional')).toBeNull()

    credential.value = '  synthetic-replacement-token  '
    credential.dispatchEvent(new Event('input', { bubbles: true }))
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
    credential.dispatchEvent(new Event('input', { bubbles: true }))
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
