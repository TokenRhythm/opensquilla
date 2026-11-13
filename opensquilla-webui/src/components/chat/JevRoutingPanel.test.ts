// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import { jevDecision, jevSnapshot, jevEnsemble, jevHandoffDecision } from '@/testing/jevRouting'
import JevRoutingPanel from './JevRoutingPanel.vue'

const apps: ReturnType<typeof createApp>[] = []
function mount(props: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(JevRoutingPanel, { snapshot: jevSnapshot(), ...props }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(host)
  apps.push(app)
  return host
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('JEV chat suggestion', () => {
  it.each([
    jevSnapshot(),
    jevSnapshot({ enabled: false, recommendation: jevDecision() }),
    jevSnapshot({ status: 'checking' }),
    jevSnapshot({ recommendation: jevDecision({ status: 'dismissed' }) }),
    jevSnapshot({ recommendation: jevDecision({ status: 'applied' }) }),
    jevSnapshot({ recommendation: jevDecision({ status: 'accepted' }) }),
    jevSnapshot({ recommendation: jevDecision({ status: 'expired' }) }),
    jevSnapshot({ recommendation: jevDecision({ action: 'keep' }) }),
    jevSnapshot({ recommendation: jevDecision({ target: null }) }),
  ])('leaves the chat clear without an actionable pending choice', snapshot => {
    const host = mount({ snapshot })
    expect(host.querySelector('.jev-panel')).toBeNull()
    expect(host.querySelector('button, input, select, form')).toBeNull()
  })

  it('only shows the suggestion and its actions, with all settings absent', () => {
    const host = mount({ snapshot: jevSnapshot({ recommendation: jevDecision() }) })
    expect(host.querySelector('.jev-panel__identity h3')!.textContent).toBe('Danbao suggests')
    expect(host.textContent).toContain('A compact task can use a smaller model.')
    expect(host.textContent).toContain('Short input')
    expect(host.textContent).toContain('large-model')
    expect(host.textContent).toContain('small-model')
    expect(host.querySelectorAll('button')).toHaveLength(2)
    expect(host.querySelector('input, select, form, .jev-panel__settings, .jev-panel__ledger')).toBeNull()
    expect(host.textContent).not.toContain('Decision model')
    expect(host.querySelector('[aria-modal]')).toBeNull()
    const panel = host.querySelector('.jev-panel')!
    expect(host.querySelector(`#${panel.getAttribute('aria-labelledby')}`)).not.toBeNull()
    expect(host.querySelector(`#${panel.getAttribute('aria-describedby')}`)!.textContent).toBe('A compact task can use a smaller model.')
  })

  it('emits manual confirmation and keeping the model for an in-flight request', () => {
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({ recommendation: jevDecision() }), turnBusy: true, onRespond })
    expect(host.textContent).toContain(en.chat.jev.safeBoundaryNote)
    const accept = host.querySelector<HTMLButtonElement>('.jev-panel__accept')!
    expect(accept.disabled).toBe(false)
    accept.click()
    host.querySelector<HTMLButtonElement>('.jev-panel__dismiss')!.click()
    expect(onRespond.mock.calls).toEqual([['decision-synthetic', true], ['decision-synthetic', false]])
  })

  it('guards both choices while a response mutation is pending', () => {
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({ recommendation: jevDecision() }), busy: true, onRespond })
    for (const button of host.querySelectorAll<HTMLButtonElement>('.jev-panel__actions button')) {
      expect(button.disabled).toBe(true)
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    }
    expect(onRespond).not.toHaveBeenCalled()
  })

  it('renders provider text safely, including reasons, evidence, and cost hints', () => {
    const host = mount({ snapshot: jevSnapshot({ recommendation: jevDecision({
      reason: '<img src=x onerror=alert(1)>', evidence: ['<script>unsafe()</script>'],
      costHint: 'Known lower token prices',
    }) }) })
    expect(host.querySelector('img, script')).toBeNull()
    expect(host.textContent).toContain('<img src=x onerror=alert(1)>')
    expect(host.textContent).toContain('<script>unsafe()</script>')
    expect(host.querySelector('.jev-panel__cost')!.textContent).toBe('Known lower token prices')
  })

  it('states that suggest mode resumes the original model at its deadline', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(10000)
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({
      mode: 'suggest', blocked: true, recommendation: jevDecision({ autoSwitchAt: 15000 }),
    }), turnBusy: true, onRespond })
    expect(host.querySelector('[role=timer]')!.textContent).toBe('Continue with the current model in 5s')
    expect(host.querySelector('.jev-panel__paused')!.textContent).toContain('Confirm to switch; otherwise continue with the original model')
    expect(host.querySelector('.jev-panel__badge')).toBeNull()
    await vi.advanceTimersByTimeAsync(2000)
    expect(host.querySelector('[role=timer]')!.textContent).toBe('Continue with the current model in 3s')
    await vi.advanceTimersByTimeAsync(3000)
    expect(host.querySelector('[role=timer]')!.textContent).toBe('Continuing with the current model…')
    expect(onRespond).not.toHaveBeenCalled()
    for (const button of host.querySelectorAll<HTMLButtonElement>('.jev-panel__actions button')) {
      expect(button.disabled).toBe(true)
    }
  })

  it('states that trust mode switches only when its countdown ends', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(10000)
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({
      mode: 'trust', blocked: true, recommendation: jevDecision({ autoSwitchAt: 15000 }),
    }), onRespond })
    expect(host.querySelector('.jev-panel__badge')!.textContent).toBe('Trust mode')
    expect(host.querySelector('[role=timer]')!.textContent).toContain('Switch automatically in 5s')
    expect(host.querySelector('.jev-panel__paused')!.textContent).toContain('When the countdown ends, switch models')
    await vi.advanceTimersByTimeAsync(5000)
    expect(host.querySelector('[role=timer]')!.textContent).toBe('Switching models and continuing…')
    expect(onRespond).not.toHaveBeenCalled()
  })

  it('updates countdown policy from current mode and closes after the server settles', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(10000)
    const state = reactive({ snapshot: jevSnapshot({
      mode: 'trust', recommendation: jevDecision({ autoSwitchAt: 15000 }),
    }) })
    const host = mount(state)
    state.snapshot = jevSnapshot({ mode: 'suggest', recommendation: jevDecision({ autoSwitchAt: 15000 }) })
    await nextTick()
    expect(host.querySelector('[role=timer]')!.textContent).toBe('Continue with the current model in 5s')
    state.snapshot = jevSnapshot({ mode: 'suggest', recommendation: null })
    await nextTick()
    await vi.advanceTimersByTimeAsync(100)
    expect(host.querySelector('.jev-panel')).toBeNull()
  })

  it('keeps older snapshots actionable without inventing a local timeout', () => {
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({ recommendation: jevDecision() }), onRespond })
    expect(host.querySelector('[role=timer]')).toBeNull()
    expect(host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.disabled).toBe(false)
    host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.click()
    expect(onRespond).toHaveBeenCalledExactlyOnceWith('decision-synthetic', true)
  })

  it('returns keyboard focus to the composer when a focused suggestion closes', async () => {
    const composer = document.createElement('textarea')
    document.body.append(composer)
    composer.focus()
    const state = reactive({ snapshot: jevSnapshot({ recommendation: jevDecision() }) })
    const host = mount(state)
    host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.focus()
    state.snapshot = jevSnapshot({ recommendation: null })
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(composer)
  })

  it('does not move focus when a background suggestion closes while the user types', async () => {
    const composer = document.createElement('textarea')
    document.body.append(composer)
    composer.focus()
    const state = reactive({ snapshot: jevSnapshot({ recommendation: jevDecision() }) })
    mount(state)
    state.snapshot = jevSnapshot({ recommendation: null })
    await nextTick()
    await nextTick()
    expect(document.activeElement).toBe(composer)
  })
})

describe('fusion and conversation handoff suggestions', () => {
  it.each(['suggest', 'trust'] as const)('shows the actual C4 lineup and %s timeout policy', mode => {
    vi.useFakeTimers()
    vi.setSystemTime(10000)
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({ mode, recommendation: jevDecision({
      action: 'ensemble', target: null, ensemble: jevEnsemble(), autoSwitchAt: 15000,
    }) }), onRespond })
    expect(host.textContent).toContain('C4 · Model fusion')
    expect(host.textContent).toContain('analyst-a')
    expect(host.textContent).toContain('analyst-b')
    expect(host.textContent).toContain('synthesis')
    expect(host.querySelector('[role=timer]')?.textContent).toContain(mode === 'suggest'
      ? 'Continue with the current model in 5s' : 'Switch automatically in 5s')
    host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.click()
    expect(onRespond).toHaveBeenCalledExactlyOnceWith('decision-synthetic', true)
  })

  it('never gives session migration an automatic deadline or trust badge', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(10000)
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({ mode: 'trust', blocked: true,
      recommendation: jevHandoffDecision({ autoSwitchAt: 15000 }),
    }), onRespond })
    expect(host.textContent).toContain('64,000 / 80,000 tokens · 80%')
    expect(host.textContent).toContain('Referenced attachments stay in the original conversation')
    expect(host.querySelector('[role=timer], .jev-panel__badge, .jev-panel__paused')).toBeNull()
    await vi.advanceTimersByTimeAsync(20000)
    expect(onRespond).not.toHaveBeenCalled()
    expect(host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.disabled).toBe(false)
    host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.click()
    expect(onRespond).toHaveBeenCalledExactlyOnceWith('decision-synthetic', true)
  })

  it('lets a running task continue and only disables migration confirmation', () => {
    const onRespond = vi.fn()
    const host = mount({ snapshot: jevSnapshot({ recommendation: jevHandoffDecision() }), turnBusy: true, onRespond })
    expect(host.querySelector<HTMLButtonElement>('.jev-panel__accept')!.disabled).toBe(true)
    expect(host.querySelector<HTMLButtonElement>('.jev-panel__dismiss')!.disabled).toBe(false)
    expect(host.textContent).toContain('current turn finishes')
    host.querySelector<HTMLButtonElement>('.jev-panel__dismiss')!.click()
    expect(onRespond).toHaveBeenCalledExactlyOnceWith('decision-synthetic', false)
  })

  it.each([
    jevDecision({ action: 'ensemble', target: null }),
    jevDecision({ action: 'ensemble', target: null, ensemble: jevEnsemble({ proposers: [] }) }),
    jevHandoffDecision({ handoff: { sourceSessionKey: 'another-session', contextTokens: 1, contextWindowTokens: 1 } }),
  ])('hides incomplete or wrongly owned action metadata', recommendation => {
    const host = mount({ snapshot: jevSnapshot({ recommendation }) })
    expect(host.querySelector('.jev-panel')).toBeNull()
  })
})

it.each([0, 20, 42])('renders %i completed conversation turns only from supplied handoff metadata', completedTurns => {
  const host = mount({ snapshot: jevSnapshot({ recommendation: jevHandoffDecision({ handoff: {
    sourceSessionKey: jevSnapshot().sessionKey, contextTokens: 64000, contextWindowTokens: 80000, completedTurns,
  } }) }) })
  expect(host.textContent).toContain(`Completed ${completedTurns} conversation turns · excludes tool iterations`)
  expect(host.querySelector('[role=timer]')).toBeNull()
})
