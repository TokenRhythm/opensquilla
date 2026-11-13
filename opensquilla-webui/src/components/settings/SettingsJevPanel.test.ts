// @vitest-environment happy-dom
import { createApp, nextTick, type App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import { APP_SETTINGS_KEY, type AppSettings } from '@/modules/appSettings'
import SettingsJevPanel from './SettingsJevPanel.vue'

let app: App | undefined
afterEach(() => { app?.unmount(); document.body.innerHTML = '' })
async function mount(props: Record<string, unknown> = {}) {
  const read = vi.fn().mockResolvedValue({ enabled: true, mode: 'suggest', auto_switch_seconds: 10 })
  const patch = vi.fn().mockResolvedValue({})
  const dirty = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(SettingsJevPanel, { onDirtyChange: dirty, ...props })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(APP_SETTINGS_KEY, { read, patch } as unknown as AppSettings)
  const api = app.mount(el) as unknown as { save(): Promise<boolean>; discard(): void }
  await vi.waitFor(() => expect(el.querySelector('[name="jev_wait"]')).not.toBeNull())
  await nextTick()
  return { el, patch, dirty, api }
}

describe('Danbao settings panel', () => {
  it('shows global scope and the different timeout outcomes for both modes', async () => {
    const { el } = await mount()
    expect(el.textContent).toContain('Applies to all conversations')
    expect(el.textContent).toContain('keep the original model when the countdown ends')
    const trust = Array.from(el.querySelectorAll<HTMLButtonElement>('.danbao-settings__modes button'))[1]!
    trust.click()
    await nextTick()
    expect(trust.getAttribute('aria-pressed')).toBe('true')
    expect(el.textContent).toContain('switch for you after the countdown')
    expect(el.querySelector('[name="jev_budget"]')).not.toBeNull()
    expect(el.querySelector('[name="jev_interval"]')).not.toBeNull()
    expect(el.textContent).toContain('NeoHorse-Jev-4B')
    expect(el.textContent).not.toContain('API Key')
  })

  it('reports a draft to the shared save bar and persists mode and waiting time', async () => {
    const { el, patch, dirty, api } = await mount()
    Array.from(el.querySelectorAll<HTMLButtonElement>('.danbao-settings__modes button'))[1]!.click()
    const wait = el.querySelector<HTMLSelectElement>('[name="jev_wait"]')!
    wait.value = '5'
    wait.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(dirty).toHaveBeenLastCalledWith(true)
    expect(await api.save()).toBe(true)
    await nextTick()
    expect(patch).toHaveBeenCalledWith(expect.arrayContaining([
      { path: 'jev_router.mode', value: 'trust' },
      { path: 'jev_router.auto_switch_seconds', value: 5 },
    ]), expect.anything())
    expect(dirty).toHaveBeenLastCalledWith(false)
  })
})

it('keeps fusion and handoff controls in Settings with an actual configured lineup', async () => {
  const model = (name: string, role: 'proposer' | 'aggregator') => ({
    key: name, model: name, provider: 'example', enabled: true, role, source: 'manual',
    credential: { provider: 'example', available: true, source: 'explicit' },
  })
  const open = vi.fn()
  const { el } = await mount({ ensembleSaved: true, onOpenModelStrategy: open,
    ensemble: { scheme: 'preset', fixedProfile: {
      providerLabel: 'Example', proposers: [model('analyst-a', 'proposer'), model('analyst-b', 'proposer')],
      aggregator: model('synthesis', 'aggregator'),
    }, custom: {} },
  })
  expect(el.textContent).toContain('C4 model fusion')
  expect(el.textContent).toContain('analyst-a')
  expect(el.textContent).toContain('analyst-b')
  expect(el.textContent).toContain('synthesis')
  expect(el.textContent).toContain('Configured lineup')
  expect(el.textContent).toContain('including in Trust mode')
  expect(el.textContent).toContain('must all reach their thresholds before JEV decides')
  expect(el.textContent).toContain('If its window is unknown, no reminder is shown')
  expect(el.textContent).toContain('A topic change or many turns alone cannot trigger a reminder')
  expect(el.textContent).toContain('Tool iterations do not count')
  expect(el.querySelector<HTMLInputElement>('[name="jev_context_turns"]')?.value).toBe('20')
  expect(el.querySelector('[name="jev_ensemble"]')).not.toBeNull()
  expect(el.querySelector('[name="jev_session"]')).not.toBeNull()
  expect(el.querySelector('[name="jev_context_ratio"]')).not.toBeNull()
  expect(el.querySelector('[name="jev_context_tokens"]')).not.toBeNull()
  el.querySelector<HTMLButtonElement>('.danbao-settings__link')!.click()
  expect(open).toHaveBeenCalledOnce()
})

it('saves conversation-turn eligibility independently from the Agent iteration interval', async () => {
  const { el, patch, api } = await mount()
  const turns = el.querySelector<HTMLInputElement>('[name="jev_context_turns"]')!
  turns.value = '30'
  turns.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  expect(el.querySelector<HTMLSelectElement>('[name="jev_interval"]')?.value).toBe('5')
  expect(await api.save()).toBe(true)
  expect(patch).toHaveBeenCalledWith([{ path: 'jev_router.context_restart_min_turns', value: 30 }], expect.anything())
})


it('connects all three threshold controls to the joint eligibility rule and preserves saved field names', async () => {
  const { el, patch, api } = await mount()
  const turns = el.querySelector<HTMLInputElement>('[name="jev_context_turns"]')!
  const ratio = el.querySelector<HTMLSelectElement>('[name="jev_context_ratio"]')!
  const tokens = el.querySelector<HTMLInputElement>('[name="jev_context_tokens"]')!
  expect(turns.value).toBe('20')
  expect(ratio.value).toBe('0.8')
  expect(tokens.value).toBe('32000')
  for (const control of [turns, ratio, tokens]) {
    const descriptions = control.getAttribute('aria-describedby')!.split(' ')
      .map(id => el.querySelector(`#${id}`)?.textContent).join(' ')
    expect(descriptions).toContain('must all reach their thresholds before JEV decides')
  }
  turns.value = '30'
  turns.dispatchEvent(new Event('input', { bubbles: true }))
  ratio.value = '0.9'
  ratio.dispatchEvent(new Event('change', { bubbles: true }))
  tokens.value = '64000'
  tokens.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  expect(await api.save()).toBe(true)
  expect(patch).toHaveBeenCalledWith([
    { path: 'jev_router.context_restart_ratio', value: 0.9 },
    { path: 'jev_router.context_restart_min_tokens', value: 64000 },
    { path: 'jev_router.context_restart_min_turns', value: 30 },
  ], expect.anything())
})

it('disables all continuity thresholds when reminders are turned off', async () => {
  const { el } = await mount()
  const enabled = el.querySelector<HTMLInputElement>('[name="jev_session"]')!
  enabled.checked = false
  enabled.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
  for (const name of ['jev_context_turns', 'jev_context_ratio', 'jev_context_tokens']) {
    expect(el.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)!.disabled).toBe(true)
  }
})
