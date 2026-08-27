// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'

import { useCronForm } from './useCronForm'
import { DEFAULT_CRON_EXPRESSION } from '@/utils/cron/schedule'

const rpcCall = vi.fn()
const toasts: { message: string; tone?: string }[] = []

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: rpcCall }),
}))

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
  const api = scope.run(() => useCronForm({ afterSaved: vi.fn() }))!
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
  toasts.length = 0
})

afterEach(() => {
  while (scopes.length) scopes.pop()?.()
  scopes = []
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
  it('reports a missing expression locally instead of posting it to the Gateway', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Cleared by hand'
    form.form.cron = '   '

    await form.saveJob()

    expect(rpcCall).not.toHaveBeenCalled()
    expect(toasts).toHaveLength(1)
    expect(toasts[0].tone).toBe('danger')
  })

  it('still guards the interval and ISO branches it always guarded', async () => {
    const form = cronForm()
    form.openPanel(null)
    form.form.name = 'Interval'
    form.form.type = 'every'
    form.form.every = '0'

    await form.saveJob()

    expect(rpcCall).not.toHaveBeenCalled()
    expect(toasts[0].tone).toBe('danger')
  })
})
