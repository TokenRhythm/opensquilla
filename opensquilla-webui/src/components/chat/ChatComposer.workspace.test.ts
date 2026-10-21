// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'
import { applySessionProjectWorkspaceMetadata, useActiveProjectWorkspace } from '@/composables/useActiveProjectWorkspace'

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

  it('keeps the durable toolbar stable through repeated metadata refreshes while gating sending', async () => {
    const state = useActiveProjectWorkspace()
    const workspace = { id: 'a', name: 'A', path: '/a', available: true, removed: false }
    state.applySessionSnapshot('session', state.beginSessionResolution('session'), { workspaceId: 'a', projectWorkspace: workspace })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({ render: () => h(ChatComposer, composerProps({
      isNewLanding: false, canCloseProject: false, hasSendContent: true, modelValue: 'hello',
      projectWorkspace: state.activeWorkspace.value,
      projectWorkspaceStatus: state.presentationStatus.value,
      projectStatusMessage: state.presentationStatus.value === 'ready' ? '' : 'Project unavailable',
      sendBlockedMessage: state.sendBlockedReason.value ? 'Project check pending' : '',
    }) as never) })
    app.use(i18n)
    app.mount(host)
    const loadWorkspaces = vi.fn()
    for (let index = 0; index < 3; index++) {
      const generation = state.beginSessionRefresh('session')
      await nextTick()
      expect(host.querySelector('.chat-project-chip')).toBeNull()
      expect(host.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(true)
      await applySessionProjectWorkspaceMetadata({ state, sessionKey: 'session', generation,
        metadata: { workspaceId: 'a', projectWorkspace: workspace }, loadWorkspaces, isCurrent: () => true })
      await nextTick()
      expect(host.querySelector('.chat-project-chip')).toBeNull()
      expect(host.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(false)
    }
    state.applyWorkspaceRefresh({ ...workspace, available: false })
    await nextTick()
    expect(host.querySelector('.chat-project-chip')?.getAttribute('data-status')).toBe('unavailable')
    const chip = host.querySelector('.chat-project-chip')
    state.beginSessionRefresh('session')
    await nextTick()
    expect(host.querySelector('.chat-project-chip')).toBe(chip)
    expect(host.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(true)
    expect(loadWorkspaces).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps known chat visits settled and places visible workspace problems after the safety control', async () => {
    const state = useActiveProjectWorkspace()
    state.setPresentationScope('gateway/connection')
    const workspace = { id: 'a', name: 'A', path: '/a', available: true, removed: false }
    for (const key of ['a', 'b']) state.applySessionSnapshot(key, state.beginSessionVisit(key), {
      workspaceId: 'a', projectWorkspace: workspace,
    })
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({ render: () => h(ChatComposer, composerProps({
      isNewLanding: false, canCloseProject: false, hasSendContent: true, modelValue: 'hello',
      projectWorkspace: state.activeWorkspace.value, projectWorkspaceStatus: state.presentationStatus.value,
      projectStatusMessage: state.presentationStatus.value === 'ready' ? '' : 'Project unavailable',
      sendBlockedMessage: state.sendBlockedReason.value ? 'Project check pending' : '',
    }) as never) })
    app.use(i18n)
    app.mount(host)
    const safety = host.querySelector('.chat-run-mode-anchor')!
    for (const key of ['a', 'b', 'a']) {
      const generation = state.beginSessionVisit(key)
      await nextTick()
      expect(host.querySelector('.chat-project-chip')).toBeNull()
      expect(host.querySelector('.chat-run-mode-anchor')).toBe(safety)
      expect(host.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(true)
      await applySessionProjectWorkspaceMetadata({ state, sessionKey: key, generation,
        metadata: { workspaceId: 'a', projectWorkspace: workspace }, loadWorkspaces: vi.fn(), isCurrent: () => true })
    }
    state.applyWorkspaceRefresh({ ...workspace, available: false })
    await nextTick()
    const chip = host.querySelector('.chat-project-chip')!
    expect(chip.textContent).toContain('Project unavailable')
    expect(safety.compareDocumentPosition(chip) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
    expect(host.querySelector<HTMLButtonElement>('.chat-send-btn')?.disabled).toBe(true)
    app.unmount()
  })

  it.each([false, true])('disables project changes during a pending binding (selected=%s)', async selected => {
    const chooseProject = vi.fn()
    const closeProject = vi.fn()
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(ChatComposer, composerProps({
      ...(!selected ? { projectWorkspace: null } : {}),
      canCloseProject: selected,
      projectBindingBusy: true,
      onChooseProject: chooseProject,
      onCloseProject: closeProject,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const action = host.querySelector<HTMLButtonElement>(selected
      ? '.chat-project-chip button'
      : '.chat-project-choose')!
    expect(action.disabled).toBe(true)
    action.click()
    expect(chooseProject).not.toHaveBeenCalled()
    expect(closeProject).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps unavailable project context and disables sending without another send notice', async () => {
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
    const send = host.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(send.disabled).toBe(true)
    expect(send.hasAttribute('title')).toBe(false)
    expect(send.getAttribute('aria-describedby')).toBeNull()
    expect(host.querySelector('.chat-composer-send-status')).toBeNull()
    expect(host.querySelector('.chat-composer-status-announcement')).toBeNull()
    expect(host.querySelector('.chat-send-tooltip')).toBeNull()

    app.unmount()
  })
})
