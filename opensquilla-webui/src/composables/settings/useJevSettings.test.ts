// @vitest-environment happy-dom
import { effectScope, type EffectScope } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppSettingsError, type AppSettings } from '@/modules/appSettings'
import { useJevSettings } from './useJevSettings'

const scopes: EffectScope[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })
function setup(value: Record<string, unknown> = {}) {
  const read = vi.fn().mockResolvedValue(value)
  const patch = vi.fn().mockResolvedValue({ restartRequired: false })
  const scope = effectScope()
  scopes.push(scope)
  const api = scope.run(() => useJevSettings({ settings: { read, patch } as unknown as AppSettings }))!
  return { api, read, patch, scope }
}

describe('Danbao global settings', () => {
  it('reads only its own config section and migrates a legacy unlimited delay', async () => {
    const { api, read } = setup({ enabled: true, mode: 'suggest', auto_switch_seconds: 0, max_prompts_per_hour: 3 })
    await api.load()
    expect(read).toHaveBeenCalledExactlyOnceWith('jev_router', { signal: expect.any(AbortSignal) })
    expect(api.draft.value.auto_switch_seconds).toBe(10)
    expect(api.dirty.value).toBe(false)
  })

  it('patches only edited fields and preserves unrelated JEV config', async () => {
    const { api, patch } = setup({ enabled: true, mode: 'suggest', timeout_seconds: 7, confidence_threshold: 0.8 })
    await api.load()
    api.draft.value.mode = 'trust'
    api.draft.value.auto_switch_seconds = 5
    expect(await api.save()).toBe(true)
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'jev_router.mode', value: 'trust' },
      { path: 'jev_router.auto_switch_seconds', value: 5 },
    ], { signal: expect.any(AbortSignal) })
    expect(api.dirty.value).toBe(false)
  })

  it('retains an unsaved draft after a rejected save and supports explicit discard', async () => {
    const { api, patch } = setup({ enabled: true, mode: 'suggest' })
    await api.load()
    api.draft.value.mode = 'trust'
    patch.mockRejectedValue(new AppSettingsError('conflict', 'Synthetic conflict'))
    expect(await api.save()).toBe(false)
    expect(api.dirty.value).toBe(true)
    expect(api.draft.value.mode).toBe('trust')
    expect(api.errorKey.value).toBe('settings.jev.conflict')
    api.discard()
    expect(api.draft.value.mode).toBe('suggest')
    expect(api.dirty.value).toBe(false)
  })

  it('serializes save clicks and does not discard a draft during a pending write', async () => {
    const { api, patch } = setup({ enabled: false })
    await api.load()
    api.draft.value.enabled = true
    let finish!: () => void
    patch.mockImplementation(() => new Promise(resolve => { finish = () => resolve({}) }))
    const pending = api.save()
    expect(api.busy.value).toBe(true)
    expect(await api.save()).toBe(false)
    api.discard()
    expect(api.draft.value.enabled).toBe(true)
    finish()
    expect(await pending).toBe(true)
    expect(patch).toHaveBeenCalledOnce()
  })

  it('does not save an invalid waiting time', async () => {
    const { api, patch } = setup({ enabled: true })
    await api.load()
    api.draft.value.auto_switch_seconds = 0
    expect(await api.save()).toBe(false)
    expect(patch).not.toHaveBeenCalled()
    expect(api.errorKey.value).toBe('settings.jev.invalid')
  })
})

describe('fusion and context settings compatibility', () => {
  it('defaults missing fields without marking existing installations dirty', async () => {
    const { api, patch } = setup({ enabled: true })
    await api.load()
    expect(api.draft.value).toMatchObject({ ensemble_enabled: true, session_restart_enabled: true,
      context_restart_ratio: 0.8, context_restart_min_tokens: 32000, context_restart_min_turns: 20, loop_interval: 5 })
    expect(api.dirty.value).toBe(false)
    expect(await api.save()).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps an existing three-loop preference and only patches edited expansion settings', async () => {
    const { api, patch } = setup({ enabled: true, loop_interval: 3 })
    await api.load()
    expect(api.draft.value.loop_interval).toBe(3)
    api.draft.value.ensemble_enabled = false
    api.draft.value.session_restart_enabled = false
    api.draft.value.context_restart_ratio = 0.9
    api.draft.value.context_restart_min_tokens = 64000
    api.draft.value.context_restart_min_turns = 30
    expect(await api.save()).toBe(true)
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'jev_router.ensemble_enabled', value: false },
      { path: 'jev_router.session_restart_enabled', value: false },
      { path: 'jev_router.context_restart_ratio', value: 0.9 },
      { path: 'jev_router.context_restart_min_tokens', value: 64000 },
      { path: 'jev_router.context_restart_min_turns', value: 30 },
    ], { signal: expect.any(AbortSignal) })
  })

  it.each([
    { context_restart_ratio: 0.49 }, { context_restart_ratio: 0.96 },
    { context_restart_ratio: Number.NaN }, { context_restart_min_tokens: 1023 },
    { context_restart_min_tokens: 2000001 }, { context_restart_min_tokens: 32000.5 },
    { context_restart_min_turns: 1 }, { context_restart_min_turns: 1001 },
    { context_restart_min_turns: 2.5 }, { context_restart_min_turns: Number.NaN },
  ])('does not send invalid context thresholds %j', async invalid => {
    const { api, patch } = setup({ enabled: true })
    await api.load()
    Object.assign(api.draft.value, invalid)
    expect(await api.save()).toBe(false)
    expect(patch).not.toHaveBeenCalled()
  })
})

it.each([2, 37, 1000])('preserves a saved conversation minimum of %i turns', async minimum => {
  const { api } = setup({ context_restart_min_turns: minimum })
  await api.load()
  expect(api.draft.value.context_restart_min_turns).toBe(minimum)
  expect(api.dirty.value).toBe(false)
})
