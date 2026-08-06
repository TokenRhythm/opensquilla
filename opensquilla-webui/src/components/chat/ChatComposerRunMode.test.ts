// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import ChatComposerRunMode from './ChatComposerRunMode.vue'

let unmount: (() => void) | null = null

function mount(
  allowedRunModes: Array<'safe' | 'full'>,
  safeSetupAvailable = false,
  onSetRunMode?: (mode: 'safe' | 'full') => void,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposerRunMode, {
    runMode: 'full',
    allowedRunModes,
    safeSetupAvailable,
    onSetRunMode,
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        chat: {
          closeComposerSettings: 'Close',
          composer: {
            runMode: 'Run mode',
            runModeSafe: 'Safe',
            runModeSafeDesc: 'Sandboxed',
            runModeFull: 'Full access',
            runModeFullDesc: 'Host access',
          },
        },
      },
    },
  }))
  app.mount(el)
  unmount = () => app.unmount()
  return el
}

afterEach(() => {
  unmount?.()
  unmount = null
  document.body.innerHTML = ''
})

describe('ChatComposerRunMode', () => {
  it('renders exactly Safe and Full', () => {
    const el = mount(['safe', 'full'])
    const radios = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')]
    expect(radios).toHaveLength(2)
    expect(radios.map(radio => radio.textContent?.trim())).toEqual([
      'SafeSandboxed',
      'Full accessHost access',
    ])
  })

  it('quietly disables Safe when the capability is unavailable', () => {
    const el = mount(['full'])
    const radios = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')]
    expect(radios[0].disabled).toBe(true)
    expect(el.querySelector('.composer-run-mode__hint')).toBeNull()
  })

  it('lets a repairable Safe choice request first-time setup', () => {
    const selected: Array<'safe' | 'full'> = []
    const el = mount(['full'], true, mode => selected.push(mode))
    const radios = [...el.querySelectorAll<HTMLButtonElement>('[role="radio"]')]

    expect(radios[0].disabled).toBe(false)
    radios[0].click()

    expect(selected).toEqual(['safe'])
    expect(radios[0].getAttribute('aria-checked')).toBe('false')
  })
})
