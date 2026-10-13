// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { setActivePinia } from 'pinia'
import i18n from '@/i18n'
import { WORKSPACE_FILES_KEY, type WorkspaceFile, type WorkspaceFiles } from '@/modules/workspaceFiles'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import TextPart from './TextPart.vue'

const mocks = vi.hoisted(() => ({ platform: { id: 'web', files: {} as Record<string, unknown>,
  gateway: { getConnection: vi.fn(), getStatus: vi.fn() }, capabilities: { hasNativeWorkbenchSurfaces: false } }, toast: vi.fn() }))
vi.mock('@/platform', () => ({ usePlatform: () => mocks.platform }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: mocks.toast }) }))

const apps: App[] = []
const file: WorkspaceFile = { requestedPath: '图.svg', path: '图.svg', name: '图.svg', mime: 'image/svg+xml', size: 64,
  kind: 'text', workspaceBinding: 'binding-A' }
function pending<T>() { let finish!: (value: T) => void; return { promise: new Promise<T>(resolve => { finish = resolve }), finish: (value: T) => finish(value) } }
async function mount(access: WorkspaceFiles, preferWorkspaceWorkbench = false) {
  const state = reactive({ sessionKey: 'A', text: '| Result |\n| --- |\n| `图.svg` |' })
  const gateway = reactive({ isLocalOwner: true, isAvailable: true, deliveryIdentity: 'owner-A', subscriptionEpoch: 1 })
  const renderer = useChatTextRendering()
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp({ render: () => h(TextPart, { sessionKey: state.sessionKey, preferWorkspaceWorkbench,
    part: { type: 'text', key: 'answer', rawText: state.text, html: renderer.renderMarkdown(state.text) } }) })
  app.use(i18n)
  app.provide(WORKSPACE_FILES_KEY, access)
  app.provide(GATEWAY_ACCESS_KEY, gateway as unknown as GatewayAccess)
  app.mount(root)
  apps.push(app)
  await nextTick()
  return { state, gateway, root }
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.platform.id = 'web'
  mocks.platform.files = {}
  mocks.platform.gateway.getConnection.mockResolvedValue({ status: 'ready', instanceId: 'owner-A' })
  mocks.platform.gateway.getStatus.mockResolvedValue({ owned: true, status: 'ready' })
})
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; vi.restoreAllMocks() })

describe('TextPart workspace files', () => {
  it('uses the preview fallback when Workbench is preferred but its store is unavailable', async () => {
    setActivePinia(undefined)
    const { root } = await mount({ resolve: vi.fn().mockResolvedValue([file]),
      read: vi.fn().mockResolvedValue(new Blob(['safe source'])) }, true)
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-link')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('.workspace-file-link')!.click()
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"] pre')?.textContent).toBe('safe source'))
  })
  it.each(['open', 'reveal'])('passes only bound relative workspace identity to desktop %s', async action => {
    mocks.platform.id = 'desktop'
    const nativeAction = vi.fn().mockResolvedValue({ ok: true })
    mocks.platform.files.workspaceFileAction = nativeAction
    const { root } = await mount({ resolve: vi.fn().mockResolvedValue([{ ...file, nativeActions: true }]), read: vi.fn() })
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-action-trigger')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('.workspace-file-action-trigger')!.click()
    await nextTick()
    const label = i18n.global.t(action === 'open' ? 'resourceActions.openSource' : 'resourceActions.reveal')
    await vi.waitFor(() => expect(document.querySelector('[role="menu"]')?.textContent).toContain(label))
    Array.from(document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
      .find(button => button.textContent?.trim() === label)!.click()
    await vi.waitFor(() => expect(nativeAction).toHaveBeenCalledWith({ gatewayInstanceId: 'owner-A', sessionKey: 'A',
      workspaceBinding: 'binding-A', path: '图.svg', action }))
  })

  it.each(['web', 'old-gateway', 'remote-desktop'])('hides native actions in %s', async mode => {
    mocks.platform.id = mode === 'web' ? 'web' : 'desktop'
    mocks.platform.files.workspaceFileAction = vi.fn()
    if (mode === 'remote-desktop') mocks.platform.gateway.getStatus.mockResolvedValue({ owned: false, status: 'ready' })
    const { root } = await mount({ resolve: vi.fn().mockResolvedValue([{ ...file, nativeActions: mode !== 'old-gateway' }]), read: vi.fn() })
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-action-trigger')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('.workspace-file-action-trigger')!.click()
    await nextTick()
    await nextTick()
    await nextTick()
    expect(document.querySelector('[role="menu"]')?.textContent).not.toContain(i18n.global.t('resourceActions.openSource'))
    expect(document.querySelector('[role="menu"]')?.textContent).not.toContain(i18n.global.t('resourceActions.reveal'))
  })

  it('reports native failures without exposing filesystem error paths', async () => {
    mocks.platform.id = 'desktop'
    mocks.platform.files.workspaceFileAction = vi.fn().mockRejectedValue(new Error('ENOENT /private/host/path'))
    const { root } = await mount({ resolve: vi.fn().mockResolvedValue([{ ...file, nativeActions: true }]), read: vi.fn() })
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-action-trigger')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('.workspace-file-action-trigger')!.click()
    await nextTick()
    await vi.waitFor(() => expect(document.querySelector('[role="menu"]')?.textContent).toContain(i18n.global.t('resourceActions.openSource')))
    Array.from(document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
      .find(button => button.textContent?.trim() === i18n.global.t('resourceActions.openSource'))!.click()
    await vi.waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('resourceActions.failed'), { tone: 'danger' }))
  })

  it('opens resolved SVG as escaped text with a download action and never embeds SVG', async () => {
    const access = { resolve: vi.fn().mockResolvedValue([file]), read: vi.fn().mockResolvedValue(new Blob(['<svg onload="alert(1)">safe</svg>'])) }
    const { root, state } = await mount(access)
    await vi.waitFor(() => expect(root.querySelector('button.workspace-file-link')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('button.workspace-file-link')!.click()
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"] pre')?.textContent).toContain('<svg onload'))
    expect(document.querySelector('[role="dialog"] svg[onload]')).toBeNull()
    expect(document.querySelector('[role="dialog"] img')).toBeNull()
    expect(access.read).toHaveBeenCalledWith('A', file, expect.any(AbortSignal))
    state.text = 'Another sentence.\n\n`图.svg`'
    await nextTick()
    expect(access.resolve).toHaveBeenCalledOnce()
  })

  it('keeps the inline link appearance and shares the ellipsis/context menu actions', async () => {
    const access = { resolve: vi.fn().mockResolvedValue([file]), read: vi.fn().mockResolvedValue(new Blob(['safe'])) }
    const { root } = await mount(access)
    await vi.waitFor(() => expect(root.querySelector('button.workspace-file-link')).not.toBeNull())
    expect(root.querySelector<HTMLButtonElement>('button.workspace-file-link')?.textContent).toBe('图.svg')
    const trigger = root.querySelector<HTMLButtonElement>('.workspace-file-action-trigger')
    expect(trigger).not.toBeNull()
    trigger!.click()
    await nextTick()
    const menu = document.querySelector('[role="menu"]')
    expect(menu?.textContent).toContain('Copy relative path')
    expect(menu?.textContent).toContain('Download')
    ;(document.activeElement as HTMLElement)?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBeNull()

    root.querySelector<HTMLButtonElement>('button.workspace-file-link')!.dispatchEvent(
      new MouseEvent('contextmenu', { bubbles: true, clientX: 24, clientY: 24 }),
    )
    await nextTick()
    expect(document.querySelector('[role="menu"]')).not.toBeNull()
  })

  it('closes a file actions menu when the workspace scope changes', async () => {
    const access = { resolve: vi.fn().mockResolvedValue([file]), read: vi.fn().mockResolvedValue(new Blob(['safe'])) }
    const { root, state } = await mount(access)
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-action-trigger')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('.workspace-file-action-trigger')!.click()
    await nextTick()
    expect(document.querySelector('[role="menu"]')).not.toBeNull()
    state.sessionKey = 'B'
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  it.each(['ContextMenu', 'F10'])('opens the same actions with %s and restores focus on Escape', async key => {
    const { root } = await mount({ resolve: vi.fn().mockResolvedValue([file]), read: vi.fn() })
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-link')).not.toBeNull())
    const link = root.querySelector<HTMLButtonElement>('.workspace-file-link')!
    link.focus()
    link.dispatchEvent(new KeyboardEvent('keydown', { key, shiftKey: key === 'F10', bubbles: true, cancelable: true }))
    await nextTick()
    const menu = document.querySelector('[role="menu"]')!
    expect(menu.textContent).toContain('Copy relative path')
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(link)
  })

  it('cancels a pending copy when switching sessions before file bytes arrive', async () => {
    const old = pending<Blob>()
    const read = vi.fn().mockReturnValue(old.promise)
    const clipboard = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    const { root, state } = await mount({ resolve: vi.fn().mockResolvedValue([file]), read })
    await vi.waitFor(() => expect(root.querySelector('.workspace-file-action-trigger')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('.workspace-file-action-trigger')!.click()
    await nextTick()
    Array.from(document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
      .find(button => button.textContent?.includes('Copy file contents'))!.click()
    state.sessionKey = 'B'
    await nextTick()
    old.finish(new Blob(['obsolete content']))
    await nextTick(); await nextTick()
    expect(read.mock.calls[0][2].aborted).toBe(true)
    expect(clipboard).not.toHaveBeenCalled()
  })

  it('does not make missing paths clickable when resolution fails', async () => {
    const { root } = await mount({ resolve: vi.fn().mockRejectedValue(new Error('404')), read: vi.fn() })
    await nextTick()
    expect(root.querySelector('button.workspace-file-link')).toBeNull()
    expect(root.querySelector('code')?.textContent).toBe('图.svg')
  })

  it('resolves a newly created file after an earlier mention was not yet readable', async () => {
    const resolve = vi.fn().mockResolvedValueOnce([]).mockResolvedValue([file])
    const { root, state } = await mount({ resolve, read: vi.fn() })
    await vi.waitFor(() => expect(resolve).toHaveBeenCalledOnce())
    await nextTick()
    expect(root.querySelector('button.workspace-file-link')).toBeNull()
    state.text = 'Created the file: `图.svg`'
    await vi.waitFor(() => expect(root.querySelector('button.workspace-file-link')).not.toBeNull())
    expect(resolve).toHaveBeenCalledTimes(2)
  })

  it.each(['session', 'identity', 'epoch', 'owner'] as const)('discards stale resolution after %s changes', async kind => {
    const old = pending<WorkspaceFile[]>()
    const access = { resolve: vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue([]), read: vi.fn() }
    const { root, state, gateway } = await mount(access)
    if (kind === 'session') { state.sessionKey = 'B'; await nextTick(); state.sessionKey = 'A' }
    if (kind === 'identity') gateway.deliveryIdentity = 'owner-B'
    if (kind === 'epoch') gateway.subscriptionEpoch++
    if (kind === 'owner') gateway.isLocalOwner = false
    await nextTick()
    old.finish([file])
    await nextTick(); await nextTick()
    expect(root.querySelector('button.workspace-file-link')).toBeNull()
  })

  it('closes and cancels an in-flight read on session change', async () => {
    const old = pending<Blob>()
    const access = { resolve: vi.fn().mockResolvedValue([file]), read: vi.fn().mockReturnValue(old.promise) }
    const { root, state } = await mount(access)
    await vi.waitFor(() => expect(root.querySelector('button.workspace-file-link')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('button.workspace-file-link')!.click()
    await nextTick()
    state.sessionKey = 'B'
    await nextTick()
    old.finish(new Blob(['SECRET A']))
    await nextTick(); await nextTick()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.textContent).not.toContain('SECRET A')
    expect(access.read.mock.calls[0][2].aborted).toBe(true)
  })

  it('revokes PNG preview object URLs on close and session change', async () => {
    const createUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:png-preview')
    const revokeUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const png = { ...file, requestedPath: '图.png', path: '图.png', name: '图.png', mime: 'image/png', kind: 'image' as const }
    const access = { resolve: vi.fn().mockImplementation(async (_key: string, paths: string[]) => paths.includes(png.requestedPath) ? [png] : []),
      read: vi.fn().mockResolvedValue(new Blob(['image'], { type: 'image/png' })) }
    const { root, state } = await mount(access)
    state.text = '`图.png`'
    await vi.waitFor(() => expect(root.querySelector('button.workspace-file-link')).not.toBeNull())
    root.querySelector<HTMLButtonElement>('button.workspace-file-link')!.click()
    await vi.waitFor(() => expect(document.querySelector('[role="dialog"] img')?.getAttribute('src')).toBe('blob:png-preview'))
    expect(createUrl).toHaveBeenCalledOnce()
    state.sessionKey = 'B'
    await nextTick()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(revokeUrl).toHaveBeenCalledExactlyOnceWith('blob:png-preview')
  })
})
