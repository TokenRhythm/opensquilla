// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import type { DesktopUpdateState } from '@/platform'
import type { SystemHeaderLayout } from '@/utils/headerLayout'

const mocks = vi.hoisted(() => ({
  controller: null as unknown,
}))

vi.mock('@/composables/useDesktopUpdate', () => ({
  useDesktopUpdate: () => mocks.controller,
}))

import ChatSystemStatus from './ChatSystemStatus.vue'

const mountedApps: App[] = []

function updateState(overrides: Partial<DesktopUpdateState> = {}): DesktopUpdateState {
  return {
    status: 'idle',
    currentVersion: '1.0.0',
    latestVersion: null,
    progress: null,
    checkedAt: null,
    error: null,
    errorCode: null,
    snoozedUntil: null,
    canCheck: false,
    canNativeInstall: false,
    installMode: 'unsupported',
    releaseUrl: null,
    source: null,
    fallbackUsed: false,
    ...overrides,
  }
}

type StatusProps = {
  layout: SystemHeaderLayout
  connectionState: 'connected' | 'connecting' | 'disconnected'
  connectionLabel: string
  approvalCount: number
  canManageConnection: boolean
}

async function mountStatus(overrides: Partial<StatusProps> = {}) {
  const props = reactive<StatusProps>({
    layout: 'wide',
    connectionState: 'connected',
    connectionLabel: 'Connected',
    approvalCount: 0,
    canManageConnection: true,
    ...overrides,
  })
  const events = {
    connection: vi.fn(),
    approval: vi.fn(),
    update: vi.fn(),
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    setup: () => () => h(ChatSystemStatus, {
      ...props,
      onOpenConnection: events.connection,
      onOpenApproval: events.approval,
      onOpenUpdate: events.update,
    }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  await nextTick()
  await nextTick()
  return { host, props, events }
}

function byTestId(root: ParentNode, testId: string): HTMLElement | null {
  return root.querySelector<HTMLElement>(`[data-testid="${testId}"]`)
}

function key(target: Element, value: string, shiftKey = false) {
  target.dispatchEvent(new KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    key: value,
    shiftKey,
  }))
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  mocks.controller = {
    state: ref(updateState()),
    ready: ref(true),
    loading: ref(false),
    actionBusy: ref(false),
    visible: ref(false),
    latestVersion: ref('1.0.0'),
    localizedError: ref('raw transport detail'),
    isNativeDesktopUpdate: ref(false),
    isManagedDesktopUpdate: ref(false),
    init: vi.fn(),
    refresh: vi.fn(),
    check: vi.fn(),
    download: vi.fn(),
    relaunch: vi.fn(),
    dismiss: vi.fn(),
  }
})

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ChatSystemStatus layout matrix', () => {
  it('renders connection, approval, and the existing update indicator directly in wide', async () => {
    const controller = mocks.controller as {
      state: ReturnType<typeof ref<DesktopUpdateState>>
      visible: ReturnType<typeof ref<boolean>>
      latestVersion: ReturnType<typeof ref<string>>
    }
    controller.state.value = updateState({
      status: 'available',
      latestVersion: '2.0.0',
      canCheck: true,
      installMode: 'native',
    })
    controller.latestVersion.value = '2.0.0'
    controller.visible.value = true
    const { host, events } = await mountStatus({ approvalCount: 2 })

    const connection = byTestId(host, 'connection-status') as HTMLButtonElement
    expect(connection.classList.contains('connected')).toBe(true)
    expect(connection.classList.contains('topbar-state--connection')).toBe(true)
    expect(connection.dataset.state).toBe('normal')
    expect(connection.getAttribute('aria-label')).toContain('Connected')
    const approval = byTestId(host, 'chat-system-approval') as HTMLButtonElement
    expect(approval.textContent).toContain('2')
    expect(approval.classList.contains('topbar-state--approval')).toBe(true)
    expect(approval.dataset.state).toBe('danger')
    const desktopUpdate = byTestId(host, 'desktop-update-indicator') as HTMLButtonElement
    expect(desktopUpdate.classList.contains('topbar-state--update')).toBe(true)
    expect(desktopUpdate.dataset.state).toBe('info')
    expect(byTestId(host, 'chat-system-status-trigger')).toBeNull()

    connection.click()
    ;(byTestId(host, 'chat-system-approval') as HTMLButtonElement).click()
    expect(events.connection).toHaveBeenCalledTimes(1)
    expect(events.approval).toHaveBeenCalledTimes(1)
  })

  it('keeps only a compact connection control when no summary item exists', async () => {
    const { host } = await mountStatus({ layout: 'compact' })
    expect(byTestId(host, 'connection-status')).not.toBeNull()
    expect(byTestId(host, 'chat-system-status-trigger')).toBeNull()
    expect(byTestId(host, 'chat-system-approval')).toBeNull()
    expect(byTestId(host, 'chat-system-update')).toBeNull()
  })

  it('summarizes approval and update in compact without duplicating connection', async () => {
    const controller = mocks.controller as {
      state: ReturnType<typeof ref<DesktopUpdateState>>
      visible: ReturnType<typeof ref<boolean>>
      latestVersion: ReturnType<typeof ref<string>>
    }
    controller.state.value = updateState({
      status: 'downloaded',
      latestVersion: '2.0.0',
      canCheck: true,
      installMode: 'native',
    })
    controller.latestVersion.value = '2.0.0'
    controller.visible.value = true
    const { host, events } = await mountStatus({ layout: 'compact', approvalCount: 3 })

    expect(host.querySelectorAll('[data-testid="connection-status"]')).toHaveLength(1)
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    expect(trigger.classList.contains('topbar-state--system')).toBe(true)
    expect(trigger.dataset.state).toBe('danger')
    trigger.click()
    await nextTick()

    expect(byTestId(host, 'chat-system-connection')).toBeNull()
    expect(host.querySelectorAll('[data-testid="chat-system-approval"]')).toHaveLength(1)
    expect(host.querySelectorAll('[data-testid="chat-system-update"]')).toHaveLength(1)
    ;(byTestId(host, 'chat-system-update') as HTMLButtonElement).click()
    expect(events.update).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(trigger)
  })

  it('uses one tight connection-classed trigger and keeps connection reachable in its menu', async () => {
    const { host, events } = await mountStatus({ layout: 'tight' })
    expect(byTestId(host, 'connection-status')).toBeNull()
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    expect(trigger.classList.contains('conn-pill')).toBe(true)
    expect(trigger.classList.contains('connected')).toBe(true)
    expect(trigger.classList.contains('topbar-state--system')).toBe(true)
    expect(trigger.dataset.state).toBe('normal')
    trigger.click()
    await nextTick()

    expect(byTestId(host, 'chat-system-connection')).not.toBeNull()
    expect(byTestId(host, 'chat-system-approval')).toBeNull()
    expect(byTestId(host, 'chat-system-update')).toBeNull()
    ;(byTestId(host, 'chat-system-connection') as HTMLButtonElement).click()
    expect(events.connection).toHaveBeenCalledTimes(1)
  })

  it('keeps a hidden web update state out of tight and compact menus', async () => {
    const controller = mocks.controller as {
      state: ReturnType<typeof ref<DesktopUpdateState>>
      visible: ReturnType<typeof ref<boolean>>
    }
    controller.state.value = updateState({
      status: 'error',
      error: 'private transport detail',
      canCheck: false,
      installMode: 'unsupported',
    })
    controller.visible.value = false
    const { host } = await mountStatus({ layout: 'tight' })
    ;(byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement).click()
    await nextTick()

    expect(byTestId(host, 'chat-system-update')).toBeNull()
    expect(host.textContent).not.toContain('private transport detail')
    expect(host.textContent).not.toContain('raw transport detail')
  })

  it('uses warning for connecting and danger for disconnected connection controls', async () => {
    const connecting = await mountStatus({
      layout: 'compact',
      connectionState: 'connecting',
      connectionLabel: 'Connecting',
    })
    expect(byTestId(connecting.host, 'connection-status')?.dataset.state).toBe('warning')

    const disconnected = await mountStatus({
      layout: 'compact',
      connectionState: 'disconnected',
      connectionLabel: 'Disconnected',
    })
    expect(byTestId(disconnected.host, 'connection-status')?.dataset.state).toBe('danger')
  })

  it('uses update severity until a higher-priority approval takes over the summary', async () => {
    const controller = mocks.controller as {
      state: ReturnType<typeof ref<DesktopUpdateState>>
      visible: ReturnType<typeof ref<boolean>>
      latestVersion: ReturnType<typeof ref<string>>
    }
    controller.state.value = updateState({
      status: 'available',
      latestVersion: '2.0.0',
      installMode: 'native',
    })
    controller.latestVersion.value = '2.0.0'
    controller.visible.value = true
    const { host, props } = await mountStatus({ layout: 'compact' })
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    expect(trigger.dataset.state).toBe('info')

    props.approvalCount = 1
    await nextTick()
    expect(trigger.dataset.state).toBe('danger')
  })

  it('keeps an unavailable connection row focusable without emitting an action', async () => {
    const { host, events } = await mountStatus({
      layout: 'tight',
      canManageConnection: false,
    })
    ;(byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement).click()
    await nextTick()
    const connection = byTestId(host, 'chat-system-connection') as HTMLButtonElement
    expect(connection.getAttribute('aria-disabled')).toBe('true')
    connection.focus()
    connection.click()
    expect(events.connection).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(connection)
  })
})

describe('ChatSystemStatus keyboard and focus behavior', () => {
  beforeEach(() => {
    const controller = mocks.controller as {
      state: ReturnType<typeof ref<DesktopUpdateState>>
      visible: ReturnType<typeof ref<boolean>>
      latestVersion: ReturnType<typeof ref<string>>
    }
    controller.state.value = updateState({
      status: 'available',
      latestVersion: '2.0.0',
      canCheck: true,
      installMode: 'native',
    })
    controller.latestVersion.value = '2.0.0'
    controller.visible.value = true
  })

  it('supports first/last opening, wrapping arrows, Home/End, Escape, and Tab', async () => {
    const { host } = await mountStatus({ layout: 'tight', approvalCount: 1 })
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    trigger.focus()
    key(trigger, 'ArrowDown')
    await nextTick()
    expect(host.querySelector('[data-chat-topbar-popover="system-status"]')).not.toBeNull()
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-connection')

    key(document.activeElement as Element, 'ArrowUp')
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-update')
    key(document.activeElement as Element, 'Home')
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-connection')
    key(document.activeElement as Element, 'End')
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-update')
    key(document.activeElement as Element, 'ArrowDown')
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-connection')

    key(document.activeElement as Element, 'Escape')
    await nextTick()
    expect(byTestId(host, 'chat-system-status-menu')).toBeNull()
    expect(document.activeElement).toBe(trigger)

    key(trigger, 'ArrowUp')
    await nextTick()
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-update')
    key(document.activeElement as Element, 'Tab')
    await nextTick()
    expect(byTestId(host, 'chat-system-status-menu')).toBeNull()
  })

  it('closes on an outside click without restoring focus to the trigger', async () => {
    const { host } = await mountStatus({ layout: 'compact', approvalCount: 1 })
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    trigger.click()
    await nextTick()
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    outside.click()
    await nextTick()

    expect(byTestId(host, 'chat-system-status-menu')).toBeNull()
    expect(document.activeElement).toBe(outside)
  })

  it('maps a focused menu action when the layout changes', async () => {
    const { host, props } = await mountStatus({ layout: 'compact', approvalCount: 1 })
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    trigger.click()
    await nextTick()
    const approval = byTestId(host, 'chat-system-approval') as HTMLButtonElement
    approval.focus()

    props.layout = 'wide'
    await nextTick()
    await nextTick()
    expect((document.activeElement as HTMLElement).dataset.testid).toBe('chat-system-approval')
  })

  it('moves focus to compact connection when the summary trigger disappears', async () => {
    const controller = mocks.controller as { visible: ReturnType<typeof ref<boolean>> }
    const { host, props } = await mountStatus({ layout: 'compact', approvalCount: 1 })
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    trigger.focus()
    props.approvalCount = 0
    controller.visible.value = false
    await nextTick()
    await nextTick()

    expect(byTestId(host, 'chat-system-status-trigger')).toBeNull()
    expect(document.activeElement).toBe(byTestId(host, 'connection-status'))
  })

  it('can restore programmatic focus to a non-interactive compact connection status', async () => {
    const controller = mocks.controller as { visible: ReturnType<typeof ref<boolean>> }
    const { host, props } = await mountStatus({
      layout: 'compact',
      approvalCount: 1,
      canManageConnection: false,
    })
    const trigger = byTestId(host, 'chat-system-status-trigger') as HTMLButtonElement
    trigger.focus()
    props.approvalCount = 0
    controller.visible.value = false
    await nextTick()
    await nextTick()

    const connection = byTestId(host, 'connection-status')
    expect(connection?.getAttribute('tabindex')).toBe('-1')
    expect(document.activeElement).toBe(connection)
  })
})
