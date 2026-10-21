// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { WORKSPACE_FILES_KEY, type WorkspaceAppCatalogue, type WorkspaceFiles, type WorkspaceRoot, type WorkspaceRootScope } from '@/modules/workspaceFiles'
import SidebarConversations, { type SidebarSection } from './SidebarConversations.vue'

const copy = vi.hoisted(() => vi.fn(async (_text: string) => {}))
vi.mock('@/utils/browser', async importOriginal => ({
  ...await importOriginal<typeof import('@/utils/browser')>(), copyTextWithFallback: copy,
}))

const mounted: App[] = []
function rootFor(projectId: string): WorkspaceRoot {
  return { kind: 'workspace-root', name: projectId, path: `C:\\authoritative\\${projectId}`, workspaceBinding: projectId.repeat(64).slice(0, 64) }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function settle() { for (let count = 0; count < 8; count++) await nextTick() }
async function mountSidebar(options: { gateway?: Partial<GatewayAccess>; files?: Partial<WorkspaceFiles>; projects?: string[] } = {}) {
  i18n.global.locale.value = 'en'
  const gateway = reactive({
    isAuthenticated: true, availability: 'available', isAvailable: true,
    connectionHealth: 'healthy', connectionPhase: 'healthy', isResuming: false,
    canExecuteHost: true, isLocalOwner: true, deliveryIdentity: 'gateway-a', subscriptionEpoch: 1,
    ...options.gateway,
  }) as GatewayAccess
  const files = {
    root: vi.fn(async (scope: WorkspaceRootScope) => rootFor(scope.projectId!)),
    rootApps: vi.fn(async () => ({ available: true, host: 'gateway' as const, apps: [{ id: 'reveal', label: 'File manager' }] })),
    openRoot: vi.fn(async () => {}), resolve: vi.fn(async () => []), read: vi.fn(async () => new Blob()),
    ...options.files,
  } satisfies WorkspaceFiles
  const sections: SidebarSection[] = [{ family: 'chats', label: 'Tasks', rows: (options.projects || ['a']).map(id => ({
    rowKind: 'workspace', key: `workspace:${id}`, title: `Project ${id}`, effectiveAgentId: '', agentName: '',
    sessionKind: 'workspace', depth: 0, runStatus: 'idle', runLabel: '', taskAttention: 'none', updatedAt: 0,
    hasContractGaps: false, workspace: `D:\\stale-display\\${id}`, workspaceId: id, workspaceTaskCount: 0,
    workspaceAvailable: true,
  })) }]
  const host = document.createElement('div')
  document.body.appendChild(host)
  const events = { select: vi.fn(), newTask: vi.fn() }
  const app = createApp(SidebarConversations, {
    sections, error: false, loading: false, currentKey: 'agent:unrelated:webchat:other', contractDebugEnabled: false,
    searchHint: 'Ctrl+K', canManageProjects: true, onSelect: events.select, onNewProjectTask: events.newTask,
  })
  app.use(i18n).provide(WORKSPACE_FILES_KEY, files).provide(GATEWAY_ACCESS_KEY, gateway)
  app.mount(host); mounted.push(app)
  await settle()
  const openMenu = async (index = 0) => {
    host.querySelectorAll<HTMLButtonElement>('[data-testid="project-workspace-more"]')[index]!.click()
    await settle()
  }
  return { host, files, gateway, events, openMenu }
}
function action() { return document.querySelector<HTMLButtonElement>('[data-project-action="open-directory"]')! }
function closeMenu() {
  document.querySelector('.sidebar-project-menu')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
}
afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''; localStorage.clear(); copy.mockReset(); copy.mockResolvedValue()
})

describe('project directory menu actions', () => {
  it('probes only an opened project menu and opens its authoritative root without a session', async () => {
    const fixture = await mountSidebar()
    expect(fixture.files.root).not.toHaveBeenCalled()
    expect(fixture.files.rootApps).not.toHaveBeenCalled()
    await fixture.openMenu()
    expect(fixture.files.root).toHaveBeenCalledWith({ projectId: 'a' }, expect.any(AbortSignal))
    expect(fixture.files.rootApps).toHaveBeenCalledWith({ projectId: 'a' }, expect.any(AbortSignal))
    expect(action().disabled).toBe(false)
    action().click(); await settle()
    expect(fixture.files.openRoot).toHaveBeenCalledWith({ projectId: 'a' }, rootFor('a'), 'reveal', expect.any(AbortSignal))
    expect(fixture.events.select).not.toHaveBeenCalled()
    expect(fixture.events.newTask).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-project-menu')).toBeNull()
  })

  it('keeps project identities independent and ignores the previous menu response', async () => {
    const pending = deferred<WorkspaceRoot>()
    const root = vi.fn(async (scope: WorkspaceRootScope, _signal?: AbortSignal) => scope.projectId === 'a' ? pending.promise : rootFor('b'))
    const fixture = await mountSidebar({ projects: ['a', 'b'], files: { root } })
    await fixture.openMenu(0)
    const firstSignal = root.mock.calls[0]![1] as AbortSignal | undefined
    await fixture.openMenu(1)
    pending.resolve(rootFor('a')); await settle()
    expect(firstSignal?.aborted).toBe(true)
    action().click(); await settle()
    expect(fixture.files.openRoot).toHaveBeenCalledWith({ projectId: 'b' }, rootFor('b'), 'reveal', expect.any(AbortSignal))
    expect(fixture.files.rootApps).toHaveBeenCalledTimes(1)
  })

  it('copies the authoritative directory for a remote client instead of launching', async () => {
    const fixture = await mountSidebar({ files: { rootApps: vi.fn(async (): Promise<WorkspaceAppCatalogue> => ({ available: false, host: 'gateway', apps: [], reason: 'remote-client' })) } })
    await fixture.openMenu()
    expect(action().textContent).toContain(i18n.global.t('workspaces.copyDirectoryPath'))
    expect(action().title).toBe(i18n.global.t('workbench.workspaceFiles.remoteApplicationsUnavailable'))
    action().click(); await settle()
    expect(copy).toHaveBeenCalledWith(rootFor('a').path)
    expect(fixture.files.openRoot).not.toHaveBeenCalled()
  })

  it('offers copy without probing applications when host execution is unavailable', async () => {
    const fixture = await mountSidebar({ gateway: { canExecuteHost: false } })
    await fixture.openMenu()
    expect(fixture.files.rootApps).not.toHaveBeenCalled()
    expect(action().title).toBe(i18n.global.t('workbench.workspaceFiles.applicationsPermissionRequired'))
    action().click(); await settle()
    expect(copy).toHaveBeenCalledWith(rootFor('a').path)
    expect(fixture.files.openRoot).not.toHaveBeenCalled()
  })

  it('aborts pending resolution on close and discards its late failure', async () => {
    const pending = deferred<WorkspaceRoot>()
    const root = vi.fn((_scope: WorkspaceRootScope, _signal?: AbortSignal) => pending.promise)
    const fixture = await mountSidebar({ files: { root } })
    await fixture.openMenu()
    closeMenu(); await settle()
    expect(root.mock.calls[0]![1]?.aborted).toBe(true)
    pending.reject(new Error('late')); await settle()
    expect(document.querySelector('[role="alert"]')).toBeNull()
    expect(fixture.files.rootApps).not.toHaveBeenCalled()
  })

  it.each(['disconnect', 'logout', 'gateway-change'] as const)('invalidates pending application discovery on %s', async change => {
    const pending = deferred<Awaited<ReturnType<NonNullable<WorkspaceFiles['rootApps']>>>>()
    const rootApps = vi.fn((_scope: WorkspaceRootScope, _signal?: AbortSignal) => pending.promise)
    const fixture = await mountSidebar({ files: { rootApps } })
    await fixture.openMenu()
    const signal = rootApps.mock.calls[0]![1]
    Object.assign(fixture.gateway, change === 'disconnect' ? { connectionHealth: 'suspect' }
      : change === 'logout' ? { isAuthenticated: false } : { deliveryIdentity: 'gateway-b', isAvailable: false })
    await settle()
    expect(signal?.aborted).toBe(true)
    pending.resolve({ available: true, host: 'gateway', apps: [{ id: 'reveal', label: 'File manager' }] }); await settle()
    expect(action().disabled).toBe(true)
    expect(action().title).toBe(i18n.global.t('workbench.newPanel.requiresConnection'))
    action().click(); await settle()
    expect(fixture.files.openRoot).not.toHaveBeenCalled()
  })

  it('shows launch failures in the menu and refreshes authority before retry', async () => {
    const fixture = await mountSidebar({ files: { openRoot: vi.fn(async () => { throw new Error('changed') }) } })
    await fixture.openMenu()
    action().click(); await settle()
    expect(document.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('workspaces.openDirectoryFailed'))
    action().click(); await settle()
    expect(fixture.files.root).toHaveBeenCalledTimes(2)
    expect(fixture.files.openRoot).toHaveBeenCalledTimes(1)
    expect(document.querySelector('[role="alert"]')).toBeNull()
  })

  it('retains a visible copy error and permits retry', async () => {
    copy.mockRejectedValueOnce(new Error('clipboard unavailable'))
    const fixture = await mountSidebar({ gateway: { canExecuteHost: false } })
    await fixture.openMenu()
    action().click(); await settle()
    expect(document.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('workspaces.copyDirectoryFailed'))
    action().click(); await settle()
    expect(copy).toHaveBeenCalledTimes(2)
    expect(document.querySelector('.sidebar-project-menu')).toBeNull()
  })
})
