// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ChatComposer project draft', () => {
  it('shows both the project name and path and can return to the default workspace', async () => {
    const closeProject = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, {
      modelValue: '',
      'onUpdate:modelValue': () => {},
      attachments: [],
      busySendMode: 'queue',
      hasSendContent: false,
      isStreaming: false,
      canStop: false,
      isNewLanding: true,
      placeholder: 'Send a message',
      sendButtonTitle: 'Send',
      runMode: 'trusted',
      allowedRunModes: ['standard', 'trusted', 'full'],
      modelRoutingMode: 'off',
      modelRoutingSettingsBusy: false,
      routerVisualEffectsEnabled: true,
      codingModeEnabled: false,
      codingModeSettingsBusy: false,
      voiceBusy: false,
      voiceRecording: false,
      voiceReady: true,
      projectWorkspace: {
        id: 'project-a',
        name: 'Project A',
        path: 'D:\\repos\\project-a',
      },
      onCloseProject: closeProject,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-project-chip__name')?.textContent).toBe('Project A')
    expect(host.querySelector('.chat-project-chip__path')?.textContent).toBe('D:\\repos\\project-a')
    host.querySelector<HTMLButtonElement>('.chat-project-chip button')?.click()
    expect(closeProject).toHaveBeenCalledOnce()

    app.unmount()
  })
})
