// @vitest-environment happy-dom
import { computed, createApp, h, nextTick, reactive, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import type { SystemHeaderLayout } from '@/utils/headerLayout'
import ChatSystemStatus from './ChatSystemStatus.vue'
import { CONNECTION_NOTICE_DELAY_MS, useDelayedNotice } from '@/composables/useDelayedNotice'

const mounted: App[] = []
type StatusProps = {
  layout: SystemHeaderLayout
  connectionState: 'connected' | 'connecting' | 'disconnected'
  connectionLabel: string
  approvalCount: number
  canManageConnection: boolean
  connectionNoticeVisible?: boolean
}
async function mountStatus(overrides: Partial<StatusProps> = {}, delayed = false) {
  const props = reactive<StatusProps>({ layout: 'wide', connectionState: 'connected',
    connectionLabel: 'Connected', approvalCount: 0, canManageConnection: true, ...overrides })
  const events = { connection: vi.fn(), approval: vi.fn() }
  const blocked = ref(false)
  const host = document.createElement('header')
  document.body.appendChild(host)
  const app = createApp({ setup: () => {
    const visible = useDelayedNotice(
      computed(() => props.connectionState !== 'connected'), blocked, undefined, CONNECTION_NOTICE_DELAY_MS,
    )
    return () => h('div', [
    h('button', { 'data-testid': 'chat-session-actions-trigger' }, 'Session actions'),
    h(ChatSystemStatus, { ...props,
      ...(delayed ? { connectionNoticeVisible: visible.value } : {}),
      onOpenConnection: events.connection, onOpenApproval: events.approval }),
  ]) } })
  mounted.push(app)
  app.use(i18n).mount(host)
  await nextTick()
  return { host, props, events, blocked }
}
function byId(host: HTMLElement, id: string) {
  return host.querySelector<HTMLButtonElement>(`[data-testid="${id}"]`)
}
beforeEach(() => { i18n.global.locale.value = 'en' })
afterEach(() => { mounted.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; vi.useRealTimers() })

describe('direct system status controls', () => {
  it('adds no icon, copy or layout container during a short automatic reconnect', async () => {
    vi.useFakeTimers()
    const { host, props } = await mountStatus({}, true)
    props.connectionState = 'disconnected'
    props.connectionLabel = 'Disconnected'
    await nextTick()
    await vi.advanceTimersByTimeAsync(500)
    expect(byId(host, 'chat-system-status')).toBeNull()
    props.connectionState = 'connecting'
    props.connectionLabel = 'Reconnecting'
    await nextTick()
    await vi.advanceTimersByTimeAsync(3_000)
    expect(byId(host, 'connection-status')).toBeNull()
    props.connectionState = 'connected'
    await nextTick()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(byId(host, 'chat-system-status')).toBeNull()
  })
  it('keeps approvals available during the grace period and shows a lasting outage once', async () => {
    vi.useFakeTimers()
    const { host, props } = await mountStatus({ connectionState: 'disconnected', approvalCount: 1 }, true)
    expect(byId(host, 'connection-status')).toBeNull()
    expect(byId(host, 'chat-system-approval')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(1_000)
    props.connectionState = 'connecting'
    props.connectionLabel = 'Reconnecting'
    await nextTick()
    await vi.advanceTimersByTimeAsync(3_999)
    expect(byId(host, 'connection-status')).toBeNull()
    await vi.advanceTimersByTimeAsync(1)
    expect(byId(host, 'connection-status')?.textContent).toContain('Reconnecting')
    expect(host.querySelectorAll('[data-testid="connection-status"]')).toHaveLength(1)
  })
  it('does not delay a rejected connection that requires user action', async () => {
    vi.useFakeTimers()
    const { host, blocked, events } = await mountStatus({ connectionState: 'disconnected' }, true)
    blocked.value = true
    await nextTick()
    byId(host, 'connection-status')!.click()
    expect(events.connection).toHaveBeenCalledOnce()
  })
  it.each(['wide', 'compact', 'tight'] as const)('has no healthy connection chrome or generic status menu at %s', async layout => {
    const { host } = await mountStatus({ layout })
    expect(byId(host, 'chat-system-status')).toBeNull()
    expect(host.querySelector('[aria-haspopup="menu"]')).toBeNull()
  })
  it.each(['wide', 'compact', 'tight'] as const)('keeps disconnect and pending approval separately actionable at %s', async layout => {
    const { host, events } = await mountStatus({ layout, connectionState: 'disconnected', connectionLabel: 'Disconnected', approvalCount: 3 })
    const connection = byId(host, 'connection-status')!
    const approval = byId(host, 'chat-system-approval')!
    expect(connection.dataset.state).toBe('danger')
    expect(connection.getAttribute('aria-label')).toContain('Disconnected')
    expect(approval.dataset.state).toBe('warning')
    expect(approval.getAttribute('aria-label')).toContain('3')
    connection.click(); approval.click()
    expect(events.connection).toHaveBeenCalledTimes(1)
    expect(events.approval).toHaveBeenCalledTimes(1)
    expect(byId(host, 'chat-system-status-trigger')).toBeNull()
    expect(byId(host, 'desktop-update-indicator')).toBeNull()
  })
  it('keeps reconnecting distinct and removes it when connection recovers', async () => {
    const { host, props } = await mountStatus({ connectionState: 'connecting', connectionLabel: 'Connecting' })
    expect(byId(host, 'connection-status')?.dataset.state).toBe('warning')
    props.connectionState = 'connected'
    await nextTick()
    expect(byId(host, 'connection-status')).toBeNull()
  })
  it('does not present an action when connection settings are unavailable', async () => {
    const { host, events } = await mountStatus({ connectionState: 'disconnected', canManageConnection: false })
    const status = byId(host, 'connection-status')!
    expect(status.tagName).toBe('SPAN')
    status.click()
    expect(events.connection).not.toHaveBeenCalled()
  })
  it.each([0, -2, NaN, Infinity])('ignores invalid or empty approval count %s', async approvalCount => {
    const { host } = await mountStatus({ approvalCount })
    expect(byId(host, 'chat-system-approval')).toBeNull()
  })
  it('bounds visible counts while retaining the complete accessible count', async () => {
    const { host } = await mountStatus({ layout: 'tight', approvalCount: 105 })
    expect(byId(host, 'chat-system-approval')?.textContent).toContain('99+')
    expect(byId(host, 'chat-system-approval')?.getAttribute('aria-label')).toContain('105')
  })
  it('keeps the same focused action when measured layout changes', async () => {
    const { host, props } = await mountStatus({ approvalCount: 1 })
    const approval = byId(host, 'chat-system-approval')!
    approval.focus()
    props.layout = 'tight'
    await nextTick()
    expect(document.activeElement).toBe(approval)
    props.layout = 'wide'
    await nextTick()
    expect(document.activeElement).toBe(approval)
  })
  it('returns focus to a stable header action when the focused status resolves', async () => {
    const { host, props } = await mountStatus({ connectionState: 'disconnected' })
    byId(host, 'connection-status')!.focus()
    props.connectionState = 'connected'
    await nextTick(); await nextTick()
    expect(document.activeElement).toBe(byId(host, 'chat-session-actions-trigger'))
  })
  it('returns focus to the remaining approval if connection recovers', async () => {
    const { host, props } = await mountStatus({ connectionState: 'disconnected', approvalCount: 1 })
    byId(host, 'connection-status')!.focus()
    props.connectionState = 'connected'
    await nextTick(); await nextTick()
    expect(document.activeElement).toBe(byId(host, 'chat-system-approval'))
  })
  it('does not steal focus from a composer when status resolves', async () => {
    const { props } = await mountStatus({ connectionState: 'disconnected' })
    const composer = document.createElement('textarea')
    document.body.appendChild(composer); composer.focus()
    props.connectionState = 'connected'
    await nextTick(); await nextTick()
    expect(document.activeElement).toBe(composer)
  })
})
