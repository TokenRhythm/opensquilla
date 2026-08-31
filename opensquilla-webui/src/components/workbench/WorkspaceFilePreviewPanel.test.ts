// @vitest-environment happy-dom

import { createApp, nextTick, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import en from '@/locales/en.json'
import { WORKSPACE_FILES_KEY, type WorkspaceFiles } from '@/modules/workspaceFiles'
import { WORKSPACE_FILE_ATTACH_EVENT } from '@/workbench/workspaceFileAttachEvent'
import {
  createWorkspaceFileWorkbenchItem,
  workspaceFileFromWorkbenchItem,
  workspaceFileWorkbenchItemId,
} from '@/workbench/workspaceFileItems'
import { createWorkspaceFileWorkbenchDefinition } from './workspaceFileWorkbenchProvider'

const monacoEditor = vi.hoisted(() => {
  const calls = {
    modelValue: '',
    createOptions: null as Record<string, unknown> | null,
  }
  const state = { hasSelection: false, selectedText: '' }
  const handlers = { scroll: [] as Array<() => void> }
  return {
    calls,
    state,
    handlers,
    reset() {
      calls.modelValue = ''
      calls.createOptions = null
      state.hasSelection = false
      state.selectedText = ''
      handlers.scroll = []
    },
  }
})

vi.mock('monaco-editor', () => ({
  editor: {
    create: (_container: HTMLElement, options: Record<string, unknown>) => {
      monacoEditor.calls.createOptions = options
      return {
        getModel: () => ({
          getLanguageId: () => 'plaintext',
          getValue: () => monacoEditor.calls.modelValue,
          setValue: (value: string) => {
            monacoEditor.calls.modelValue = value
          },
          getValueInRange: () => monacoEditor.state.selectedText,
        }),
        getSelection: () => ({
          isEmpty: () => !monacoEditor.state.hasSelection,
        }),
        onDidScrollChange: (callback: () => void) => {
          monacoEditor.handlers.scroll.push(callback)
          return { dispose: () => undefined }
        },
        dispose: () => undefined,
      }
    },
    setModelLanguage: () => undefined,
  },
}))
vi.mock('monaco-editor/editor/editor.worker.js?worker', () => ({
  default: class EditorWorker {},
}))

const readFileMock = vi.fn(async (_workspaceId: string, path: string) => ({
  path,
  size: 11,
  binary: false,
  truncated: false,
  content: 'README root\n',
}))

const filesPort: WorkspaceFiles = {
  listDir: vi.fn(async (_workspaceId: string, path: string) => ({ path, entries: [] })),
  readFile: readFileMock,
}

const writeText = vi.fn<(text: string) => Promise<undefined>>(async () => undefined)

function stubClipboard() {
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
}

const apps: App<Element>[] = []
let host: HTMLElement | null = null

async function mountPanel(path: string) {
  host = document.createElement('div')
  document.body.append(host)
  const Panel = (await import('./WorkspaceFilePreviewPanel.vue')).default
  const app = createApp(Panel, {
    workspace: { id: 'ws-1', name: 'proj', path: 'C:/tmp/proj' },
    path,
    rootPath: 'C:/tmp/proj',
  })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(WORKSPACE_FILES_KEY, filesPort)
  apps.push(app)
  app.mount(host)
  return host
}

function ctxMenuElement(): HTMLElement | null {
  return document.querySelector('[data-ws-file-ctx-menu]')
}

/** Drive the real DOM path: the panel owns the contextmenu event. */
function fireContainerContextMenu(clientX = 200, clientY = 150) {
  const container = document.querySelector('.ws-file-preview__editor')
  if (!container) throw new Error('editor container not rendered')
  container.dispatchEvent(new MouseEvent('contextmenu', {
    bubbles: true,
    cancelable: true,
    clientX,
    clientY,
    button: 2,
  }))
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  host = null
  readFileMock.mockClear()
  writeText.mockClear()
  monacoEditor.reset()
  vi.restoreAllMocks()
})

describe('WorkspaceFilePreviewPanel', () => {
  it('fetches once per mount and renders the content in the editor', async () => {
    await mountPanel('README.md')
    await vi.waitFor(() => expect(readFileMock).toHaveBeenCalledTimes(1))
    // The initial mount race (editor created before/after the fetch) must
    // still deliver the content into the editor exactly once.
    await vi.waitFor(() => expect(monacoEditor.calls.modelValue).toBe('README root\n'))
    // Give any accidental state-triggered refetch several macro-task cycles
    // to manifest before asserting the count stayed at one.
    await new Promise(resolve => setTimeout(resolve, 50))
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(readFileMock).toHaveBeenCalledTimes(1)
  })

  it('creates the editor with its native context menu disabled', async () => {
    await mountPanel('README.md')
    await vi.waitFor(() => expect(monacoEditor.calls.createOptions).not.toBeNull())
    expect(monacoEditor.calls.createOptions?.contextmenu).toBe(false)
  })

  it('refetches exactly once when the panel is reused for another path', async () => {
    await mountPanel('README.md')
    await vi.waitFor(() => expect(readFileMock).toHaveBeenCalledTimes(1))
    // Re-mount with a different path simulates workbench panel reuse via
    // :key-less patching; the panel must load the new file once.
    apps.splice(0).forEach(app => app.unmount())
    document.body.innerHTML = ''
    await mountPanel('src/lib/b.ts')
    await vi.waitFor(() => expect(readFileMock).toHaveBeenCalledTimes(2))
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(readFileMock).toHaveBeenCalledTimes(2)
    expect(readFileMock.mock.calls[1]?.slice(0, 2)).toEqual(['ws-1', 'src/lib/b.ts'])
  })

  it('shows the selection context menu only when text is selected', async () => {
    await mountPanel('README.md')
    await vi.waitFor(() => expect(monacoEditor.calls.modelValue).not.toBe(''))

    fireContainerContextMenu(120, 80)
    await nextTick()
    // No selection: no custom menu, and Monaco's native menu must not
    // exist either (contextmenu:false).
    expect(ctxMenuElement()).toBeNull()
    expect(document.querySelector('.monaco-menu-container')).toBeNull()
    expect(document.querySelector('.context-view')).toBeNull()

    monacoEditor.state.hasSelection = true
    monacoEditor.state.selectedText = 'README root'
    fireContainerContextMenu(120, 80)
    await nextTick()
    const menu = ctxMenuElement()
    expect(menu).not.toBeNull()
    expect(menu?.style.left).toBe('120px')
    expect(menu?.style.top).toBe('80px')
    const items = menu?.querySelectorAll('button')
    expect(items?.length).toBe(2)
    expect(items?.[0]?.textContent?.trim()).toBe('Copy')
    expect(items?.[1]?.textContent?.trim()).toBe('Add to conversation')
  })

  it('copies the selected text from the context menu', async () => {
    stubClipboard()
    await mountPanel('README.md')
    await vi.waitFor(() => expect(monacoEditor.calls.modelValue).not.toBe(''))

    monacoEditor.state.hasSelection = true
    monacoEditor.state.selectedText = 'README root'
    fireContainerContextMenu()
    await nextTick()
    const menu = ctxMenuElement()
    expect(menu).not.toBeNull()
    ;(menu?.querySelectorAll('button')?.[0] as HTMLButtonElement).click()
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('README root'))
    await nextTick()
    expect(ctxMenuElement()).toBeNull()
  })

  it('attaches the selection to the conversation via the attach event', async () => {
    await mountPanel('README.md')
    await vi.waitFor(() => expect(monacoEditor.calls.modelValue).not.toBe(''))

    const attachSpy = vi.fn()
    window.addEventListener(WORKSPACE_FILE_ATTACH_EVENT, attachSpy)

    monacoEditor.state.hasSelection = true
    monacoEditor.state.selectedText = 'README root'
    fireContainerContextMenu()
    await nextTick()
    const menu = ctxMenuElement()
    expect(menu).not.toBeNull()
    ;(menu?.querySelectorAll('button')?.[1] as HTMLButtonElement).click()
    await vi.waitFor(() => expect(attachSpy).toHaveBeenCalledTimes(1))

    const detail = (attachSpy.mock.calls[0]?.[0] as CustomEvent).detail
    expect(detail).toMatchObject({
      workspaceId: 'ws-1',
      workspacePath: 'C:/tmp/proj',
      path: 'README.md',
      name: 'README.selection.txt',
      content: 'README root',
    })
    window.removeEventListener(WORKSPACE_FILE_ATTACH_EVENT, attachSpy)
  })

  it('closes the context menu on Escape and on outside mousedown', async () => {
    await mountPanel('README.md')
    await vi.waitFor(() => expect(monacoEditor.calls.modelValue).not.toBe(''))

    monacoEditor.state.hasSelection = true
    monacoEditor.state.selectedText = 'x'
    fireContainerContextMenu()
    await nextTick()
    expect(ctxMenuElement()).not.toBeNull()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(ctxMenuElement()).toBeNull()

    fireContainerContextMenu()
    await nextTick()
    expect(ctxMenuElement()).not.toBeNull()
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await nextTick()
    expect(ctxMenuElement()).toBeNull()
  })
})

describe('workspace file item openNonce', () => {
  it('stamps a fresh nonce per open while keeping the stable id', () => {
    const ref = {
      workspaceId: 'ws-1',
      workspaceName: 'proj',
      workspacePath: 'C:/tmp/proj',
      path: 'README.md',
    }
    const first = createWorkspaceFileWorkbenchItem(ref)
    const second = createWorkspaceFileWorkbenchItem(ref)
    expect(first.id).toBe(second.id)
    expect(second.id).toBe(workspaceFileWorkbenchItemId('ws-1', 'README.md'))
    const firstNonce = workspaceFileFromWorkbenchItem(first)?.openNonce
    const secondNonce = workspaceFileFromWorkbenchItem(second)?.openNonce
    expect(typeof firstNonce).toBe('number')
    expect(secondNonce).toBeGreaterThan(firstNonce as number)
  })

  it('round-trips the nonce through the deserializer and provider props', () => {
    const item = createWorkspaceFileWorkbenchItem({
      workspaceId: 'ws-1',
      workspaceName: 'proj',
      workspacePath: 'C:/tmp/proj',
      path: 'README.md',
    })
    const ref = workspaceFileFromWorkbenchItem(item)
    expect(ref?.openNonce).toBeDefined()
    const state = {
      item,
      active: true,
      layoutMode: 'split',
      hostAvailable: true,
      nativeSurface: false,
      runtimeState: {},
    } as Parameters<
      NonNullable<
        ReturnType<typeof createWorkspaceFileWorkbenchDefinition>['getProps']
      >
    >[1]
    const props = createWorkspaceFileWorkbenchDefinition().getProps?.(item, state) ?? {}
    expect(props.openNonce).toBe(ref?.openNonce)
    expect(props.path).toBe('README.md')
  })
})
