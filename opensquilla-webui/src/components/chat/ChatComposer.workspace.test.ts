// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ChatComposer project draft', () => {
  function composerProps(overrides: Record<string, unknown> = {}) {
    return {
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
      runMode: 'safe',
      allowedRunModes: ['safe', 'full'],
      runModeLocked: false,
      runModeLockMessage: '',
      sessionRoutingMode: 'off',
      sessionRoutingBusy: false,
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
      ...overrides,
    }
  }

  it('uses a folder-styled project chooser', async () => {
    const chooseProject = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, composerProps({
      projectWorkspace: null,
      onChooseProject: chooseProject,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const chooser = host.querySelector<HTMLButtonElement>('.chat-project-choose')
    expect(chooser?.innerHTML).toContain('M3 6.5')
    expect(chooser?.closest('.chat-input-footer')).toBeTruthy()
    expect(chooser?.closest('.chat-input-panel')).toBeTruthy()
    chooser?.click()
    expect(chooseProject).toHaveBeenCalledOnce()

    app.unmount()
  })

  it('hides the project chooser when the principal cannot create projects', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, composerProps({
      projectWorkspace: null,
      canChooseProject: false,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-project-choose')).toBeNull()

    app.unmount()
  })

  it('shows a new project draft in the bottom toolbar and lets it close', async () => {
    const closeProject = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, composerProps({
      canCloseProject: true,
      onCloseProject: closeProject,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-project-chip__name')?.textContent).toBe('Project A')
    expect(host.querySelector('.chat-project-chip__path')).toBeNull()
    expect(host.querySelector('.chat-project-chip')?.getAttribute('title')).toBe('D:\\repos\\project-a')
    expect(host.querySelector('.chat-project-chip')?.closest('.chat-input-footer')).toBeTruthy()
    expect(host.querySelector('.chat-project-chip')?.parentElement?.classList.contains('chat-input-actions--left')).toBe(true)
    host.querySelector<HTMLButtonElement>('.chat-project-chip button')?.click()
    expect(closeProject).toHaveBeenCalledOnce()

    app.unmount()
  })

  it('hides redundant project context inside a durable project task', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, composerProps({
      isNewLanding: false,
      canCloseProject: false,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-project-chip')).toBeNull()

    app.unmount()
  })

  it('announces an unavailable active project and disables sending', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, composerProps({
      isNewLanding: false,
      canCloseProject: false,
      modelValue: 'hello',
      hasSendContent: true,
      projectWorkspaceStatus: 'unavailable',
      projectStatusMessage: 'This project directory is unavailable.',
      sendBlockedMessage: 'This project directory is unavailable.',
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.chat-project-chip')?.getAttribute('data-status')).toBe('unavailable')
    expect(host.querySelector('.chat-project-chip__status')?.textContent).toContain('unavailable')
    expect(host.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(true)
    expect(host.querySelector('#chat-composer-send-status')?.textContent).toContain('unavailable')

    app.unmount()
  })
})
