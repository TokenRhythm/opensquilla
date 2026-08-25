// @vitest-environment happy-dom

import { createPinia } from 'pinia'
import { createApp, defineComponent, h, nextTick, reactive, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'

import en from '@/locales/en.json'
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import type { ArtifactDocument, ArtifactSourceSnapshot } from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import {
  createLegacyArtifactWorkspace,
  type ArtifactDocumentProvider,
} from '@/workbench/artifactDocumentProvider'
import ArtifactHtmlStudio from './ArtifactHtmlStudio.vue'

const copyTextWithFallback = vi.hoisted(() => vi.fn().mockResolvedValue(undefined))

vi.mock('@/utils/browser', () => ({ copyTextWithFallback }))

const monaco = vi.hoisted(() => {
  let value = ''
  let onChange: (() => void) | null = null
  let onSelection: (() => void) | null = null
  let selected = { start: 0, end: 0 }
  let readOnly = false
  return {
    editor: {
      getValue: () => value,
      setValue(next: string) {
        value = next
        onChange?.()
      },
      getSelection: () => ({
        getStartPosition: () => ({ offset: selected.start }),
        getEndPosition: () => ({ offset: selected.end }),
      }),
      getModel: () => ({
        getOffsetAt: (position: { offset: number }) => position.offset,
      }),
      onDidChangeModelContent(callback: () => void) {
        onChange = callback
        return { dispose: () => { onChange = null } }
      },
      onDidChangeCursorSelection(callback: () => void) {
        onSelection = callback
        return { dispose: () => { onSelection = null } }
      },
      updateOptions(options: { readOnly?: boolean }) {
        if (typeof options.readOnly === 'boolean') readOnly = options.readOnly
      },
      dispose: () => undefined,
    },
    input(next: string) {
      if (readOnly) return
      value = next
      onChange?.()
    },
    forceInput(next: string) {
      value = next
      onChange?.()
    },
    setInitialOptions(options: { readOnly?: boolean }) {
      readOnly = options.readOnly === true
    },
    isReadOnly: () => readOnly,
    select(start: number, end: number) {
      selected = { start, end }
      onSelection?.()
    },
    reset() {
      value = ''
      onChange = null
      onSelection = null
      selected = { start: 0, end: 0 }
      readOnly = false
    },
  }
})

vi.mock('monaco-editor', () => ({
  editor: {
    create: (_element: unknown, options: { readOnly?: boolean }) => {
      monaco.setInitialOptions(options)
      return monaco.editor
    },
  },
}))
vi.mock('monaco-editor/editor/editor.worker.js?worker', () => ({
  default: class EditorWorker {},
}))
vi.mock('monaco-editor/language/html/html.worker.js?worker', () => ({
  default: class HtmlWorker {},
}))

const artifact: ArtifactPayload = {
  id: 'artifact-html',
  name: 'page.html',
  mime: 'text/html',
  download_url: '/api/v1/artifacts/artifact-html',
}

const documentModel: ArtifactDocument = {
  documentId: 'document-html',
  sessionKey: 'session-a',
  sessionId: 'session-id',
  name: 'page.html',
  kind: 'html',
  headRevisionId: 'revision-1',
  latestDownloadUrl: '/api/v1/artifact-documents/document-html',
  generation: 1,
  stateRevision: 1,
  createdAt: null,
  updatedAt: null,
  schemaVersion: 1,
  capabilities: {
    download: true,
    preview: true,
    selectionContext: true,
    manualEdit: true,
    agentEdit: true,
    publish: true,
    edit: true,
    revisions: true,
    changeSets: true,
    comments: true,
    source: true,
    reason: null,
  },
}

const apps: App<Element>[] = []

afterEach(() => {
  vi.useRealTimers()
  apps.splice(0).forEach(app => app.unmount())
  vi.restoreAllMocks()
  document.body.innerHTML = ''
  monaco.reset()
  copyTextWithFallback.mockReset().mockResolvedValue(undefined)
})

describe('ArtifactHtmlStudio', () => {
  it('starts on source mount, serializes heartbeat and save, then closes the session', async () => {
    const startEditSession = vi.fn().mockResolvedValue({
      editSessionId: 'edit-session-1',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-1',
      mode: 'edit',
      status: 'active',
      stateRevision: 1,
      expiresAt: Date.now() + 60_000,
    })
    const heartbeatEditSession = vi.fn().mockResolvedValue({
      editSessionId: 'edit-session-1',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-1',
      mode: 'edit',
      status: 'active',
      stateRevision: 2,
      expiresAt: Date.now() + 60_000,
    })
    const patchSource = vi.fn().mockResolvedValue({
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
      language: 'html',
      content: '',
      sha256: 'b'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: 1,
      stateRevision: 2,
      editSession: {
        editSessionId: 'edit-session-1',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-1',
        lastSavedRevisionId: 'revision-2',
        mode: 'edit',
        status: 'active',
        stateRevision: 3,
        expiresAt: Date.now() + 60_000,
      },
    })
    const closeEditSession = vi.fn().mockResolvedValue({
      editSessionId: 'edit-session-1',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-2',
      mode: 'edit',
      status: 'closed',
      stateRevision: 4,
      expiresAt: Date.now() + 60_000,
    })
    const provider = {
      startEditSession,
      heartbeatEditSession,
      closeEditSession,
      readSource: vi.fn().mockResolvedValue({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<h1>Before</h1>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot),
      patchSource,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: {
      flush: () => Promise<boolean>
      beforeClose: (options?: { preserveRuntime?: boolean }) => Promise<boolean>
    } | null = null
    const timeoutSpy = vi.spyOn(globalThis, 'setTimeout')
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)

    await vi.waitFor(() => expect(startEditSession).toHaveBeenCalledOnce())
    expect(startEditSession).toHaveBeenCalledWith(expect.objectContaining({
      sessionKey: 'session-a',
      documentId: documentModel.documentId,
      mode: 'edit',
      clientRequestId: expect.stringMatching(/^edit-session-/),
    }))
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<h1>Before</h1>'))
    const heartbeatCallback = timeoutSpy.mock.calls.find(call => (
      call[1] === 20_000 && (call[0] as { name?: string }).name === 'onHeartbeatTimer'
    ))?.[0]
    expect(heartbeatCallback).toBeTypeOf('function')
    heartbeatCallback?.()
    await vi.waitFor(() => expect(heartbeatEditSession).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      editSessionId: 'edit-session-1',
      expectedStateRevision: 1,
    }))

    monaco.input('<h1>After</h1>')
    await expect(studio!.flush()).resolves.toBe(true)
    expect(patchSource).toHaveBeenCalledWith(expect.objectContaining({
      editSessionId: 'edit-session-1',
      expectedEditSessionStateRevision: 2,
      expectedLastSavedRevisionId: 'revision-1',
      expectedHeadRevisionId: 'revision-1',
      clientRequestId: expect.stringMatching(/^document-save-/),
    }))
    await expect(studio!.beforeClose({ preserveRuntime: true })).resolves.toBe(true)
    expect(closeEditSession).not.toHaveBeenCalled()
    await expect(studio!.beforeClose()).resolves.toBe(true)
    expect(closeEditSession).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      editSessionId: 'edit-session-1',
      expectedStateRevision: 3,
    })
  })

  it('keeps the dirty buffer editable while a failed heartbeat reacquires editing', async () => {
    let resolveReacquire!: (value: Record<string, unknown>) => void
    const heartbeatEditSession = vi.fn().mockRejectedValue(new Error('EditSession stale'))
    const patchSource = vi.fn().mockResolvedValue({
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
      language: 'html',
      content: '',
      sha256: 'b'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: 1,
      stateRevision: 2,
      editSession: {
        editSessionId: 'edit-session-reacquired',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-1',
        lastSavedRevisionId: 'revision-2',
        mode: 'edit',
        status: 'active',
        stateRevision: 2,
        expiresAt: Date.now() + 60_000,
      },
    })
    const startEditSession = vi.fn()
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-stale',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-1',
        lastSavedRevisionId: 'revision-1',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
        expiresAt: Date.now() + 60_000,
      })
      .mockReturnValueOnce(new Promise(resolve => {
        resolveReacquire = resolve
      }))
    const provider = {
      startEditSession,
      heartbeatEditSession,
      closeEditSession: vi.fn(),
      patchSource,
      readSource: vi.fn().mockResolvedValue({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<p>base</p>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot),
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: { flush: () => Promise<boolean> } | null = null
    const timeoutSpy = vi.spyOn(globalThis, 'setTimeout')
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>base</p>'))

    const heartbeatCallback = timeoutSpy.mock.calls.find(call => (
      call[1] === 20_000 && (call[0] as { name?: string }).name === 'onHeartbeatTimer'
    ))?.[0]
    expect(heartbeatCallback).toBeTypeOf('function')
    heartbeatCallback?.()
    await vi.waitFor(() => expect(heartbeatEditSession).toHaveBeenCalledOnce())
    await vi.waitFor(() => expect(startEditSession).toHaveBeenCalledTimes(2))
    expect(monaco.isReadOnly()).toBe(false)

    monaco.input('<p>kept while reconnecting</p>')
    const saving = studio!.flush()
    expect(patchSource).not.toHaveBeenCalled()
    resolveReacquire({
      editSessionId: 'edit-session-reacquired',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-1',
      mode: 'edit',
      status: 'active',
      stateRevision: 1,
      expiresAt: Date.now() + 60_000,
    })
    await expect(saving).resolves.toBe(true)
    expect(monaco.editor.getValue()).toBe('<p>kept while reconnecting</p>')
    expect(monaco.isReadOnly()).toBe(false)
    expect(patchSource).toHaveBeenCalledWith(expect.objectContaining({
      editSessionId: 'edit-session-reacquired',
      expectedLastSavedRevisionId: 'revision-1',
    }))
    expect(host.textContent).not.toContain('EditSession stale')
  })

  it('loads the current clean head when reacquisition discovers a server update', async () => {
    const startEditSession = vi.fn()
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-clean-old',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-1',
        lastSavedRevisionId: 'revision-1',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
      })
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-clean-current',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-2',
        lastSavedRevisionId: 'revision-2',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
      })
    const heartbeatEditSession = vi.fn().mockRejectedValue(new Error('private lease detail'))
    const readSource = vi.fn().mockImplementation(
      ({ revisionId }: { revisionId: string }) => Promise.resolve({
        documentId: documentModel.documentId,
        revisionId,
        language: 'html',
        content: revisionId === 'revision-2' ? '<p>current</p>' : '<p>old</p>',
        sha256: (revisionId === 'revision-2' ? 'b' : 'a').repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: revisionId === 'revision-2' ? 2 : 1,
      } satisfies ArtifactSourceSnapshot),
    )
    const provider = {
      startEditSession,
      heartbeatEditSession,
      closeEditSession: vi.fn(),
      patchSource: vi.fn(),
      readSource,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    const timeoutSpy = vi.spyOn(globalThis, 'setTimeout')
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ArtifactHtmlStudio, {
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    })
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)

    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>old</p>'))
    const heartbeatCallback = timeoutSpy.mock.calls.find(call => (
      call[1] === 20_000 && (call[0] as { name?: string }).name === 'onHeartbeatTimer'
    ))?.[0]
    heartbeatCallback?.()

    await vi.waitFor(() => expect(startEditSession).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>current</p>'))
    expect(readSource).toHaveBeenLastCalledWith(expect.objectContaining({
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
    }))
    expect(monaco.isReadOnly()).toBe(false)
    expect(host.textContent).not.toContain('private lease detail')
  })

  it('closes a session whose start response arrives during component teardown', async () => {
    let resolveStart!: (value: Record<string, unknown>) => void
    const startEditSession = vi.fn().mockReturnValue(new Promise(resolve => {
      resolveStart = resolve
    }))
    const closeEditSession = vi.fn().mockResolvedValue({
      editSessionId: 'edit-session-late',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-1',
      mode: 'edit',
      status: 'closed',
      stateRevision: 2,
      expiresAt: Date.now() + 60_000,
    })
    const provider = {
      startEditSession,
      heartbeatEditSession: vi.fn(),
      closeEditSession,
      readSource: vi.fn(),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ArtifactHtmlStudio, {
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    })
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    app.mount(host)
    await vi.waitFor(() => expect(startEditSession).toHaveBeenCalledOnce())

    app.unmount()
    resolveStart({
      editSessionId: 'edit-session-late',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-1',
      mode: 'edit',
      status: 'active',
      stateRevision: 1,
      expiresAt: Date.now() + 60_000,
    })

    await vi.waitFor(() => expect(closeEditSession).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      editSessionId: 'edit-session-late',
      expectedStateRevision: 1,
    }))
    expect(provider.readSource).not.toHaveBeenCalled()
  })

  it('ignores stale loads and never replaces edits made while a reload is pending', async () => {
    let resolveFirst!: (value: ArtifactSourceSnapshot) => void
    let resolveSecond!: (value: ArtifactSourceSnapshot) => void
    let resolveThird!: (value: ArtifactSourceSnapshot) => void
    const firstRead = new Promise<ArtifactSourceSnapshot>(resolve => { resolveFirst = resolve })
    const secondRead = new Promise<ArtifactSourceSnapshot>(resolve => { resolveSecond = resolve })
    const thirdRead = new Promise<ArtifactSourceSnapshot>(resolve => { resolveThird = resolve })
    const readSource = vi.fn()
      .mockReturnValueOnce(firstRead)
      .mockReturnValueOnce(secondRead)
      .mockReturnValueOnce(thirdRead)
    const provider = {
      readSource,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: { reload: () => Promise<boolean> } | null = null
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(readSource).toHaveBeenCalledOnce())
    expect(monaco.isReadOnly()).toBe(true)
    monaco.input('<p>must not be accepted while loading</p>')
    expect(monaco.editor.getValue()).toBe('')

    const latestLoad = studio!.reload()
    await vi.waitFor(() => expect(readSource).toHaveBeenCalledTimes(2))
    resolveSecond({
      documentId: documentModel.documentId,
      revisionId: 'revision-1',
      language: 'html',
      content: '<p>latest</p>',
      sha256: 'b'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: null,
      stateRevision: 1,
    })
    await expect(latestLoad).resolves.toBe(true)
    expect(monaco.editor.getValue()).toBe('<p>latest</p>')
    expect(monaco.isReadOnly()).toBe(false)

    resolveFirst({
      documentId: documentModel.documentId,
      revisionId: 'revision-1',
      language: 'html',
      content: '<p>stale</p>',
      sha256: 'a'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: null,
      stateRevision: 1,
    })
    await nextTick()
    expect(monaco.editor.getValue()).toBe('<p>latest</p>')

    const guardedReload = studio!.reload()
    await vi.waitFor(() => expect(readSource).toHaveBeenCalledTimes(3))
    monaco.forceInput('<p>local edit</p>')
    resolveThird({
      documentId: documentModel.documentId,
      revisionId: 'revision-1',
      language: 'html',
      content: '<p>remote</p>',
      sha256: 'c'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: null,
      stateRevision: 1,
    })
    await expect(guardedReload).resolves.toBe(false)
    await nextTick()
    expect(monaco.editor.getValue()).toBe('<p>local edit</p>')
    expect(
      host.querySelectorAll<HTMLButtonElement>('.artifact-html-studio__action')[0]?.disabled,
    ).toBe(false)
  })

  it('keeps edits made during save dirty and autosaves the next generation', async () => {
    let resolveFirst!: (value: ArtifactSourceSnapshot) => void
    const firstSave = new Promise<ArtifactSourceSnapshot>(resolve => {
      resolveFirst = resolve
    })
    const readSource = vi.fn().mockResolvedValue({
      documentId: documentModel.documentId,
      revisionId: 'revision-1',
      language: 'html',
      content: '<p>😀 base</p>',
      sha256: 'a'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: null,
      stateRevision: 1,
    } satisfies ArtifactSourceSnapshot)
    const patchSource = vi.fn()
      .mockReturnValueOnce(firstSave)
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-3',
        language: 'html',
        content: '',
        sha256: 'c'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: 1,
        stateRevision: 3,
      } satisfies ArtifactSourceSnapshot)
    const provider = {
      readSource,
      patchSource,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: { flush: () => Promise<boolean> } | null = null
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(readSource).toHaveBeenCalledOnce())
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>😀 base</p>'))

    vi.useFakeTimers()
    monaco.input('<p>😀 first</p>')
    const saving = studio!.flush()
    await vi.waitFor(() => expect(patchSource).toHaveBeenCalledOnce())
    expect(patchSource).toHaveBeenNthCalledWith(1, expect.objectContaining({
      offsetEncoding: 'unicode-code-point',
      patches: [{ startOffset: 5, endOffset: 9, replacement: 'first' }],
    }))

    monaco.input('<p>😀 second</p>')
    resolveFirst({
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
      language: 'html',
      content: '',
      sha256: 'b'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: 1,
      stateRevision: 2,
    })
    await saving
    await nextTick()
    expect(host.querySelector('[data-state="dirty"]')).not.toBeNull()
    expect(monaco.editor.getValue()).toBe('<p>😀 second</p>')

    await vi.advanceTimersByTimeAsync(1_200)
    await vi.waitFor(() => expect(patchSource).toHaveBeenCalledTimes(2))
    expect(patchSource).toHaveBeenNthCalledWith(2, expect.objectContaining({
      expectedHeadRevisionId: 'revision-2',
      expectedStateRevision: 2,
      patches: [{ startOffset: 5, endOffset: 10, replacement: 'second' }],
    }))
    expect(patchSource.mock.calls[1]?.[0]?.clientRequestId)
      .not.toBe(patchSource.mock.calls[0]?.[0]?.clientRequestId)
    await nextTick()
    expect(host.querySelector('[data-state="dirty"]')).toBeNull()
  })

  it('accepts its own head event when it arrives before the save response', async () => {
    let resolveSave!: (value: ArtifactSourceSnapshot) => void
    const pendingSave = new Promise<ArtifactSourceSnapshot>(resolve => {
      resolveSave = resolve
    })
    const provider = {
      readSource: vi.fn().mockResolvedValue({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<p>before</p>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot),
      patchSource: vi.fn().mockReturnValue(pendingSave),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: { flush: () => Promise<boolean> } | null = null
    const state = reactive({ document: { ...documentModel } })
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: state.document,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>before</p>'))

    monaco.input('<p>after</p>')
    const saving = studio!.flush()
    await vi.waitFor(() => expect(provider.patchSource).toHaveBeenCalledOnce())
    state.document = {
      ...state.document,
      headRevisionId: 'revision-2',
      generation: 2,
      stateRevision: 2,
    }
    await nextTick()
    expect(monaco.isReadOnly()).toBe(false)

    resolveSave({
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
      language: 'html',
      content: '',
      sha256: 'b'.repeat(64),
      offsetEncoding: 'unicode-code-point',
      patchCount: 1,
      stateRevision: 2,
    })
    await expect(saving).resolves.toBe(true)
    await nextTick()
    expect(monaco.isReadOnly()).toBe(false)
    expect(host.querySelector('[data-testid="discard-and-load-latest"]')).toBeNull()
    expect(host.querySelector('[role="alert"]')).toBeNull()
  })

  it('reuses an opaque save identity after response loss without aliasing another patch', async () => {
    const transportLoss = (message: string) => Object.assign(new Error(message), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    })
    const patchSource = vi.fn()
      .mockRejectedValueOnce(transportLoss('response lost'))
      .mockRejectedValueOnce(transportLoss('response still unavailable'))
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-2',
        language: 'html',
        content: '',
        sha256: 'b'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: 1,
        stateRevision: 2,
      } satisfies ArtifactSourceSnapshot)
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-3',
        language: 'html',
        content: '',
        sha256: 'c'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: 1,
        stateRevision: 3,
      } satisfies ArtifactSourceSnapshot)
    const provider = {
      readSource: vi.fn().mockResolvedValue({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<p>base</p>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot),
      patchSource,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    const liveDocument = reactive({ ...documentModel })
    let studio: { flush: () => Promise<boolean> } | null = null
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: liveDocument,
      sessionKey: 'session-a',
      onSourceSaved: (revisionId: string) => {
        liveDocument.headRevisionId = revisionId
        liveDocument.generation += 1
        liveDocument.stateRevision += 1
      },
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>base</p>'))

    monaco.input('<p>first private replacement</p>')
    await expect(studio!.flush()).resolves.toBe(false)
    expect(patchSource).toHaveBeenCalledOnce()
    await expect(studio!.flush()).resolves.toBe(false)

    monaco.input('<p>second private replacement</p>')
    await expect(studio!.flush()).resolves.toBe(false)
    await expect(studio!.flush()).resolves.toBe(true)

    const requestIds = patchSource.mock.calls.map(call => String(call[0]?.clientRequestId))
    expect(requestIds[0]).toMatch(/^document-save-/)
    expect(requestIds[1]).toBe(requestIds[0])
    expect(requestIds[2]).toBe(requestIds[0])
    expect(requestIds[3]).not.toBe(requestIds[0])
    expect(requestIds.join(' ')).not.toContain('private replacement')
    expect(patchSource.mock.calls[0]?.[0]).toMatchObject({
      expectedHeadRevisionId: 'revision-1',
      patches: [{ startOffset: 3, endOffset: 7, replacement: 'first private replacement' }],
    })
    expect(patchSource.mock.calls[3]?.[0]).toMatchObject({
      expectedHeadRevisionId: 'revision-2',
      patches: [expect.objectContaining({ replacement: 'second' })],
    })
  })

  it('resolves a committed response loss without replaying the source write', async () => {
    const patchSource = vi.fn().mockRejectedValue(Object.assign(new Error('private disconnect'), {
      code: 'RPC_TRANSPORT_ERROR',
      accepted: null,
    }))
    const resolveMutation = vi.fn().mockResolvedValue({
      status: 'applied',
      retryAfterMs: null,
      result: {
        documentId: documentModel.documentId,
        revisionId: 'revision-2',
        sha256: 'b'.repeat(64),
        stateRevision: 2,
      },
    })
    const provider = {
      readSource: vi.fn().mockResolvedValue({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<p>base</p>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot),
      patchSource,
      resolveMutation,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: { flush: () => Promise<boolean> } | null = null
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: documentModel,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<p>base</p>'))

    monaco.input('<p>committed</p>')
    await expect(studio!.flush()).resolves.toBe(true)

    expect(patchSource).toHaveBeenCalledOnce()
    expect(resolveMutation).toHaveBeenCalledOnce()
    expect(resolveMutation).toHaveBeenCalledWith(expect.objectContaining({
      operation: 'source.patch',
      requestId: patchSource.mock.calls[0]?.[0]?.clientRequestId,
    }))
    expect(host.textContent).not.toContain('private disconnect')
  })

  it('restarts editing and reloads an externally advanced head when the buffer is clean', async () => {
    const liveDocument = reactive({ ...documentModel })
    const startEditSession = vi.fn()
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-clean-1',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-1',
        lastSavedRevisionId: 'revision-1',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
        expiresAt: Date.now() + 60_000,
      })
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-clean-2',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-2',
        lastSavedRevisionId: 'revision-2',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
        expiresAt: Date.now() + 60_000,
      })
    const readSource = vi.fn()
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<h1>Original</h1>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot)
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-2',
        language: 'html',
        content: '<h1>Agent update</h1>',
        sha256: 'b'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 2,
      } satisfies ArtifactSourceSnapshot)
    const closeEditSession = vi.fn(async (request: {
      editSessionId: string
    }) => ({
      editSessionId: request.editSessionId,
      documentId: documentModel.documentId,
      baseRevisionId: request.editSessionId.endsWith('-1') ? 'revision-1' : 'revision-2',
      lastSavedRevisionId: request.editSessionId.endsWith('-1') ? 'revision-1' : 'revision-2',
      mode: 'edit' as const,
      status: 'closed' as const,
      stateRevision: 2,
      expiresAt: Date.now() + 60_000,
    }))
    const provider = {
      startEditSession,
      heartbeatEditSession: vi.fn(),
      closeEditSession,
      readSource,
      patchSource: vi.fn(),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      artifact,
      document: liveDocument,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<h1>Original</h1>'))

    liveDocument.headRevisionId = 'revision-2'
    liveDocument.generation = 2
    liveDocument.stateRevision = 2

    await vi.waitFor(() => expect(startEditSession).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<h1>Agent update</h1>'))
    expect(closeEditSession).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      editSessionId: 'edit-session-clean-1',
      expectedStateRevision: 1,
    })
    expect(readSource).toHaveBeenLastCalledWith({
      sessionKey: 'session-a',
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
    })
    expect(monaco.isReadOnly()).toBe(false)
    expect(host.querySelector('[data-testid="discard-and-load-latest"]')).toBeNull()
    expect(host.querySelector('[role="alert"]')).toBeNull()

    monaco.input('<h1>Continue editing</h1>')
    await nextTick()
    expect(host.querySelector('[data-state="dirty"]')).not.toBeNull()
  })

  it('fails closed on a new head and lets the user copy or explicitly discard stale source', async () => {
    const liveDocument = reactive({ ...documentModel })
    const startEditSession = vi.fn()
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-before-conflict',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-1',
        lastSavedRevisionId: 'revision-1',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
        expiresAt: Date.now() + 60_000,
      })
      .mockResolvedValueOnce({
        editSessionId: 'edit-session-after-conflict',
        documentId: documentModel.documentId,
        baseRevisionId: 'revision-2',
        lastSavedRevisionId: 'revision-2',
        mode: 'edit',
        status: 'active',
        stateRevision: 1,
        expiresAt: Date.now() + 60_000,
      })
    const readSource = vi.fn()
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-1',
        language: 'html',
        content: '<h1>Original</h1>',
        sha256: 'a'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 1,
      } satisfies ArtifactSourceSnapshot)
      .mockResolvedValueOnce({
        documentId: documentModel.documentId,
        revisionId: 'revision-2',
        language: 'html',
        content: '<h1>Remote</h1>',
        sha256: 'b'.repeat(64),
        offsetEncoding: 'unicode-code-point',
        patchCount: null,
        stateRevision: 2,
      } satisfies ArtifactSourceSnapshot)
    const closeEditSession = vi.fn().mockResolvedValue({
      editSessionId: 'edit-session-before-conflict',
      documentId: documentModel.documentId,
      baseRevisionId: 'revision-1',
      lastSavedRevisionId: 'revision-1',
      mode: 'edit',
      status: 'closed',
      stateRevision: 2,
      expiresAt: Date.now() + 60_000,
    })
    const patchSource = vi.fn()
    const provider = {
      startEditSession,
      heartbeatEditSession: vi.fn(),
      closeEditSession,
      readSource,
      patchSource,
      loadWorkspace: vi.fn().mockResolvedValue(
        createLegacyArtifactWorkspace(artifact, 'session-a'),
      ),
    } as unknown as ArtifactDocumentProvider
    const pinia = createPinia()
    useArtifactDocumentsStore(pinia).setProvider(provider)
    let studio: {
      discardAndLoadLatest: () => Promise<boolean>
      flush: () => Promise<boolean>
    } | null = null
    const host = document.createElement('div')
    document.body.append(host)
    const Root = defineComponent(() => () => h(ArtifactHtmlStudio, {
      ref: (value: unknown) => { studio = value as typeof studio },
      artifact,
      document: liveDocument,
      sessionKey: 'session-a',
    }))
    const app = createApp(Root)
    app.use(pinia)
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    apps.push(app)
    app.mount(host)
    await vi.waitFor(() => expect(monaco.editor.getValue()).toBe('<h1>Original</h1>'))

    vi.useFakeTimers()
    monaco.input('<h1>Local unsaved</h1>')
    liveDocument.headRevisionId = 'revision-2'
    liveDocument.stateRevision = 2
    await nextTick()

    expect(monaco.isReadOnly()).toBe(true)
    expect(host.querySelector('[data-testid="copy-unsaved-source"]')).not.toBeNull()
    expect(host.querySelector('[data-testid="discard-and-load-latest"]')).not.toBeNull()
    await vi.advanceTimersByTimeAsync(1_200)
    await expect(studio!.flush()).resolves.toBe(false)
    expect(patchSource).not.toHaveBeenCalled()
    const copyButton = host.querySelector<HTMLButtonElement>(
      '[data-testid="copy-unsaved-source"]',
    )
    expect(copyButton?.disabled).toBe(false)
    copyButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await vi.waitFor(() => {
      expect(copyTextWithFallback).toHaveBeenCalledWith('<h1>Local unsaved</h1>')
    })

    await expect(studio!.discardAndLoadLatest()).resolves.toBe(true)
    expect(closeEditSession).toHaveBeenCalledWith({
      sessionKey: 'session-a',
      editSessionId: 'edit-session-before-conflict',
      expectedStateRevision: 1,
    })
    expect(startEditSession).toHaveBeenCalledTimes(2)
    expect(startEditSession.mock.calls[1]?.[0]?.clientRequestId)
      .not.toBe(startEditSession.mock.calls[0]?.[0]?.clientRequestId)
    expect(readSource).toHaveBeenLastCalledWith({
      sessionKey: 'session-a',
      documentId: documentModel.documentId,
      revisionId: 'revision-2',
    })
    expect(monaco.editor.getValue()).toBe('<h1>Remote</h1>')
    expect(monaco.isReadOnly()).toBe(false)
    expect(host.querySelector('[data-testid="copy-unsaved-source"]')).toBeNull()
  })
})
