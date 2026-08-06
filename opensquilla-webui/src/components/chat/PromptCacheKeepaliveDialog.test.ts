// @vitest-environment happy-dom
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { useRpcStore } from '@/stores/rpc'
import PromptCacheKeepaliveDialog from './PromptCacheKeepaliveDialog.vue'

async function settle() {
  await Promise.resolve()
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
  const { toasts, dismissToast } = useToasts()
  for (const toast of [...toasts.value]) dismissToast(toast.id)
  vi.restoreAllMocks()
})

describe('PromptCacheKeepaliveDialog', () => {
  it('explains the bounded plan and saves both timing values', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const rpc = useRpcStore(pinia)
    const scheduledStatus = {
      enabled: true,
      ttlSeconds: 300,
      intervalSeconds: 240,
      idleTimeoutSeconds: 3_600,
      idleExpiresAt: null,
      state: 'scheduled',
      reason: null,
      hasSnapshot: true,
      lastCacheHitTokens: 42,
      provider: 'synthetic',
      model: 'synthetic-model',
    } as const
    const waitingStatus = {
      ...scheduledStatus,
      idleExpiresAt: null,
      state: 'waiting',
      reason: 'waiting_for_stable_prefix',
      hasSnapshot: false,
      lastCacheHitTokens: 0,
    } as const
    const call = vi.spyOn(rpc, 'call')
      .mockResolvedValueOnce(scheduledStatus)
      .mockResolvedValueOnce(waitingStatus)
    const saved = vi.fn()
    const closed = vi.fn()

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:test',
      onSaved: saved,
      onClose: closed,
    })
    app.use(pinia)
    app.use(i18n)
    app.mount(host)
    await settle()

    expect(document.body.textContent).toContain('Provider cache lifetime')
    expect(document.body.textContent).toContain('Idle keepalive duration')
    expect(document.body.textContent).toContain('About every 4 min')
    expect(document.body.textContent).toContain('up to about 14 requests')
    expect(document.body.textContent).toContain('Scheduled')
    expect(document.body.textContent).not.toContain('Keepalive plan')
    expect(document.body.querySelector('.keepalive-dialog__plan')).toBeNull()
    const enableSwitch = document.body.querySelector<HTMLInputElement>(
      '[role="switch"][name="prompt_cache_keepalive_enabled"]',
    )
    expect(enableSwitch?.checked).toBe(true)
    expect(enableSwitch?.getAttribute('aria-checked')).toBe('true')
    expect(document.body.textContent).toContain('applies after saving')

    const ttlHelp = document.body.querySelector<HTMLElement>(
      '[aria-describedby="prompt-cache-keepalive-ttl-tip"]',
    )
    const ttlTooltip = document.body.querySelector<HTMLElement>(
      '#prompt-cache-keepalive-ttl-tip[role="tooltip"]',
    )
    expect(ttlHelp?.tagName).toBe('BUTTON')
    expect(ttlHelp?.hasAttribute('title')).toBe(false)
    expect(ttlTooltip?.textContent).toContain('cache lifetime published')

    const save = document.body.querySelector<HTMLButtonElement>('.btn--primary')
    save?.click()
    await settle()

    expect(call).toHaveBeenLastCalledWith(
      'sessions.promptCacheKeepalive.set',
      {
        key: 'agent:main:webchat:test',
        enabled: true,
        ttlSeconds: 300,
        idleTimeoutSeconds: 3_600,
      },
    )
    expect(saved).toHaveBeenCalledWith({
      sessionKey: 'agent:main:webchat:test',
      status: waitingStatus,
    })
    expect(closed).toHaveBeenCalledTimes(1)
    const enabledToasts = useToasts().toasts.value
    expect(enabledToasts[enabledToasts.length - 1]).toMatchObject({
      message: 'Keepalive enabled. It will begin after the next successful message.',
      tone: 'ok',
    })
    app.unmount()
  })

  it('confirms when keepalive is turned off', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const rpc = useRpcStore(pinia)
    const enabledStatus = {
      enabled: true,
      ttlSeconds: 300,
      intervalSeconds: 240,
      idleTimeoutSeconds: 3_600,
      idleExpiresAt: null,
      state: 'waiting',
      reason: 'waiting_for_stable_prefix',
      hasSnapshot: false,
      lastCacheHitTokens: 0,
    } as const
    const offStatus = {
      ...enabledStatus,
      enabled: false,
      state: 'off',
      reason: null,
    } as const
    vi.spyOn(rpc, 'call')
      .mockResolvedValueOnce(enabledStatus)
      .mockResolvedValueOnce(offStatus)

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:test',
    })
    app.use(pinia)
    app.use(i18n)
    app.mount(host)
    await settle()

    const checkbox = document.body.querySelector<HTMLInputElement>('[role="switch"]')
    if (!checkbox) throw new Error('keepalive toggle was not rendered')
    checkbox.click()
    await settle()
    expect(checkbox.checked).toBe(false)
    expect(checkbox.getAttribute('aria-checked')).toBe('false')
    document.body.querySelector<HTMLButtonElement>('.btn--primary')?.click()
    await settle()

    const disabledToasts = useToasts().toasts.value
    expect(disabledToasts[disabledToasts.length - 1]).toMatchObject({
      message: 'Prompt cache keepalive turned off.',
      tone: 'ok',
    })
    app.unmount()
  })

  it('blocks a keepalive window that cannot reach the next probe', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const rpc = useRpcStore(pinia)
    vi.spyOn(rpc, 'call').mockResolvedValue({
      enabled: true,
      ttlSeconds: 300,
      intervalSeconds: 240,
      idleTimeoutSeconds: 3_600,
      idleExpiresAt: null,
      state: 'waiting',
      reason: null,
      hasSnapshot: false,
      lastCacheHitTokens: 0,
    })

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:test',
    })
    app.use(pinia)
    app.use(i18n)
    app.mount(host)
    await settle()

    const ttl = document.body.querySelector<HTMLInputElement>(
      '[data-testid="prompt-cache-keepalive-ttl"]',
    )
    const idle = document.body.querySelector<HTMLInputElement>(
      '[data-testid="prompt-cache-keepalive-idle-timeout"]',
    )
    if (!ttl || !idle) throw new Error('timing inputs were not rendered')
    ttl.value = '60'
    ttl.dispatchEvent(new Event('input', { bubbles: true }))
    idle.value = '40'
    idle.dispatchEvent(new Event('input', { bubbles: true }))
    await settle()

    const save = document.body.querySelector<HTMLButtonElement>('.btn--primary')
    expect(save?.disabled).toBe(true)
    expect(document.body.textContent).toContain(
      'must be longer than the roughly 48-minute probe interval',
    )
    expect(document.body.textContent).not.toContain('about 0 requests')
    app.unmount()
  })

  it('replaces a missing-session transport error with actionable copy', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const rpc = useRpcStore(pinia)
    vi.spyOn(rpc, 'call').mockRejectedValue(new Error('Session not found'))

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:draft',
    })
    app.use(pinia)
    app.use(i18n)
    app.mount(host)
    await settle()

    const error = document.body.querySelector<HTMLElement>('[role="alert"]')
    expect(error?.textContent).toContain('This session is not available yet')
    expect(document.body.textContent).not.toContain('Session not found')

    app.unmount()
  })
})
