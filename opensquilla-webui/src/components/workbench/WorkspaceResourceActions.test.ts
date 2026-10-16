// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import { WORKSPACE_FILES_KEY, WorkspaceFilesChangedError, type WorkspaceFile, type WorkspaceFiles } from '@/modules/workspaceFiles'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import WorkspaceResourceActions from './WorkspaceResourceActions.vue'
import vscodeIcon from '@/assets/workspace-apps/vscode.svg'
const browser = vi.hoisted(() => ({ download: vi.fn(), copy: vi.fn(async () => {}) }))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: browser.copy, downloadBlob: browser.download }))
const disposers: Array<() => void> = []
afterEach(() => { disposers.splice(0).forEach(fn => fn()); vi.clearAllMocks() })
const flush = async () => { await new Promise(resolve => setTimeout(resolve, 0)); await nextTick() }
type Catalog = Awaited<ReturnType<NonNullable<WorkspaceFiles['apps']>>>
function catalog(available = true): Catalog {
  return { available, host: 'gateway', apps: available ? [
    { id: 'default', label: 'Default app' }, { id: 'reveal', label: 'Show in folder' }, { id: 'vscode', label: 'Visual Studio Code' },
  ] : [], reason: available ? undefined : 'remote-client' }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
async function mount(options: { available?: boolean; catalog?: () => Promise<Catalog>; file?: WorkspaceFile | null; identity?: string } = {}) {
  const file: WorkspaceFile = { requestedPath: 'notes.md', path: 'notes.md', name: 'notes.md', mime: 'text/markdown', kind: 'text', size: 20, workspaceBinding: 'root-a', revision: 'version-a' }
  const files = { resolve: vi.fn(), read: vi.fn(async () => new Blob(['notes'])),
    apps: vi.fn<NonNullable<WorkspaceFiles['apps']>>(options.catalog ?? (async () => catalog(options.available !== false))), openWith: vi.fn(async () => {}) } satisfies WorkspaceFiles
  const gateway = reactive({ deliveryIdentity: options.identity || crypto.randomUUID(), subscriptionEpoch: 1,
    availability: 'available', isAvailable: true, connectionHealth: 'healthy', isResuming: false, canExecuteHost: true, isLocalOwner: true })
  const el = document.createElement('div'); document.body.append(el)
  const props = reactive({ sessionKey: 'session-a', file: 'file' in options ? options.file! : file as WorkspaceFile | null })
  const app = createApp({ render: () => h(WorkspaceResourceActions, props) })
  app.provide(WORKSPACE_FILES_KEY, files)
  app.provide(GATEWAY_ACCESS_KEY, gateway as unknown as GatewayAccess)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(el)
  disposers.push(() => { app.unmount(); el.remove() })
  await flush()
  const primary = () => el.querySelector<HTMLButtonElement>('[data-testid="workspace-open-primary"]')!
  const openApps = async () => { el.querySelector<HTMLButtonElement>('[data-testid="workspace-open-app-menu"]')!.click(); await flush() }
  const openMore = async () => { el.querySelector<HTMLButtonElement>('[data-testid="workspace-file-more"]')!.click(); await flush() }
  return { el, props, files, gateway, primary, openApps, openMore }
}
const menuButton = (label: string) => [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"], [role="menuitemradio"]')].find(button => button.textContent?.trim() === label)!
const appButton = (id: string) => document.querySelector<HTMLButtonElement>(`[data-app-id="${id}"]`)!
function key(key: string) { document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })) }

describe('workspace resource actions', () => {
  it('offers a separate default file-manager action without first opening a menu', async () => {
    const f = await mount()
    expect(f.primary().textContent).toContain('File manager')
    expect(f.primary().title).toContain('Gateway computer')
    expect(document.querySelector('[role="menu"]')).toBeNull()
    f.primary().click(); await flush()
    expect(f.files.openWith).toHaveBeenCalledExactlyOnceWith('session-a', f.props.file, 'reveal', expect.any(AbortSignal))
    expect(f.files.apps).toHaveBeenCalledOnce()
  })

  it('marks the current application, selects and opens another app, then reuses it from the main button', async () => {
    const f = await mount()
    await f.openApps()
    expect(document.body.textContent).toContain('Open on the Gateway computer')
    expect(appButton('reveal').getAttribute('aria-checked')).toBe('true')
    expect(appButton('vscode').getAttribute('aria-checked')).toBe('false')
    expect(menuButton('Download')).toBeUndefined()
    appButton('vscode').click(); await flush()
    expect(f.files.openWith).toHaveBeenLastCalledWith('session-a', f.props.file, 'vscode', expect.any(AbortSignal))
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(f.primary().textContent).toContain('Visual Studio Code')
    f.primary().click(); await flush()
    expect(f.files.openWith).toHaveBeenCalledTimes(2)
    await f.openApps()
    expect(appButton('vscode').getAttribute('aria-checked')).toBe('true')
    expect(document.activeElement).toBe(appButton('vscode'))
  })

  it('keeps icon, label and selection in separate fixed slots and updates the primary brand', async () => {
    const f = await mount()
    await f.openApps()
    for (const id of ['default', 'reveal', 'vscode']) {
      const row = appButton(id)
      expect(row.children).toHaveLength(3)
      expect(row.children[0].classList.contains('workspace-application-icon')).toBe(true)
      expect(row.children[1].classList.contains('workspace-resource-actions__item-label')).toBe(true)
      expect(row.children[2].classList.contains('workspace-resource-actions__item-check')).toBe(true)
      expect(row.children[2].querySelector('svg') !== null).toBe(id === 'reveal')
    }
    expect(appButton('vscode').querySelector('img')?.getAttribute('src')).toBe(vscodeIcon)
    appButton('vscode').click(); await flush()
    expect(f.primary().querySelector<HTMLElement>('.workspace-application-icon')?.dataset.applicationIcon).toBe('vscode')
    expect(f.primary().querySelector('img')?.getAttribute('src')).toBe(vscodeIcon)
    await f.openApps()
    expect(appButton('reveal').querySelector('.workspace-resource-actions__item-check')?.children).toHaveLength(0)
    expect(appButton('vscode').querySelector('.workspace-resource-actions__item-check svg')).not.toBeNull()
  })

  it('keeps copy and download in the separate more menu', async () => {
    const f = await mount()
    await f.openMore()
    expect(document.querySelector('[role="menuitemradio"]')).toBeNull()
    menuButton('Copy relative path').click(); await flush()
    expect(browser.copy).toHaveBeenCalledWith('notes.md')
    await f.openMore()
    menuButton('Download').click(); await flush()
    expect(f.files.read).toHaveBeenCalledWith('session-a', expect.objectContaining({ revision: 'version-a', workspaceBinding: 'root-a' }), expect.any(AbortSignal))
    expect(browser.download).toHaveBeenCalledOnce()
    expect(f.files.openWith).not.toHaveBeenCalled()
  })

  it('uses download as the remote primary action and explains the device boundary', async () => {
    const f = await mount({ available: false })
    expect(f.primary().textContent).toContain('Download')
    expect(f.primary().title).toContain('Download a copy to open it on your device')
    expect(f.el.querySelector('[data-testid="workspace-open-app-menu"]')).toBeNull()
    f.primary().click(); await flush()
    expect(browser.download).toHaveBeenCalledOnce()
    expect(f.files.openWith).not.toHaveBeenCalled()
    await f.openMore()
    expect(document.body.textContent).toContain('Remote connections cannot launch applications on the workspace computer')
    expect(document.querySelector('[role="menuitemradio"]')).toBeNull()
  })

  it('remembers only explicit choices in the same Gateway identity, without overwriting them during a remote fallback', async () => {
    const f = await mount()
    const identity = f.gateway.deliveryIdentity
    await f.openApps(); appButton('vscode').click(); await flush()
    f.files.apps.mockResolvedValue(catalog(false))
    f.props.file = { ...f.props.file!, path: 'next.md', name: 'next.md' }; await flush()
    expect(f.primary().textContent).toContain('Download')
    f.files.apps.mockResolvedValue(catalog())
    f.props.file = { ...f.props.file!, revision: 'version-b' }; await flush()
    expect(f.primary().textContent).toContain('Visual Studio Code')
    f.gateway.deliveryIdentity = 'different-gateway'; await flush()
    expect(f.primary().textContent).toContain('File manager')
    f.gateway.deliveryIdentity = identity; await flush()
    expect(f.primary().textContent).toContain('Visual Studio Code')
    expect(f.files.openWith).toHaveBeenCalledOnce()
  })

  it.each([
    ['host-permission-required', 'requires permission to execute'],
    ['desktop-unavailable', 'has no available desktop'],
  ])('explains %s without exposing an enabled native launch', async (reason, explanation) => {
    const f = await mount({ catalog: async () => ({ ...catalog(false), reason }) })
    expect(f.primary().title).toContain(explanation)
    expect(f.primary().textContent).toContain('Download')
    expect(f.el.querySelector('[data-testid="workspace-open-app-menu"]')).toBeNull()
    await f.openMore()
    expect(document.body.textContent).toContain(explanation)
    expect(f.files.openWith).not.toHaveBeenCalled()
  })

  it('retains a failed version-conflict action and asks for reload', async () => {
    const f = await mount()
    f.files.openWith.mockRejectedValue(new WorkspaceFilesChangedError())
    await f.openApps(); appButton('vscode').click(); await flush()
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('file changed')
    expect(appButton('vscode').getAttribute('aria-checked')).toBe('true')
    expect(browser.download).not.toHaveBeenCalled()
  })

  it('also exposes errors from the direct primary action', async () => {
    const f = await mount()
    f.files.openWith.mockRejectedValue(new Error('not installed'))
    f.primary().click(); await flush()
    expect(f.el.querySelector('[role="alert"]')?.textContent).toContain('Could not open this file')
    expect(f.primary().disabled).toBe(false)
  })

  it('resets the open menu when workspace authority changes and rejects a late catalog', async () => {
    const old = deferred<Catalog>()
    const f = await mount({ catalog: () => old.promise })
    expect(f.primary().disabled).toBe(true)
    await f.openApps()
    expect(document.querySelector('[role="status"]')?.textContent).toContain('Checking applications')
    const oldSignal = f.files.apps.mock.calls[0]?.[1]
    f.files.apps.mockResolvedValue(catalog(false))
    f.props.file = { ...f.props.file!, workspaceBinding: 'root-b' }; await flush()
    expect(oldSignal?.aborted).toBe(true)
    expect(document.querySelector('[role="menu"]')).toBeNull()
    old.resolve(catalog()); await flush()
    expect(f.primary().textContent).toContain('Download')
    expect(f.el.querySelector('[data-testid="workspace-open-app-menu"]')).toBeNull()
  })

  it('never downloads stale bytes after a file version changes during download', async () => {
    const f = await mount({ available: false })
    const read = deferred<Blob>()
    f.files.read.mockReturnValue(read.promise)
    f.primary().click(); await flush()
    f.props.file = { ...f.props.file!, revision: 'version-b' }; await flush()
    read.resolve(new Blob(['old'])); await flush()
    expect(browser.download).not.toHaveBeenCalled()
    expect(f.primary().disabled).toBe(false)
  })

  it('does not let an old open failure overwrite a new resource or close its menu', async () => {
    const f = await mount()
    const opening = deferred<void>()
    f.files.openWith.mockReturnValue(opening.promise)
    f.primary().click(); await flush()
    f.props.file = { ...f.props.file!, revision: 'version-b' }; await flush()
    await f.openApps()
    opening.reject(new WorkspaceFilesChangedError()); await flush()
    expect(document.querySelector('[role="menu"]')).not.toBeNull()
    expect(document.querySelector('[role="alert"]')).toBeNull()
  })

  it('supports menu keyboard navigation and restores focus on Escape', async () => {
    const f = await mount()
    const arrow = f.el.querySelector<HTMLButtonElement>('[data-testid="workspace-open-app-menu"]')!
    arrow.focus(); key('ArrowDown'); await flush()
    expect(document.activeElement).toBe(appButton('reveal'))
    key('End'); expect(document.activeElement).toBe(appButton('vscode'))
    key('Home'); expect(document.activeElement).toBe(appButton('default'))
    key('ArrowUp'); expect(document.activeElement).toBe(appButton('vscode'))
    key('Escape'); await flush()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(arrow)
  })

  it('allows retry after catalog failure instead of leaving the opener permanently disabled', async () => {
    const f = await mount({ catalog: async () => { throw new Error('offline') } })
    expect(f.primary().textContent).toContain('Download')
    await f.openMore()
    expect(document.body.textContent).toContain('Could not check available applications')
    f.files.apps.mockResolvedValue(catalog())
    menuButton('Check applications again').click(); await flush()
    key('Escape'); await flush()
    expect(f.primary().textContent).toContain('File manager')
    expect(f.primary().disabled).toBe(false)
  })

  it('gates launch on connection health, while copying the known relative path still works offline', async () => {
    const f = await mount()
    f.gateway.isAvailable = false; await flush()
    expect(f.primary().disabled).toBe(true)
    await f.openMore(); menuButton('Copy relative path').click(); await flush()
    expect(browser.copy).toHaveBeenCalledWith('notes.md')
    expect(f.files.openWith).not.toHaveBeenCalled()
    f.gateway.isAvailable = true; await flush()
    expect(f.primary().textContent).toContain('File manager')
    expect(f.primary().disabled).toBe(false)
  })

  it.each(['canExecuteHost', 'isLocalOwner'] as const)('retires the open application menu when %s is revoked on the same connection', async permission => {
    const f = await mount()
    await f.openApps(); appButton('vscode').click(); await flush()
    await f.openApps()
    const catalogReads = f.files.apps.mock.calls.length
    f.gateway[permission] = false; await flush()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(f.el.querySelector('[data-testid="workspace-open-app-menu"]')).toBeNull()
    expect(f.primary().textContent).toContain('Download')
    expect(f.primary().title).toContain('requires permission to execute')
    expect(f.files.apps).toHaveBeenCalledTimes(catalogReads)
    expect(f.files.openWith).toHaveBeenCalledOnce()
    f.gateway[permission] = true; await flush()
    expect(f.primary().textContent).toContain('Visual Studio Code')
    expect(f.files.apps).toHaveBeenCalledTimes(catalogReads + 1)
    expect(f.files.openWith).toHaveBeenCalledOnce()
  })

  it('does not offer an actionable file control without a selected authorized file', async () => {
    const f = await mount({ file: null })
    expect(f.primary().disabled).toBe(true)
    expect([...f.el.querySelectorAll('button')].every(button => button.disabled)).toBe(true)
    expect(f.files.apps).not.toHaveBeenCalled()
    expect(f.files.openWith).not.toHaveBeenCalled()
    expect(f.files.read).not.toHaveBeenCalled()
  })

  it('does not bypass version authority through a native opener when the file has no revision', async () => {
    const f = await mount()
    f.files.apps.mockClear()
    f.props.file = { ...f.props.file!, revision: undefined, nativeActions: true }; await flush()
    expect(f.primary().textContent).toContain('Download')
    expect(f.files.apps).not.toHaveBeenCalled()
    f.primary().click(); await flush()
    expect(f.files.openWith).not.toHaveBeenCalled()
    expect(browser.download).toHaveBeenCalledOnce()
  })
})
