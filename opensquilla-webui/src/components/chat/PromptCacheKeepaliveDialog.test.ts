// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { PROMPT_CACHE_LEASE_KEY } from '@/modules/promptCacheLease'
import { promptCacheLeaseTestDouble } from '@/testing/conversationAncillary.test-helper'
import PromptCacheKeepaliveDialog from './PromptCacheKeepaliveDialog.vue'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const enabledStatus: PromptCacheKeepaliveStatus = {
  enabled: true, ttlSeconds: 300, intervalSeconds: 240,
  idleTimeoutSeconds: 3_600, idleExpiresAt: null, state: 'waiting',
  reason: null, hasSnapshot: false, lastCacheHitTokens: 0,
}

async function mountChangingDialog(overrides: Parameters<typeof promptCacheLeaseTestDouble>[0]) {
  const props = reactive({ open: true, sessionKey: 'session-a' })
  const saved = vi.fn()
  const closed = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(PromptCacheKeepaliveDialog, {
    ...props, onSaved: saved, onClose: closed,
  }) })
  app.use(i18n)
  app.provide(PROMPT_CACHE_LEASE_KEY, promptCacheLeaseTestDouble(overrides))
  app.mount(host)
  await settle()
  return { app, props, saved, closed }
}

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
  it.each(['success', 'error'])('keeps an edited reopened draft when an earlier load finishes with %s', async (outcome) => {
    const first = deferred<PromptCacheKeepaliveStatus>()
    const second = deferred<PromptCacheKeepaliveStatus>()
    const status = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { app, props } = await mountChangingDialog({ status })
    try {
      props.open = false
      await settle()
      props.open = true
      await settle()
      second.resolve(enabledStatus)
      await settle()
      const ttl = document.body.querySelector<HTMLInputElement>('[data-testid="prompt-cache-keepalive-ttl"]')!
      ttl.value = '15'
      ttl.dispatchEvent(new Event('input', { bubbles: true }))
      await settle()

      if (outcome === 'success') first.resolve({ ...enabledStatus, ttlSeconds: 1_200 })
      else first.reject(new Error('Old request failed'))
      await settle()
      expect(document.body.querySelector<HTMLInputElement>('[data-testid="prompt-cache-keepalive-ttl"]')!.value).toBe('15')
      expect(document.body.querySelector('.keepalive-dialog__error')).toBeNull()
      expect(status).toHaveBeenCalledTimes(2)
    } finally { app.unmount() }
  })

  it('keeps the new opening loading when an older request settles', async () => {
    const first = deferred<PromptCacheKeepaliveStatus>()
    const second = deferred<PromptCacheKeepaliveStatus>()
    const { app, props } = await mountChangingDialog({
      status: vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise),
    })
    try {
      props.open = false
      await settle()
      props.open = true
      await settle()
      first.reject(new Error('Old request failed'))
      await settle()
      expect(document.body.querySelector('.keepalive-dialog__muted')).toBeTruthy()
      expect(document.body.querySelector('[data-testid="prompt-cache-keepalive-ttl"]')).toBeNull()
      expect(document.body.querySelector('.keepalive-dialog__error')).toBeNull()
      expect(document.body.querySelector<HTMLButtonElement>('.btn--primary')!.disabled).toBe(true)
      second.resolve(enabledStatus)
      await settle()
      expect(document.body.querySelector('.keepalive-dialog__muted')).toBeNull()
    } finally { app.unmount() }
  })

  it('uses the new session policy when the previous session load finishes late', async () => {
    const oldStatus = deferred<PromptCacheKeepaliveStatus>()
    const status = vi.fn().mockReturnValueOnce(oldStatus.promise).mockResolvedValue(enabledStatus)
    const setPolicy = vi.fn().mockResolvedValue(enabledStatus)
    const { app, props } = await mountChangingDialog({ status, setPolicy })
    try {
      props.sessionKey = 'session-b'
      await settle()
      oldStatus.resolve({ ...enabledStatus, enabled: false, ttlSeconds: 600 })
      await settle()
      document.body.querySelector<HTMLButtonElement>('.btn--primary')!.click()
      await settle()
      expect(status.mock.calls).toEqual([['session-a'], ['session-b']])
      expect(setPolicy).toHaveBeenCalledWith({
        key: 'session-b', enabled: true, ttlSeconds: 300, idleTimeoutSeconds: 3_600,
      })
    } finally { app.unmount() }
  })

  it('does not close or unlock a new session dialog when an earlier save finishes', async () => {
    const first = deferred<PromptCacheKeepaliveStatus>()
    const second = deferred<PromptCacheKeepaliveStatus>()
    const setPolicy = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { app, props, closed, saved } = await mountChangingDialog({
      status: vi.fn().mockResolvedValue(enabledStatus), setPolicy,
    })
    try {
      document.body.querySelector<HTMLButtonElement>('.btn--primary')!.click()
      await settle()
      props.sessionKey = 'session-b'
      await settle()
      document.body.querySelector<HTMLButtonElement>('.btn--primary')!.click()
      await settle()
      first.resolve(enabledStatus)
      await settle()
      expect(closed).not.toHaveBeenCalled()
      expect(saved).not.toHaveBeenCalled()
      expect(document.body.querySelector<HTMLButtonElement>('.btn--primary')!.disabled).toBe(true)
      expect(useToasts().toasts.value).toHaveLength(0)
      second.resolve(enabledStatus)
      await settle()
      expect(closed).toHaveBeenCalledTimes(1)
      expect(saved).toHaveBeenCalledWith({ sessionKey: 'session-b', status: enabledStatus })
    } finally { app.unmount() }
  })

  it('does not emit save feedback after being unmounted', async () => {
    const pending = deferred<PromptCacheKeepaliveStatus>()
    const { app, closed, saved } = await mountChangingDialog({
      status: vi.fn().mockResolvedValue(enabledStatus), setPolicy: vi.fn().mockReturnValue(pending.promise),
    })
    document.body.querySelector<HTMLButtonElement>('.btn--primary')!.click()
    await settle()
    app.unmount()
    pending.resolve(enabledStatus)
    await settle()
    expect(closed).not.toHaveBeenCalled()
    expect(saved).not.toHaveBeenCalled()
    expect(useToasts().toasts.value).toHaveLength(0)
  })

  it('explains the bounded plan and saves both timing values', async () => {
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
    const promptCacheStatus = vi.fn().mockResolvedValue(scheduledStatus)
    const setPromptCacheStatus = vi.fn().mockResolvedValue(waitingStatus)
    const promptCacheLease = promptCacheLeaseTestDouble({
      status: promptCacheStatus,
      setPolicy: setPromptCacheStatus,
    })
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
    app.use(i18n)
    app.provide(PROMPT_CACHE_LEASE_KEY, promptCacheLease)
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

    expect(setPromptCacheStatus).toHaveBeenLastCalledWith({
      key: 'agent:main:webchat:test',
      enabled: true,
      ttlSeconds: 300,
      idleTimeoutSeconds: 3_600,
    })
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
    const promptCacheLease = promptCacheLeaseTestDouble({
      status: vi.fn().mockResolvedValue(enabledStatus),
      setPolicy: vi.fn().mockResolvedValue(offStatus),
    })

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:test',
    })
    app.use(i18n)
    app.provide(PROMPT_CACHE_LEASE_KEY, promptCacheLease)
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
    const promptCacheLease = promptCacheLeaseTestDouble({
      status: vi.fn().mockResolvedValue({
        enabled: true,
        ttlSeconds: 300,
        intervalSeconds: 240,
        idleTimeoutSeconds: 3_600,
        idleExpiresAt: null,
        state: 'waiting',
        reason: null,
        hasSnapshot: false,
        lastCacheHitTokens: 0,
      }),
    })

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:test',
    })
    app.use(i18n)
    app.provide(PROMPT_CACHE_LEASE_KEY, promptCacheLease)
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
    const promptCacheLease = promptCacheLeaseTestDouble({
      status: vi.fn().mockRejectedValue(new Error('Session not found')),
    })

    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(PromptCacheKeepaliveDialog, {
      open: true,
      sessionKey: 'agent:main:webchat:draft',
    })
    app.use(i18n)
    app.provide(PROMPT_CACHE_LEASE_KEY, promptCacheLease)
    app.mount(host)
    await settle()

    const error = document.body.querySelector<HTMLElement>('[role="alert"]')
    expect(error?.textContent).toContain('This session is not available yet')
    expect(document.body.textContent).not.toContain('Session not found')

    app.unmount()
  })
})
