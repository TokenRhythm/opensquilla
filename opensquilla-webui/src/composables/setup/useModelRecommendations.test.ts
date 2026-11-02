// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref } from 'vue'
import { useModelRecommendations } from './useModelRecommendations'
import { ProviderConfigurationError, type ModelRecommendation, type ProviderConfiguration } from '@/modules/providerConfiguration'

const toast = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: toast }) }))
const row = (model: string) => ({ role: 'c0', provider: 'tokenrhythm', model, thinkingLevel: 'high' })
const plan = (revision = 'one'): ModelRecommendation => ({ providerId: 'tokenrhythm', kind: 'router', revision, source: 'remote', rows: [row(revision)], currentRows: [row('saved')], hasUpdate: true, notified: false })
const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(fn => fn()); vi.useRealTimers(); toast.mockClear() })
async function fixture(supported = true) {
  let response = plan()
  const active = ref(true)
  const read = vi.fn(async () => ({ plans: [structuredClone(response)] }))
  const apply = vi.fn(async (_command: { providerId: string; kind: string; revision: string }) => ({ mode: 'router' }))
  const acknowledge = vi.fn(async () => ({ acknowledged: true }))
  const applied = vi.fn(async () => {})
  const uncertain = vi.fn(async () => {})
  const draft = { rows: [row('unsaved')], dirty: true }
  let api!: ReturnType<typeof useModelRecommendations>
  const element = document.createElement('div'); document.body.append(element)
  const app = createApp({ setup() {
    api = useModelRecommendations({ recommendationsSupported: supported, recommendations: read, applyRecommendation: apply, acknowledgeRecommendation: acknowledge } as unknown as ProviderConfiguration, { active, relevantProviders: ref(['tokenrhythm']), draft: () => draft, applied, uncertain })
    return () => h('div')
  } })
  app.mount(element)
  cleanups.push(() => { app.unmount(); element.remove() })
  await api.refresh(); await nextTick()
  return { api, read, apply, acknowledge, applied, uncertain, active, draft, publish: (revision: string) => { response = plan(revision) } }
}

describe('model recommendations remain separate from saved configuration', () => {
  it('background refresh and duplicate notifications never write or touch a draft', async () => {
    const f = await fixture()
    await f.api.refresh(); await f.api.refresh()
    expect(f.apply).not.toHaveBeenCalled(); expect(f.applied).not.toHaveBeenCalled()
    expect(f.draft.rows[0].model).toBe('unsaved')
    expect(f.acknowledge).toHaveBeenCalledTimes(1); expect(toast).toHaveBeenCalledTimes(1)
    expect(f.api.find('tokenrhythm', 'router')?.hasUpdate).toBe(true)
  })
  it('cancels without writes and freezes the exact revision while background updates continue', async () => {
    const f = await fixture()
    const cancelled = f.api.open('tokenrhythm', 'router'); await nextTick()
    expect(f.api.preview.value?.before[0].model).toBe('unsaved')
    f.api.close(); expect(await cancelled).toBe(false); expect(f.apply).not.toHaveBeenCalled()
    const saved = f.api.open('tokenrhythm', 'router'); await nextTick()
    f.publish('two'); await f.api.refresh()
    expect(f.api.preview.value?.plan.revision).toBe('one')
    await f.api.apply()
    expect(await saved).toBe(true)
    expect(f.apply).toHaveBeenCalledExactlyOnceWith({ providerId: 'tokenrhythm', kind: 'router', revision: 'one' })
    expect(f.applied).toHaveBeenCalledExactlyOnceWith('router')
    expect(f.api.find('tokenrhythm', 'router')?.revision).toBe('two')
  })
  it('conflict refreshes the preview and requires a new explicit confirmation', async () => {
    const f = await fixture()
    const saved = f.api.open('tokenrhythm', 'router'); await nextTick()
    f.publish('two'); f.apply.mockRejectedValueOnce(new ProviderConfigurationError('conflict', 'Changed'))
    await f.api.apply()
    expect(f.apply).toHaveBeenCalledTimes(1); expect(f.applied).not.toHaveBeenCalled()
    expect(f.api.preview.value?.plan.revision).toBe('two')
    expect(f.api.preview.value?.error).not.toBe('')
    await f.api.apply(); expect(await saved).toBe(true)
    expect(f.apply.mock.calls[1]).toEqual([{ providerId: 'tokenrhythm', kind: 'router', revision: 'two' }])
  })
  it('does not retry uncertain writes', async () => {
    const f = await fixture()
    const pending = f.api.open('tokenrhythm', 'router'); await nextTick()
    f.apply.mockRejectedValueOnce(new Error('timeout'))
    await f.api.apply(); expect(f.apply).toHaveBeenCalledTimes(1); expect(f.uncertain).toHaveBeenCalledTimes(1)
    expect(f.api.preview.value).not.toBeNull(); f.api.close(); expect(await pending).toBe(false)
  })
  it('discards an older in-flight read after saving instead of restoring its update notice', async () => {
    const f = await fixture()
    let release!: (result: { plans: ModelRecommendation[] }) => void
    f.read.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const oldRead = f.api.refresh()
    const saved = f.api.open('tokenrhythm', 'router'); await nextTick()
    const current = plan()
    current.currentRows = current.rows; current.hasUpdate = false; current.notified = true
    f.read.mockResolvedValueOnce({ plans: [current] })
    await f.api.apply(); expect(await saved).toBe(true)
    release({ plans: [plan()] }); await oldRead
    expect(f.api.find('tokenrhythm', 'router')?.hasUpdate).toBe(false)
  })
  it('performs no recommendation calls against an older Gateway', async () => {
    const f = await fixture(false)
    expect(f.read).not.toHaveBeenCalled(); expect(await f.api.open('tokenrhythm', 'router')).toBe(false)
    expect(f.apply).not.toHaveBeenCalled()
  })
  it('polls only while settings are active and stops after unmount', async () => {
    vi.useFakeTimers(); const f = await fixture()
    const count = f.read.mock.calls.length
    await vi.advanceTimersByTimeAsync(30_000); expect(f.read).toHaveBeenCalledTimes(count + 1)
    f.active.value = false; await nextTick()
    await vi.advanceTimersByTimeAsync(60_000); expect(f.read).toHaveBeenCalledTimes(count + 1)
  })
})
