// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'

import { useCronForm } from './useCronForm'
import { DEFAULT_CRON_EXPRESSION } from '@/utils/cron/schedule'
import type { CronJobMutation, CronScheduler } from '@/modules/cronScheduler'

const rpcCall = vi.fn()
const confirmClose = vi.fn()
const toasts: { message: string; tone?: string }[] = []

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
}))

const scheduler = {
  saveJob: (input: CronJobMutation, options: { existing: boolean }) =>
    rpcCall(options.existing ? 'cron.update' : 'cron.create', input),
} as unknown as CronScheduler

vi.mock('@/composables/useConfirm', () => ({ useConfirm: () => ({ confirm: confirmClose }) }))

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({
    pushToast: (message: string, options?: { tone?: string }) =>
      toasts.push({ message, tone: options?.tone }),
  }),
}))

vi.mock('@/composables/useProjectWorkspaces', () => ({
  useProjectWorkspaces: () => ({
    workspaces: { value: [] },
    isLoading: { value: false },
    hasLoaded: { value: true },
    loadWorkspaces: vi.fn().mockResolvedValue(undefined),
  }),
}))

function mountForm() {
  const scope = effectScope()
  const api = scope.run(() => useCronForm(scheduler, { afterSaved: vi.fn() }))!
  return { api, dispose: () => scope.stop() }
}

let scopes: (() => void)[] = []

function cronForm() {
  const { api, dispose } = mountForm()
  scopes.push(dispose)
  return api
}

beforeEach(() => {
  rpcCall.mockReset().mockResolvedValue({})
  confirmClose.mockReset().mockResolvedValue(false)
  toasts.length = 0
})

afterEach(() => {
  while (scopes.length) scopes.pop()?.()
  scopes = []
  document.body.innerHTML = ''
})

describe('cron form default schedule', () => {
  it('starts a new job on the schedule the friendly picker already displays', () => {
    const form = cronForm()
    form.openPanel(null)
    expect(form.form.type).toBe('cron')
    expect(form.form.cron).toBe(DEFAULT_CRON_EXPRESSION)
  })

  it('saves the untouched default without the user visiting the frequency select', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Daily reminder'
    form.form.message = 'stand up'

    await form.saveJob()

    expect(toasts.filter(entry => entry.tone === 'danger')).toEqual([])
    expect(rpcCall).toHaveBeenCalledTimes(1)
    const [method, payload] = rpcCall.mock.calls[0] as [string, Record<string, unknown>]
    expect(method).toBe('cron.create')
    expect(payload.schedule).toMatchObject({ kind: 'cron', expr: DEFAULT_CRON_EXPRESSION })
  })

  it('exposes pending state and ignores a duplicate save while the scheduler is busy', async () => {
    let resolveSave: (() => void) | undefined
    rpcCall.mockImplementationOnce(() => new Promise<void>(resolve => { resolveSave = resolve }))
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Busy reminder'
    form.form.message = 'stand up'

    const firstSave = form.saveJob()
    await Promise.resolve()
    expect(form.saving.value).toBe(true)

    await form.saveJob()
    expect(rpcCall).toHaveBeenCalledTimes(1)

    expect(await form.closePanel()).toBe(false)
    expect(form.panelOpen.value).toBe(true)
    expect(confirmClose).not.toHaveBeenCalled()

    resolveSave?.()
    await firstSave
    expect(form.saving.value).toBe(false)
  })

  it('posts the disabled state selected for a new job', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Paused reminder'
    form.form.message = 'stand up'
    form.form.enabled = false

    await form.saveJob()

    expect(rpcCall).toHaveBeenCalledTimes(1)
    const [method, payload] = rpcCall.mock.calls[0] as [string, Record<string, unknown>]
    expect(method).toBe('cron.create')
    expect(payload.enabled).toBe(false)
  })

  it('renders the schedule preview immediately instead of the empty placeholder', () => {
    const form = cronForm()
    form.openPanel(null)
    expect(form.cronExplainValid.value).toBe(true)
    expect(form.cronExplainInvalid.value).toBe(false)
  })

  it('keeps a template expression rather than overwriting it with the default', () => {
    const form = cronForm()
    form.openPanel(null, { id: 'weekly-report', expression: '30 8 * * 1' })
    expect(form.form.cron).toBe('30 8 * * 1')
  })

  it('leaves the expression empty for schedule kinds that do not use one', () => {
    const form = cronForm()
    form.openPanel(null, { id: 'interval', scheduleKind: 'every', every_seconds: 300 })
    expect(form.form.type).toBe('every')
    expect(form.form.cron).toBe('')
  })

  it('restores an existing job exactly, including one saved without an expression', () => {
    const form = cronForm()
    form.openPanel({ id: 'job-1', name: 'x', scheduleKind: 'cron', expression: '' })
    expect(form.form.cron).toBe('')
  })
})

describe('cron form validation', () => {
  it.each([
    ['deliveryWebhookUrl', { deliveryMode: 'webhook' }, 'cp-delivery-webhook-url'],
    ['fdWebhookUrl', { fdMode: 'webhook', deliveryMode: 'webhook' }, 'cp-fd-webhook-url'],
    ['fdTo', { fdMode: 'channel' }, 'cp-fd-to'],
  ] as const)('reveals and focuses the invalid delivery field %s', async (field, values, inputId) => {
    document.body.innerHTML = `<details><details><input id="${inputId}"></details></details>`
    const form = cronForm()
    form.openPanel(null)
    Object.assign(form.form, { name: 'Delivery validation', ...values })

    await form.saveJob()

    expect(rpcCall).not.toHaveBeenCalled()
    expect(Object.keys(form.fieldErrors.value)).toEqual([field])
    expect(document.activeElement?.id).toBe(inputId)
    expect([...document.querySelectorAll('details')].every(details => details.open)).toBe(true)
  })

  it('reports a missing expression locally instead of posting it to the Gateway', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Cleared by hand'
    form.form.cron = '   '

    await form.saveJob()

    expect(rpcCall).not.toHaveBeenCalled()
    expect(form.fieldErrors.value.cron).toBeTruthy()
  })

  it('still guards the interval and ISO branches it always guarded', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Interval'
    form.form.type = 'every'
    form.form.every = '0'

    await form.saveJob()

    expect(rpcCall).not.toHaveBeenCalled()
    expect(form.fieldErrors.value.every).toBeTruthy()
  })
})

describe('cron draft protection', () => {
  it('closes an unchanged draft without confirmation', async () => {
    const form = cronForm()
    form.openPanel(null)
    expect(await form.closePanel()).toBe(true)
    expect(confirmClose).not.toHaveBeenCalled()
    expect(form.panelOpen.value).toBe(false)
  })

  it('shares a pending discard decision across repeated close requests', async () => {
    let resolveClose!: (discard: boolean) => void
    confirmClose.mockImplementationOnce(() => new Promise<boolean>(resolve => { resolveClose = resolve }))
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Unsaved draft'

    const firstClose = form.closePanel()
    const secondClose = form.closePanel()
    expect(confirmClose).toHaveBeenCalledTimes(1)
    expect(form.panelOpen.value).toBe(true)
    resolveClose(false)

    expect(await firstClose).toBe(false)
    expect(await secondClose).toBe(false)
    expect(form.panelOpen.value).toBe(true)
  })

  it('retains edits when discard is cancelled and closes only after confirmation', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Draft reminder'
    expect(await form.closePanel()).toBe(false)
    expect(form.panelOpen.value).toBe(true)
    expect(form.form.name).toBe('Draft reminder')
    confirmClose.mockResolvedValueOnce(true)
    expect(await form.closePanel()).toBe(true)
    expect(form.panelOpen.value).toBe(false)
  })

  it('retains a failed save for correction and allows retry', async () => {
    rpcCall.mockRejectedValueOnce(new Error('Synthetic save failure'))
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Retry reminder'
    await form.saveJob()
    expect(form.panelOpen.value).toBe(true)
    expect(form.saving.value).toBe(false)
    expect(form.form.name).toBe('Retry reminder')
    expect(form.saveError.value).toContain('Synthetic save failure')
    await form.saveJob()
    expect(rpcCall).toHaveBeenCalledTimes(2)
    expect(form.panelOpen.value).toBe(false)
    expect(form.saveError.value).toBe('')
  })

  it('focuses the invalid time zone without sending a request', async () => {
    document.body.innerHTML = '<input id="cp-tz">'
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Time zone reminder'
    form.form.tz = 'Invalid/Timezone'
    await form.saveJob()
    expect(rpcCall).not.toHaveBeenCalled()
    expect(form.fieldErrors.value.tz).toBeTruthy()
    expect(document.activeElement?.id).toBe('cp-tz')
  })
})
