// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import type { SandboxPathListResponse } from '@/types/rpc'
import ProjectWorkspacePickerDialog from './ProjectWorkspacePickerDialog.vue'

const PICKER_KEY = 'agent:main:webchat:picker'

const mocks = vi.hoisted(() => ({
  platform: {
    files: {} as {
      chooseProjectDirectory?: () => Promise<{ path: string } | null>
    },
  },
  rpcCall: vi.fn(),
}))

vi.mock('@/platform', () => ({
  getPlatform: () => mocks.platform,
}))

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: mocks.rpcCall }),
}))

const mountedApps: App<Element>[] = []

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        common: { close: 'Close', cancel: 'Cancel' },
        workspaces: {
          chooseProject: 'Choose project',
          webPickerScope: 'Paths are on the gateway host.',
          pathPlaceholder: 'Project path',
          projectPath: 'Project path',
          browse: 'Browse',
          choose: 'Choose',
          goToPath: 'Go to path',
          parentDirectory: 'Parent directory',
          retryDirectoryPicker: 'Retry',
          directoryPickerFailed: 'Directory picker failed: {error}',
          chooseSelectedDirectory: 'Choose selected directory',
          newDirectory: 'New folder',
          newDirectoryName: 'Folder name',
          createDirectory: 'Create',
          createDirectoryFailed: 'Could not create the folder: {error}',
        },
      },
    },
  })
}

function pathResult(
  currentPath: string,
  children: Array<string | {
    path: string
    kind?: 'directory' | 'file'
    selectable?: boolean
  }>,
  systemPickerAvailable = true,
): SandboxPathListResponse {
  const parent = currentPath === '/'
    ? null
    : currentPath.replace(/[\\/][^\\/]+$/, '') || '/'
  return {
    currentPath,
    path: currentPath,
    parentPath: parent,
    systemPickerAvailable,
    entries: children.map(child => {
      const item = typeof child === 'string' ? { path: child } : child
      const segments = item.path.split(/[\\/]/)
      return {
        name: segments[segments.length - 1] || item.path,
        path: item.path,
        kind: item.kind ?? 'directory',
        selectable: item.selectable ?? true,
      }
    }),
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok
    reject = fail
  })
  return { promise, resolve, reject }
}

async function flushPromises() {
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

async function mountPicker(options: { initialPath?: string; enabled?: boolean } = {}) {
  const events = { close: vi.fn(), choose: vi.fn() }
  const open = ref(true)
  const enabled = ref(options.enabled ?? true)
  const host = document.createElement('div')
  document.body.appendChild(host)
  const Root = defineComponent(() => () => h(ProjectWorkspacePickerDialog, {
    open: open.value,
    sessionKey: PICKER_KEY,
    initialPath: options.initialPath,
    enabled: enabled.value,
    onClose: events.close,
    onChoose: events.choose,
  }))
  const app = createApp(Root)
  app.use(i18n())
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  return {
    events,
    async setOpen(value: boolean) {
      open.value = value
      await nextTick()
    },
    async setEnabled(value: boolean) {
      enabled.value = value
      await nextTick()
    },
  }
}

function locationInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('[aria-label="Project path"]')
  if (!input) throw new Error('Missing project path input')
  return input
}

function setLocation(value: string) {
  const input = locationInput()
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

function submitLocation() {
  locationInput().dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter',
    bubbles: true,
  }))
}

function button(label: string): HTMLButtonElement {
  const match = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(candidate => candidate.textContent?.trim() === label)
  if (!match) throw new Error(`Missing button: ${label}`)
  return match
}

function option(label: string): HTMLButtonElement {
  const match = [...document.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    .find(candidate => candidate.textContent?.trim() === label)
  if (!match) throw new Error(`Missing option: ${label}`)
  return match
}

beforeEach(() => {
  mocks.platform.files = {}
  mocks.rpcCall.mockReset()
})

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('ProjectWorkspacePickerDialog', () => {
  it('does not invoke native or RPC pickers while project selection is disabled', async () => {
    const nativePicker = vi.fn()
    mocks.platform.files.chooseProjectDirectory = nativePicker

    await mountPicker({ enabled: false })
    await flushPromises()

    expect(nativePicker).not.toHaveBeenCalled()
    expect(mocks.rpcCall).not.toHaveBeenCalled()
    expect(document.querySelector('.project-picker')).toBeNull()
  })

  it('omits the initial web path and exposes only selectable directories', async () => {
    mocks.rpcCall.mockResolvedValue(pathResult('/repos', [
      '/repos/project-a',
      { path: '/repos/not-selectable', selectable: false },
      { path: '/repos/readme.txt', kind: 'file' },
    ]))

    await mountPicker()
    await flushPromises()

    expect(mocks.rpcCall).toHaveBeenCalledWith('sandbox.path.list', {
      sessionKey: PICKER_KEY,
      kind: 'workspace',
    })
    expect(document.body.textContent).toContain('project-a')
    expect(document.body.textContent).not.toContain('not-selectable')
    expect(document.body.textContent).not.toContain('readme.txt')
    expect(locationInput().value).toBe('/repos')
    expect(button('Choose selected directory').disabled).toBe(false)
  })

  it('renders directory actions as recognizable icon buttons', async () => {
    mocks.rpcCall.mockResolvedValue(pathResult('/repos', []))

    await mountPicker()
    await flushPromises()

    const parentAction = button('Parent directory')
    const browseAction = button('Browse')
    const createAction = button('New folder')
    const chooseAction = button('Choose selected directory')

    expect(parentAction.classList.contains('project-picker__action')).toBe(true)
    expect(browseAction.classList.contains('project-picker__action')).toBe(true)
    expect(createAction.classList.contains('project-picker__action')).toBe(true)
    expect(chooseAction.classList.contains('project-picker__choose')).toBe(true)
    expect(parentAction.querySelector('svg')).toBeTruthy()
    expect(browseAction.querySelector('svg')).toBeTruthy()
    expect(createAction.querySelector('svg')).toBeTruthy()
    expect(chooseAction.querySelector('svg')).toBeTruthy()
    expect(createAction.closest('.project-picker__browser-toolbar')).toBeTruthy()
  })

  it('selects on click and browses only on double click', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', ['/repos/a']))
      .mockResolvedValueOnce(pathResult('/repos/a', []))
    const { events } = await mountPicker()
    await flushPromises()

    const entry = option('a')
    entry.click()
    await nextTick()
    expect(mocks.rpcCall).toHaveBeenCalledTimes(1)
    expect(entry.getAttribute('aria-selected')).toBe('true')

    entry.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await flushPromises()
    expect(mocks.rpcCall).toHaveBeenLastCalledWith('sandbox.path.list', {
      sessionKey: PICKER_KEY,
      path: '/repos/a',
      kind: 'workspace',
    })
    expect(locationInput().value).toBe('/repos/a')
    expect(button('Choose selected directory').disabled).toBe(false)
    button('Choose selected directory').click()
    expect(events.choose).toHaveBeenCalledWith('/repos/a')
  })

  it('ignores an older browse response that resolves last', async () => {
    const first = deferred<SandboxPathListResponse>()
    const second = deferred<SandboxPathListResponse>()
    mocks.rpcCall.mockResolvedValueOnce(pathResult('/repos', ['/repos/a', '/repos/b']))
    await mountPicker()
    await flushPromises()
    mocks.rpcCall.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)

    setLocation('/repos/a')
    submitLocation()
    await nextTick()
    setLocation('/repos/b')
    submitLocation()
    second.resolve(pathResult('/repos/b', []))
    first.resolve(pathResult('/repos/a', []))
    await flushPromises()

    expect(locationInput().value).toBe('/repos/b')
  })

  it('uses the current directory as the base for relative locations', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', []))
      .mockResolvedValueOnce(pathResult('/repos/child', []))
    await mountPicker()
    await flushPromises()

    setLocation('child')
    submitLocation()
    await flushPromises()

    expect(mocks.rpcCall).toHaveBeenLastCalledWith('sandbox.path.list', {
      sessionKey: PICKER_KEY,
      path: 'child',
      basePath: '/repos',
      kind: 'workspace',
    })
  })

  it('creates a folder in the current directory and opens it', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', []))
      .mockResolvedValueOnce({ path: '/repos/new-project' })
      .mockResolvedValueOnce(pathResult('/repos/new-project', []))
    await mountPicker()
    await flushPromises()

    button('New folder').click()
    await nextTick()
    const nameInput = document.querySelector<HTMLInputElement>('[aria-label="Folder name"]')
    expect(nameInput).toBeTruthy()
    nameInput!.value = 'new-project'
    nameInput!.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    button('Create').click()
    await flushPromises()

    expect(mocks.rpcCall).toHaveBeenNthCalledWith(2, 'sandbox.path.create-directory', {
      sessionKey: PICKER_KEY,
      parentPath: '/repos',
      name: 'new-project',
      kind: 'workspace',
    })
    expect(mocks.rpcCall).toHaveBeenNthCalledWith(3, 'sandbox.path.list', {
      sessionKey: PICKER_KEY,
      path: '/repos/new-project',
      kind: 'workspace',
    })
    expect(locationInput().value).toBe('/repos/new-project')
  })

  it('ignores a create-folder response after close and reopen', async () => {
    const staleCreate = deferred<{ path: string }>()
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', []))
      .mockReturnValueOnce(staleCreate.promise)
      .mockResolvedValueOnce(pathResult('/fresh', []))
    const picker = await mountPicker()
    await flushPromises()

    button('New folder').click()
    await nextTick()
    const nameInput = document.querySelector<HTMLInputElement>('[aria-label="Folder name"]')!
    nameInput.value = 'stale'
    nameInput.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    button('Create').click()
    await nextTick()

    await picker.setOpen(false)
    await picker.setOpen(true)
    staleCreate.resolve({ path: '/repos/stale' })
    await flushPromises()

    expect(locationInput().value).toBe('/fresh')
    expect(mocks.rpcCall).toHaveBeenCalledTimes(3)
  })

  it('browses the real parent returned by the gateway', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos/a', []))
      .mockResolvedValueOnce(pathResult('/repos', ['/repos/a']))
    await mountPicker({ initialPath: '/repos/a' })
    await flushPromises()

    button('Parent directory').click()
    await flushPromises()

    expect(mocks.rpcCall).toHaveBeenLastCalledWith('sandbox.path.list', {
      sessionKey: PICKER_KEY,
      path: '/repos',
      kind: 'workspace',
    })
  })

  it('keeps the prior entries and selection when the latest browse fails', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', ['/repos/a']))
      .mockRejectedValueOnce(new Error('not readable'))
    const { events } = await mountPicker()
    await flushPromises()
    option('a').click()
    await nextTick()

    setLocation('/missing')
    submitLocation()
    await flushPromises()

    expect(document.body.textContent).toContain('not readable')
    expect(option('a').getAttribute('aria-selected')).toBe('true')
    expect(button('Choose selected directory').disabled).toBe(false)
    button('Choose selected directory').click()
    expect(events.choose).toHaveBeenCalledWith('/repos/a')
  })

  it('invalidates a pending response across close and reopen', async () => {
    const stale = deferred<SandboxPathListResponse>()
    const fresh = deferred<SandboxPathListResponse>()
    mocks.rpcCall.mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise)
    const picker = await mountPicker()

    await picker.setOpen(false)
    await picker.setOpen(true)
    fresh.resolve(pathResult('/fresh', []))
    stale.resolve(pathResult('/stale', []))
    await flushPromises()

    expect(locationInput().value).toBe('/fresh')
  })

  it('browses a selected directory from the keyboard', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', ['/repos/a']))
      .mockResolvedValueOnce(pathResult('/repos/a', []))
    await mountPicker()
    await flushPromises()

    const entry = option('a')
    entry.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await flushPromises()

    expect(mocks.rpcCall).toHaveBeenCalledTimes(2)
    expect(locationInput().value).toBe('/repos/a')
  })

  it('disables choosing while a browse request is pending', async () => {
    const pending = deferred<SandboxPathListResponse>()
    mocks.rpcCall.mockReturnValueOnce(pending.promise)
    await mountPicker()
    await nextTick()

    expect(button('Choose selected directory').disabled).toBe(true)

    pending.resolve(pathResult('/repos', []))
    await flushPromises()
    expect(button('Choose selected directory').disabled).toBe(false)
  })

  it('hides the gateway system picker when the host reports it unavailable', async () => {
    mocks.rpcCall.mockResolvedValue(pathResult('/repos', [], false))

    await mountPicker()
    await flushPromises()

    const browseAction = [...document.querySelectorAll<HTMLButtonElement>('button')]
      .find(candidate => candidate.textContent?.trim() === 'Browse')
    expect(browseAction).toBeUndefined()
  })

  it('opens the gateway system picker and immediately chooses its directory', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', []))
      .mockResolvedValueOnce({ path: '/Volumes/workspace/project' })
    const { events } = await mountPicker()
    await flushPromises()

    button('Browse').click()
    await flushPromises()

    expect(mocks.rpcCall).toHaveBeenNthCalledWith(2, 'sandbox.path.pick', {
      sessionKey: PICKER_KEY,
      kind: 'workspace',
      initialPath: '/repos',
    })
    expect(events.choose).toHaveBeenCalledOnce()
    expect(events.choose).toHaveBeenCalledWith('/Volumes/workspace/project')
  })

  it('keeps the web directory browser open when the system picker is cancelled', async () => {
    mocks.rpcCall
      .mockResolvedValueOnce(pathResult('/repos', ['/repos/a']))
      .mockResolvedValueOnce({ path: null })
    const { events } = await mountPicker()
    await flushPromises()

    button('Browse').click()
    await flushPromises()

    expect(events.choose).not.toHaveBeenCalled()
    expect(events.close).not.toHaveBeenCalled()
    expect(button('Browse')).toBeTruthy()
    expect(document.body.textContent).toContain('a')
  })

  it('uses the native desktop picker and closes on cancellation', async () => {
    mocks.platform.files.chooseProjectDirectory = vi.fn(async () => null)
    const { events } = await mountPicker()
    await flushPromises()

    expect(mocks.platform.files.chooseProjectDirectory).toHaveBeenCalledOnce()
    expect(events.choose).not.toHaveBeenCalled()
    expect(events.close).toHaveBeenCalledOnce()
    expect(mocks.rpcCall).not.toHaveBeenCalled()
  })

  it('ignores a stale native result after close and reopen', async () => {
    const stale = deferred<{ path: string } | null>()
    const fresh = deferred<{ path: string } | null>()
    mocks.platform.files.chooseProjectDirectory = vi.fn()
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(fresh.promise)
    const picker = await mountPicker()

    await picker.setOpen(false)
    await picker.setOpen(true)
    fresh.resolve({ path: '/repos/fresh' })
    await flushPromises()
    stale.resolve({ path: '/repos/stale' })
    await flushPromises()

    expect(mocks.platform.files.chooseProjectDirectory).toHaveBeenCalledTimes(2)
    expect(picker.events.choose).toHaveBeenCalledOnce()
    expect(picker.events.choose).toHaveBeenCalledWith('/repos/fresh')
  })

  it('shows native rejection and retries natively without web fallback', async () => {
    mocks.platform.files.chooseProjectDirectory = vi.fn()
      .mockRejectedValueOnce(new Error('native unavailable'))
      .mockResolvedValueOnce({ path: '/repos/native' })
    const { events } = await mountPicker()
    await flushPromises()

    expect(document.body.textContent).toContain('native unavailable')
    expect(mocks.rpcCall).not.toHaveBeenCalled()
    button('Retry').click()
    await flushPromises()

    expect(mocks.platform.files.chooseProjectDirectory).toHaveBeenCalledTimes(2)
    expect(events.choose).toHaveBeenCalledWith('/repos/native')
    expect(mocks.rpcCall).not.toHaveBeenCalled()
  })

  it('allows cancelling after a native picker rejection', async () => {
    mocks.platform.files.chooseProjectDirectory = vi.fn()
      .mockRejectedValue(new Error('native unavailable'))
    const { events } = await mountPicker()
    await flushPromises()

    button('Cancel').click()

    expect(events.close).toHaveBeenCalledOnce()
    expect(mocks.rpcCall).not.toHaveBeenCalled()
  })
})
