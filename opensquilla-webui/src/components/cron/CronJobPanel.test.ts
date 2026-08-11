// @vitest-environment happy-dom

import { createApp, h, nextTick, reactive, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it } from 'vitest'
import CronJobPanel from './CronJobPanel.vue'
import type { CronJobFormModel } from '@/types/cron'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

function formModel(): CronJobFormModel {
  return {
    templateId: '',
    name: 'test',
    type: 'cron',
    cron: '0 9 * * *',
    every: '',
    at: '',
    tz: '',
    payloadKind: 'reminder',
    agentId: 'main',
    workspaceId: '',
    workspaceRequired: false,
    sessionTarget: 'isolated',
    targetSessionKey: '',
    message: 'hello',
    wakeMode: 'now',
    deliveryMode: '',
    deliveryChannel: '',
    deliveryTo: '',
    deliveryAccount: '',
    deliveryWebhookUrl: '',
    deliveryWebhookToken: '',
    deliveryBestEffort: false,
    fdMode: '',
    fdChannel: '',
    fdTo: '',
    fdAccount: '',
    fdWebhookUrl: '',
    fdWebhookToken: '',
    enabled: true,
  }
}

function mountPanel() {
  const open = ref(true)
  const form = reactive(formModel())
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    setup: () => () => h(CronJobPanel, {
      open: open.value,
      editingJob: null,
      form,
      'onUpdate:form': (next: CronJobFormModel) => Object.assign(form, next),
      cronExplainHuman: '',
      cronExplainValid: false,
      cronExplainInvalid: false,
      cronExplainUpcoming: [],
      jobModeHint: '',
      sessionTargetHint: '',
      showTargetSessionRow: false,
      targetSessionLabel: '',
      targetSessionHint: '',
      messageLabel: '',
      projectWorkspaces: [],
      projectWorkspacesLoading: false,
    }),
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    missingWarn: false,
    fallbackWarn: false,
    messages: { en: {} },
  }))
  app.mount(host)
  apps.push(app)
  return { form, open }
}

describe('CronJobPanel friendly schedule contracts', () => {
  it('writes a backend-valid offset timestamp from datetime-local input', async () => {
    const { form } = mountPanel()
    form.type = 'at'
    await nextTick()

    const input = document.querySelector<HTMLInputElement>('#cp-at-friendly')!
    input.value = '2026-05-18T09:00'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    expect(form.at).toMatch(/^2026-05-18T09:00:00[+-]\d{2}:\d{2}$/)
  })

  it('re-derives the friendly cron kind each time the panel opens', async () => {
    const { form, open } = mountPanel()
    await nextTick()

    const initial = document.querySelector<HTMLButtonElement>('#cp-repeat-kind')!
    expect(initial.textContent).toContain('cronSkills.panel.daily')
    initial.click()
    await nextTick()
    const options = document.querySelectorAll<HTMLButtonElement>('[role="option"]')
    options[options.length - 1].click()
    await nextTick()
    expect(initial.textContent).toContain('cronSkills.panel.customAdvancedTime')

    open.value = false
    await nextTick()
    form.cron = '0 9 * * *'
    open.value = true
    await nextTick()

    expect(
      document.querySelector<HTMLButtonElement>('#cp-repeat-kind')!.textContent,
    ).toContain('cronSkills.panel.daily')
  })

  it('uses themed selectors instead of the native time popup', async () => {
    const { form } = mountPanel()
    await nextTick()

    expect(document.querySelector('select')).toBeNull()
    expect(document.querySelector('input[type="time"]')).toBeNull()
    document.querySelector<HTMLButtonElement>('#cp-friendly-hour')!.click()
    await nextTick()
    const hourOptions = document.querySelectorAll<HTMLButtonElement>('[role="option"]')
    hourOptions[14].click()
    await nextTick()
    document.querySelector<HTMLButtonElement>('#cp-friendly-minute')!.click()
    await nextTick()
    const minuteOptions = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'))
    minuteOptions.find(option => option.textContent?.trim() === '30')!.click()
    await nextTick()

    expect(form.cron).toBe('30 14 * * *')
  })

  it('supports keyboard focus navigation and Escape in themed selectors', async () => {
    mountPanel()
    await nextTick()

    const trigger = document.querySelector<HTMLButtonElement>('#cp-repeat-kind')!
    trigger.focus()
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    await nextTick()
    expect(document.activeElement?.textContent).toContain('cronSkills.panel.daily')

    document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    expect(document.activeElement?.textContent).toContain('cronSkills.panel.weekdays')
    document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()

    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)
  })

  it('closes a themed selector when Tab moves focus to the next field', async () => {
    mountPanel()
    await nextTick()

    const trigger = document.querySelector<HTMLButtonElement>('#cp-repeat-kind')!
    const nextField = document.querySelector<HTMLButtonElement>('#cp-payload-kind-simple')!
    trigger.focus()
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    await nextTick()
    expect(trigger.getAttribute('aria-expanded')).toBe('true')

    nextField.focus()
    await nextTick()

    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(nextField)
  })
})
