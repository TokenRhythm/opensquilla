// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'

import i18n, { loadLocaleMessages } from '@/i18n'
import type { Attachment } from '@/types/chat'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
  modelValue: '',
  'onUpdate:modelValue': () => {},
  busySendMode: 'queue',
  hasSendContent: false,
  isStreaming: false,
  canStop: false,
  isNewLanding: false,
  placeholder: 'Send a message',
  sendButtonTitle: 'Send',
  runMode: 'safe',
  allowedRunModes: ['safe', 'full'],
  sessionRoutingMode: 'off',
  sessionRoutingBusy: false,
  routerVisualEffectsEnabled: true,
  codingModeEnabled: false,
  codingModeSettingsBusy: false,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer attachment localization', () => {
  it('localizes failed status, fallback file label, and retry accessibility text', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const attachments: Attachment[] = [
      {
        kind: 'failed',
        local_id: 1,
        name: '报告.pdf',
        mime: 'application/pdf',
        file: new File(['content'], '报告.pdf', { type: 'application/pdf' }),
      },
      {
        kind: 'staged',
        local_id: 2,
        name: '无类型附件',
        mime: '',
      },
    ]
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, attachments })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const chips = el.querySelectorAll<HTMLElement>('.attachment-chip')
    const retry = chips[0]?.querySelector<HTMLButtonElement>(
      '.attachment-action:not(.attachment-remove)',
    )
    expect(chips[0]?.querySelector('.attachment-chip__meta')?.textContent).toBe('已失败')
    expect(chips[0]?.title).toBe('报告.pdf 上传失败')
    expect(retry?.title).toBe('重新上传')
    expect(retry?.getAttribute('aria-label')).toBe('重新上传')
    expect(chips[1]?.querySelector('.attachment-chip__meta')?.textContent).toBe('文件')

    app.unmount()
  })
})
