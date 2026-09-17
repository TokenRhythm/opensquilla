// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import i18n from '@/i18n'
import { MODEL_CAPACITY_KEY, capacityKey, useModelCapacityForm } from '@/composables/setup/useModelCapacityForm'
import type { ModelCapacityTarget, ProviderConfiguration } from '@/modules/providerConfiguration'
import SetupModelCapacity from './SetupModelCapacity.vue'

const target = { provider: 'custom', model: 'example.vendor/unknown.v1:latest' }
const row = (item: ModelCapacityTarget) => ({ ...item, localRuntime: false,
  contextWindow: { automatic: 8192, automaticSource: 'default', override: null, value: 8192, source: 'default', editable: true },
  maxOutputTokens: { automatic: 8192, automaticSource: 'default', override: null, value: 8192, source: 'default', editable: true },
})
const mounted: (() => void)[] = []
afterEach(() => { mounted.splice(0).forEach(dispose => dispose()); document.body.innerHTML = '' })
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await nextTick() }
async function mount(duplicate = false, inline = false, resolver?: (items: ModelCapacityTarget[]) => Promise<unknown>) {
  i18n.global.locale.value = 'en'
  const form = useModelCapacityForm({ capacitySupported: true,
    resolveCapacity: resolver || vi.fn(async (items: ModelCapacityTarget[]) => ({ models: items.map(row) })),
  } as unknown as ProviderConfiguration)
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp({ render: () => h('div', [h(SetupModelCapacity, { ...target, inline }), ...(duplicate ? [h(SetupModelCapacity, target)] : [])]) })
  app.use(i18n); app.provide(MODEL_CAPACITY_KEY, form); app.mount(host)
  mounted.push(() => app.unmount()); await flush()
  return { host, form }
}
function button(text: string) { return [...document.querySelectorAll<HTMLButtonElement>('button')].find(el => el.textContent!.includes(text))! }
async function input(value: string) {
  const el = document.querySelector<HTMLInputElement>('.model-capacity-dialog input')!
  el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); await flush()
}

describe('capacity editor', () => {
  it('separates automatic, unsaved and saved limits without repeating the automatic value', async () => {
    const { host, form } = await mount()
    host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!.click(); await flush()
    const dialog = document.querySelector('.model-capacity-dialog')!
    const context = dialog.querySelector<HTMLInputElement>('input')!
    expect(context.value).toBe('')
    expect(context.placeholder).toBe('8,192')
    expect(dialog.textContent).toContain('Automatic · System default')
    expect(dialog.textContent).not.toContain('8,192')
    expect(button('Restore automatic').disabled).toBe(true)
    await input('64000')
    expect(dialog.querySelector('.model-capacity-fields__unsaved')?.textContent).toContain('Not saved')
    expect(button('Restore automatic').disabled).toBe(false)
    const current = form.rows.get(capacityKey(target))!
    form.rows.set(capacityKey(target), { ...current, contextWindow: { ...current.contextWindow, override: 64000, value: 64000, source: 'override' } })
    await flush()
    expect(dialog.querySelector('.model-capacity-fields__unsaved')).toBeNull()
    expect(context.value).toBe('64000')
    button('Restore automatic').click(); await flush()
    expect(context.value).toBe('')
    expect(dialog.querySelector('.model-capacity-fields__unsaved')?.textContent).toContain('Not saved')
    expect(dialog.textContent).toContain('Automatic · System default')
  })
  it('shows a constrained effective value only for a saved override', async () => {
    const { host, form } = await mount()
    const current = form.rows.get(capacityKey(target))!
    form.rows.set(capacityKey(target), { ...current, maxOutputTokens: { ...current.maxOutputTokens, override: 64000, value: 32000, source: 'override' } })
    host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!.click(); await flush()
    const dialog = document.querySelector('.model-capacity-dialog')!
    expect(dialog.textContent).toContain('Effective limit: 32,000 tokens')
    const output = dialog.querySelectorAll<HTMLInputElement>('input')[1]!
    output.value = '16000'; output.dispatchEvent(new Event('input', { bubbles: true })); await flush()
    expect(dialog.textContent).not.toContain('Effective limit:')
    expect(dialog.textContent).toContain('Not saved')
  })
  it('keeps server-controlled output limits read-only in the editor', async () => {
    const { host, form } = await mount()
    const current = form.rows.get(capacityKey(target))!
    form.rows.set(capacityKey(target), {
      ...current,
      contextWindow: { ...current.contextWindow, source: 'catalog' },
      maxOutputTokens: { ...current.maxOutputTokens, editable: false },
    })
    await flush()
    expect(host.querySelector('.model-capacity-warning')).toBeNull()
    host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!.click(); await flush()
    expect(document.querySelectorAll<HTMLInputElement>('.model-capacity-dialog input')[1]!.disabled).toBe(true)
  })
  it('re-reads an in-flight initial query after discovery invalidates an empty cache', async () => {
    let release!: (result: unknown) => void
    const resolver = vi.fn(async (items: ModelCapacityTarget[]): Promise<unknown> => ({ models: items.map(row) }))
    resolver.mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    const { host, form } = await mount(false, false, resolver)
    expect(form.rows.size).toBe(0)
    form.invalidate()
    await flush(); await flush()
    expect(form.rows.get(capacityKey(target))?.contextWindow.value).toBe(8192)
    host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!.click(); await flush()
    expect(document.querySelector('.model-capacity-dialog')?.textContent).toContain('Automatic · System default')
    release({ models: [] }); await flush()
    expect(form.rows.size).toBe(1)
    expect(resolver).toHaveBeenCalledTimes(2)
  })
  it('keeps model entries compact and shows default limits only inside settings', async () => {
    const { host } = await mount(true)
    expect(host.querySelectorAll('.model-capacity-warning')).toHaveLength(0)
    expect(host.querySelectorAll('.model-capacity-trigger')).toHaveLength(2)
    expect(host.textContent).not.toContain('8,192')
    host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!.click(); await flush()
    expect(document.querySelector('.model-capacity-dialog')?.textContent).toContain('Automatic · System default')
  })
  it('keeps cancel local and completes into the shared unsaved draft', async () => {
    const { host, form } = await mount()
    const trigger = host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!
    trigger.focus(); trigger.click(); await flush()
    await input('32000')
    button('Cancel').click(); await flush()
    expect(form.dirty('modelStrategy')).toBe(false)
    expect(document.activeElement).toBe(trigger)
    trigger.click(); await flush(); await input('64000')
    button('Done').click(); await flush()
    expect(form.values(target).contextWindow).toBe('64000')
    expect(document.querySelector('[role="dialog"]')).toBeNull()
  })
  it('disables Done on invalid input and restores automatic explicitly', async () => {
    const { host, form } = await mount()
    host.querySelector<HTMLButtonElement>('.model-capacity-trigger')!.click(); await flush()
    await input('2e5')
    expect(button('Done').disabled).toBe(true)
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('positive')
    expect(form.dirty('modelStrategy')).toBe(false)
    button('Restore automatic').click(); await flush()
    expect(button('Done').disabled).toBe(false)
    button('Done').click(); await flush()
    expect(form.dirty('modelStrategy')).toBe(false)
  })
  it('starts the provider disclosure collapsed and writes invalid drafts without coercion', async () => {
    const { host, form } = await mount(false, true)
    expect(host.querySelector('details')!.open).toBe(false)
    const field = host.querySelector<HTMLInputElement>('input')!
    field.value = '-1'; field.dispatchEvent(new Event('input', { bubbles: true })); await flush()
    expect(form.valid('modelStrategy')).toBe(false)
    expect(form.values(target).contextWindow).toBe('-1')
  })
})
