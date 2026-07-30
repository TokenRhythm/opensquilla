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
    name: 'test',
    type: 'cron',
    cron: '0 9 * * *',
    every: '',
    at: '',
    tz: '',
    payloadKind: 'reminder',
    agentId: 'main',
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

    const initial = document.querySelector<HTMLSelectElement>('#cp-repeat-kind')!
    expect(initial.value).toBe('daily')
    initial.value = 'custom'
    initial.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(initial.value).toBe('custom')

    open.value = false
    await nextTick()
    form.cron = '0 9 * * *'
    open.value = true
    await nextTick()

    expect(
      document.querySelector<HTMLSelectElement>('#cp-repeat-kind')!.value,
    ).toBe('daily')
  })
})
