// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, watch, type App } from 'vue'
import AssistantChatPanel from './AssistantChatPanel.vue'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { SESSION_LIFECYCLE_KEY, SessionLifecycleError, type SessionLifecycle, type SidechatSession } from '@/modules/sessionLifecycle'
import { assistantChatBindingKey, closeAssistantChatOwner } from '@/modules/assistantChat'
import { assistantChatPendingOwner } from '@/modules/assistantChatPendingOwner'
import { SIDE_CONVERSATION_POLICY } from '@/modules/conversationPolicy'

// This adapter owns no chat runtime. Real routed host execution is covered by
// e2e/conversation-host-parity.spec.ts; shared behavior coverage is mapped in
// docs/side-chat-conversation-contract.md rather than duplicated with partial mocks.
const { views } = vi.hoisted(() => ({ views: [] as {
  props: Record<string, unknown> & { prepareSession: () => Promise<string> }
  unmounted: boolean
}[] }))
vi.mock('@/views/ChatView.vue', async () => {
  const { defineComponent, h, onUnmounted } = await import('vue')
  return { default: defineComponent({
    name: 'SharedChatViewProbe',
    props: { embedded: Boolean, ...Object.fromEntries(['sessionKeyOverride', 'policy', 'workbenchOwnerSessionKey', 'pendingInputWal',
      'pendingQueuePolicy', 'draftStorage', 'attachmentDraftStore', 'draftIdentity', 'inheritedRunMode',
      'expectedWorkspaceId', 'ownerBlocked', 'prepareSession', 'surfaceVisible'].map(name => [name, {}])) },
    setup(props) {
      const view = { props: props as unknown as typeof views[number]['props'], unmounted: false }
      views.push(view)
      onUnmounted(() => { view.unmounted = true })
      return () => h('div', { 'data-testid': 'shared-chat-view' })
    },
  }) }
})

const apps: App[] = []
const cleanups: (() => Promise<void>)[] = []
const flush = async () => { for (let index = 0; index < 5; index++) await nextTick() }
const currentView = () => {
  const active = views.filter(view => !view.unmounted)
  return active[active.length - 1]!
}
afterEach(async () => {
  apps.splice(0).forEach(app => app.unmount())
  for (const cleanup of cleanups.splice(0)) await cleanup()
  views.splice(0)
  document.body.innerHTML = ''
  localStorage.clear()
})

async function mountPanel({ provideLifecycle = true } = {}) {
  const state = reactive({ parentKey: `parent-${crypto.randomUUID()}`, workspaceId: 'workspace-a', ownerId: 'side-tab', visible: true })
  const identity: string = crypto.randomUUID()
  const gateway = reactive({ deliveryIdentity: identity, availability: 'available', isAvailable: true,
    isResuming: false, connectionHealth: 'healthy', connectionPhase: 'healthy', subscriptionEpoch: 1 })
  let childSequence = 0
  const createSidechat = vi.fn(async ({ parentKey }: { parentKey: string; clientRequestId?: string }): Promise<SidechatSession> => ({
    key: `child-${++childSequence}`, parentKey, sessionId: `child-id-${childSequence}`,
    inherited: { model: 'parent-model', provider: 'parent-provider', reasoningLevel: 'high', modelRoutingMode: 'direct',
      modelRoutingRevision: 3, runMode: 'safe' },
  }))
  const closeSidechat = vi.fn(async ({ key }: { key: string }) => ({ key, closed: true as const }))
  const lifecycle: SessionLifecycle = { create: vi.fn(), fork: vi.fn(), rename: vi.fn(), remove: vi.fn(), createSidechat, closeSidechat }
  const scopes = new Map<string, Parameters<typeof closeAssistantChatOwner>[0]>()
  const el = document.createElement('div'); document.body.append(el)
  const app = createApp({
    setup() {
      watch(() => [gateway.deliveryIdentity, state.parentKey, state.ownerId] as const, ([identity, parentKey, ownerId]) => {
        if (identity) scopes.set(JSON.stringify([identity, parentKey, ownerId]), { identity, parentKey, ownerId, lifecycle })
      }, { immediate: true, flush: 'sync' })
      return () => h(AssistantChatPanel, { parentSessionKey: state.parentKey, workspaceId: state.workspaceId, ownerId: state.ownerId, visible: state.visible })
    },
  })
  app.provide(GATEWAY_ACCESS_KEY, gateway as unknown as GatewayAccess)
  if (provideLifecycle) app.provide(SESSION_LIFECYCLE_KEY, lifecycle)
  app.mount(el); apps.push(app)
  cleanups.push(async () => {
    closeSidechat.mockImplementation(async ({ key }) => ({ key, closed: true }))
    for (const scope of scopes.values()) await closeAssistantChatOwner(scope).catch(() => {})
  })
  await flush()
  return { app, el, state, gateway, lifecycle, createSidechat, closeSidechat,
    scope: () => ({ identity: gateway.deliveryIdentity, parentKey: state.parentKey, ownerId: state.ownerId, lifecycle }) }
}

describe('AssistantChatPanel owner adapter for the shared ChatView', () => {
  it('passes policy, authority and owner memory to exactly one shared chat without creating a child', async () => {
    const f = await mountPanel()
    const props = currentView().props
    const binding = assistantChatBindingKey(f.gateway.deliveryIdentity, f.state.parentKey, f.state.ownerId)
    const memory = assistantChatPendingOwner(binding)
    expect(f.el.querySelectorAll('[data-testid="shared-chat-view"]')).toHaveLength(1)
    expect(props).toMatchObject({ embedded: true, sessionKeyOverride: '', policy: SIDE_CONVERSATION_POLICY,
      workbenchOwnerSessionKey: f.state.parentKey, draftIdentity: binding, expectedWorkspaceId: 'workspace-a', ownerBlocked: false })
    expect(props.pendingInputWal).toBe(memory.wal)
    expect(props.pendingQueuePolicy).toBe(memory.policy)
    expect(props.draftStorage).toBe(memory.draftStorage)
    expect(props.attachmentDraftStore).toBe(memory.attachmentDraftStore)
    expect(f.createSidechat).not.toHaveBeenCalled()
    expect(f.lifecycle.create).not.toHaveBeenCalled()
    expect(localStorage.length).toBe(0)
    expect(f.el.querySelector('.assistant-chat-panel__footer')).toBeNull()
  })

  it('materializes only through the shared send prepare hook without replacing the shared view', async () => {
    const f = await mountPanel()
    const view = currentView()
    expect(await view.props.prepareSession()).toBe('child-1')
    await flush()
    expect(currentView()).toBe(view)
    expect(view.props.sessionKeyOverride).toBe('child-1')
    expect(view.props.inheritedRunMode).toBe('safe')
    expect(f.createSidechat).toHaveBeenCalledExactlyOnceWith({ parentKey: f.state.parentKey, clientRequestId: f.state.ownerId })
    expect(await view.props.prepareSession()).toBe('child-1')
    expect(f.createSidechat).toHaveBeenCalledOnce()
    expect(f.lifecycle.fork).not.toHaveBeenCalled()
  })

  it('retains the same child, view and stores through reconnect while gating preparation offline', async () => {
    const f = await mountPanel()
    const view = currentView()
    const identity = f.gateway.deliveryIdentity
    const storage = view.props.draftStorage
    await view.props.prepareSession(); await flush()
    f.gateway.deliveryIdentity = ''; f.gateway.isAvailable = false; await flush()
    expect(currentView()).toBe(view)
    expect(view.props.sessionKeyOverride).toBe('child-1')
    expect(await view.props.prepareSession()).toBe('')
    f.gateway.deliveryIdentity = identity; f.gateway.subscriptionEpoch++; f.gateway.isAvailable = true
    await flush()
    expect(currentView()).toBe(view)
    expect(view.props.draftStorage).toBe(storage)
    expect(await view.props.prepareSession()).toBe('child-1')
    expect(f.createSidechat).toHaveBeenCalledOnce()
  })

  it('hides presentation without replacing the child, reader or pending owner', async () => {
    const f = await mountPanel()
    const view = currentView()
    await view.props.prepareSession(); await flush()
    const wal = view.props.pendingInputWal
    f.state.visible = false; await flush()
    expect(view.props.surfaceVisible).toBe(false)
    expect(currentView()).toBe(view)
    expect(view.props.pendingInputWal).toBe(wal)
    expect(view.props.sessionKeyOverride).toBe('child-1')
    expect(f.closeSidechat).not.toHaveBeenCalled()
    f.state.visible = true; await flush()
    expect(view.props.surfaceVisible).toBe(true)
    expect(currentView()).toBe(view)
    expect(f.createSidechat).toHaveBeenCalledOnce()
    expect(f.closeSidechat).not.toHaveBeenCalled()
  })

  it('remounts for another parent and fences an earlier asynchronous preparation', async () => {
    const f = await mountPanel()
    const first = currentView()
    let complete!: (child: SidechatSession) => void
    const originalParent = f.state.parentKey
    f.createSidechat.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    const pending = first.props.prepareSession()
    f.state.parentKey = 'next-parent'; await flush()
    const second = currentView()
    expect(first.unmounted).toBe(true)
    expect(second).not.toBe(first)
    expect(second.props.sessionKeyOverride).toBe('')
    expect(second.props.workbenchOwnerSessionKey).toBe('next-parent')
    complete({ key: 'old-child', parentKey: originalParent, sessionId: 'old-child-id', inherited: {
      model: null, provider: null, reasoningLevel: null, modelRoutingMode: null, modelRoutingRevision: 0, runMode: null,
    } })
    expect(await pending).toBe('')
    await flush()
    expect(second.props.sessionKeyOverride).toBe('')
    await second.props.prepareSession()
    expect(f.createSidechat).toHaveBeenLastCalledWith({ parentKey: 'next-parent', clientRequestId: f.state.ownerId })
  })

  it('separates authority owner storage and remounts the shared reader on workspace changes', async () => {
    const f = await mountPanel()
    const first = currentView()
    const originalStorage = first.props.draftStorage
    const originalBinding = first.props.draftIdentity
    f.gateway.deliveryIdentity = 'different-authority'; await flush()
    const second = currentView()
    expect(second).not.toBe(first)
    expect(second.props.draftIdentity).not.toBe(originalBinding)
    expect(second.props.draftStorage).not.toBe(originalStorage)
    f.state.workspaceId = 'workspace-b'; await flush()
    expect(currentView()).not.toBe(second)
    expect(currentView().props.expectedWorkspaceId).toBe('workspace-b')
    f.state.workspaceId = ''; await flush()
    expect(currentView().props.expectedWorkspaceId).toBeNull()
    expect(f.createSidechat).not.toHaveBeenCalled()
  })

  it('reports lifecycle failure and blocks an invalid owner without an extra execution status UI', async () => {
    const f = await mountPanel()
    f.createSidechat.mockRejectedValueOnce(new SessionLifecycleError('forbidden', 'Parent context changed'))
    expect(await currentView().props.prepareSession()).toBe('')
    await flush()
    expect(currentView().props.ownerBlocked).toBe(true)
    expect(f.el.querySelector('[role="alert"]')?.textContent).toBe('Parent context changed')
    expect(f.el.querySelector('.assistant-chat-panel__status')).toBeNull()
  })

  it('forwards close-in-progress and failed-close recovery into the shared send gate', async () => {
    const f = await mountPanel()
    await currentView().props.prepareSession(); await flush()
    let rejectClose!: (error: Error) => void
    f.closeSidechat.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectClose = reject }))
    const closing = closeAssistantChatOwner(f.scope())
    await flush()
    expect(currentView().props.ownerBlocked).toBe(true)
    expect(await currentView().props.prepareSession()).toBe('')
    rejectClose(new Error('Retry close'))
    await expect(closing).rejects.toThrow('Retry close')
    await flush()
    expect(currentView().props.ownerBlocked).toBe(false)
    expect(currentView().props.sessionKeyOverride).toBe('child-1')
  })

  it('unmounts only the reader without implicitly closing or deleting the child', async () => {
    const f = await mountPanel()
    await currentView().props.prepareSession(); await flush()
    f.app.unmount(); apps.splice(apps.indexOf(f.app), 1)
    expect(f.closeSidechat).not.toHaveBeenCalled()
    expect(f.lifecycle.remove).not.toHaveBeenCalled()
    expect(views.every(view => view.unmounted)).toBe(true)
  })

  it('does not prepare without lifecycle capability or a healthy Gateway', async () => {
    const missing = await mountPanel({ provideLifecycle: false })
    expect(await currentView().props.prepareSession()).toBe('')
    expect(missing.createSidechat).not.toHaveBeenCalled()
    const f = await mountPanel()
    f.gateway.isResuming = true
    expect(await currentView().props.prepareSession()).toBe('')
    f.gateway.isResuming = false; f.gateway.connectionHealth = 'degraded'
    expect(await currentView().props.prepareSession()).toBe('')
    expect(f.createSidechat).not.toHaveBeenCalled()
  })
})
