import type { WorkspaceReferences } from '@/modules/workspaceReferences'
import { workspaceReferenceErrorKey } from '@/modules/workspaceReferences'
import type { WorkspaceFiles } from '@/modules/workspaceFiles'
import { workspaceFilePayload, workspaceFileReference } from '@/workbench/workspaceFileItems'
import type { WorkbenchItem, WorkbenchPanelDefinition, WorkbenchPanelRuntime, WorkbenchRuntimeContext } from '@/workbench/types'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import { copyTextWithFallback } from '@/utils/browser'

interface WorkspaceFileViewSnapshot {
  relativePath: string
  content: string
  totalLines: number
  startLine: number
  endLine: number
  paged?: boolean
  focusLine?: number
}

function lineCount(content: string): number {
  const values = content.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/)
  if (values[values.length - 1] === '') values.pop()
  return Math.max(1, values.length)
}

async function decodeText(blob: Blob): Promise<string> {
  try {
    const content = new TextDecoder('utf-8', { fatal: true }).decode(await blob.arrayBuffer())
    if (content.includes('\u0000')) throw new Error('Binary content')
    return content
  } catch {
    throw new Error('workspaceReference.unsupported')
  }
}

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function formatType(file: { mime: string; kind: string }): string {
  const mime = file.mime.toLowerCase().split(';', 1)[0]
  const labels: Record<string, string> = {
    'text/x-python': 'Python', 'application/javascript': 'JavaScript', 'text/javascript': 'JavaScript',
    'text/markdown': 'Markdown', 'text/html': 'HTML', 'application/json': 'JSON',
  }
  return labels[mime] || (mime.split('/').pop() || file.kind).toUpperCase()
}

class WorkspaceFileRuntime implements WorkbenchPanelRuntime {
  private pending: AbortController | null = null
  private pendingCopy: AbortController | null = null
  private copyTimer: ReturnType<typeof setTimeout> | null = null
  private viewId = 0
  constructor(
    private readonly references: WorkspaceReferences | null,
    private readonly files: WorkspaceFiles | null,
    private readonly context: WorkbenchRuntimeContext,
  ) {}
  // Network work must not block the manager queue that delivers cancellation.
  activate(item: WorkbenchItem) { void this.load(item, 1, undefined, true) }
  resume(item: WorkbenchItem) {
    const state = this.context.getRenderState()
    if (!state.snapshot && !state.loading && !state.errorKey) void this.load(item, 1, undefined, true)
  }
  update(item: WorkbenchItem) { void this.load(item, 1, undefined, true) }
  performAction(action: string, item: WorkbenchItem) {
    if (action === 'refresh') void this.load(item, 1, undefined, true)
  }
  handleComponentEvent(event: { type: string; payload?: unknown }, item: WorkbenchItem) {
    if (item.scope.type !== 'session') return
    const payload = event.payload
    const value = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {}
    if (event.type === 'workspace-file-page') {
      const startLine = Number(value.startLine)
      const focusLine = Number(value.focusLine)
      const snapshot = this.context.getRenderState().snapshot as WorkspaceFileViewSnapshot | undefined
      if (!Number.isSafeInteger(startLine) || startLine < 1 || !snapshot || startLine > snapshot.totalLines) return
      void this.load(item, startLine, Number.isSafeInteger(focusLine)
        ? Math.max(startLine, Math.min(focusLine, startLine + 199, snapshot.totalLines)) : undefined)
      return
    }
    if (event.type === 'workspace-file-search' && typeof value.query === 'string') {
      void this.search(item, value.query)
      return
    }
    if (event.type === 'workspace-file-search-cancel') {
      if (this.context.getRenderState().searchStatus === 'searching') {
        this.pending?.abort()
        this.pending = null
      }
      this.context.updateRenderState({ searchStatus: 'idle', searchQuery: '', searchErrorKey: '' })
    }
    if (event.type === 'workspace-file-copy') void this.copy(item)
  }
  private cancelCopy() {
    this.pendingCopy?.abort()
    this.pendingCopy = null
    if (this.copyTimer) clearTimeout(this.copyTimer)
    this.copyTimer = null
  }
  suspend() {
    this.pending?.abort()
    this.pending = null
    this.cancelCopy()
    this.context.updateRenderState({ snapshot: null, loading: false, errorKey: '', copying: false,
      copied: false, copyErrorKey: '', searchStatus: 'idle', searchQuery: '', searchErrorKey: '' })
  }
  dispose() { this.suspend() }
  private current(request: AbortController) {
    return !request.signal.aborted && this.context.isItemOpen()
  }
  private async copy(item: WorkbenchItem) {
    const snapshot = this.context.getRenderState().snapshot as WorkspaceFileViewSnapshot | null
    if (!snapshot || item.scope.type !== 'session') return
    this.cancelCopy()
    const request = new AbortController()
    this.pendingCopy = request
    this.context.updateRenderState({ copying: true, copied: false, copyErrorKey: '' })
    try {
      const file = workspaceFilePayload(item)
      // Page snapshots are never used as the complete file's clipboard content.
      let content = snapshot.content
      if (file && this.files) {
        const blob = await this.files.read(item.scope.id, file, request.signal)
        if (!this.current(request)) return
        content = await decodeText(blob)
      }
      if (!this.current(request)) return
      await copyTextWithFallback(content)
      if (!this.current(request)) return
      this.context.updateRenderState({ copied: true })
      this.copyTimer = setTimeout(() => {
        if (this.current(request)) this.context.updateRenderState({ copied: false })
      }, 1600)
    } catch {
      if (this.current(request)) this.context.updateRenderState({ copyErrorKey: 'workspaceReference.copyFailed' })
    } finally {
      if (this.pendingCopy === request) this.context.updateRenderState({ copying: false })
    }
  }
  private async search(item: WorkbenchItem, rawQuery: string) {
    if (item.scope.type !== 'session') return
    const file = workspaceFilePayload(item)
    const query = rawQuery.trim()
    if (!file || file.kind !== 'text' || !query || query.length > 512) return
    this.pending?.abort()
    const request = new AbortController()
    this.pending = request
    this.context.updateRenderState({ searchStatus: 'searching', searchQuery: query, searchErrorKey: '' })
    try {
      if (!this.files?.search || !this.files.readPage || file.textPaging !== true) {
        throw new Error('workspaceReference.unavailable')
      }
      const result = await this.files.search(item.scope.id, file, query, request.signal)
      if (!this.current(request)) return
      if (result.matchLine === null) {
        this.context.updateRenderState({ searchStatus: 'not-found' })
        return
      }
      const startLine = Math.floor((result.matchLine - 1) / 200) * 200 + 1
      const page = await this.files.readPage(item.scope.id, file, startLine, startLine + 199, request.signal)
      if (!this.current(request)) return
      this.context.updateRenderState({
        snapshot: { ...page, paged: true, focusLine: result.matchLine }, searchStatus: 'found',
      })
    } catch (error) {
      if (this.current(request)) this.context.updateRenderState({ searchStatus: 'idle', searchErrorKey: this.errorKey(error) })
    } finally {
      if (this.pending === request) this.pending = null
    }
  }
  private errorKey(error: unknown) {
    return error instanceof Error && error.message.startsWith('workspaceReference.')
      ? error.message : workspaceReferenceErrorKey(error)
  }
  private async load(item: WorkbenchItem, requestedStartLine = 1, focusLine?: number, reset = false) {
    this.pending?.abort()
    this.pending = null
    this.cancelCopy()
    const reference = workspaceFileReference(item)
    const file = workspaceFilePayload(item)
    this.context.updateRenderState({ loading: false, errorKey: '', snapshot: null, copying: false,
      copied: false, copyErrorKey: '', searchStatus: 'idle', searchQuery: '', searchErrorKey: '',
      ...(reset ? { viewId: ++this.viewId } : {}) })
    if ((!reference && !file) || item.scope.type !== 'session') {
      this.context.updateRenderState({ errorKey: 'workspaceReference.invalid' })
      return
    }
    const request = new AbortController()
    this.pending = request
    this.context.updateRenderState({ loading: true })
    try {
      let snapshot: WorkspaceFileViewSnapshot
      if (reference && this.references) {
        snapshot = await this.references.read(item.scope.id, reference, request.signal)
      } else if (file && this.files) {
        if (file.kind !== 'text') throw new Error('workspaceReference.unsupported')
        if (file.textPaging === true && this.files.readPage) {
          snapshot = { ...(await this.files.readPage(
            item.scope.id, file, requestedStartLine, requestedStartLine + 199, request.signal,
          )), paged: true, ...(focusLine ? { focusLine } : {}) }
        } else {
          // Hosts without text paging retain the bounded, small-file fallback.
          if (file.size > 2 * 1024 * 1024) throw new Error('workspaceReference.unsupported')
          const blob = await this.files.read(item.scope.id, file, request.signal)
          if (!this.current(request)) return
          if (blob.size > 2 * 1024 * 1024) throw new Error('workspaceReference.unsupported')
          const content = await decodeText(blob)
          const totalLines = lineCount(content)
          snapshot = { relativePath: file.path, content, totalLines, startLine: 1, endLine: totalLines }
        }
      } else {
        throw new Error('workspaceReference.unavailable')
      }
      if (this.current(request)) this.context.updateRenderState({ snapshot })
    } catch (error) {
      if (this.current(request)) this.context.updateRenderState({ errorKey: this.errorKey(error) })
    } finally {
      if (this.pending === request) {
        this.pending = null
        this.context.updateRenderState({ loading: false })
      }
    }
  }
}

export function createWorkspaceFileDefinition(
  references: WorkspaceReferences | null,
  t: (key: string) => string,
): WorkbenchPanelDefinition
export function createWorkspaceFileDefinition(
  references: WorkspaceReferences | null,
  files: WorkspaceFiles | null,
  t: (key: string) => string,
): WorkbenchPanelDefinition
export function createWorkspaceFileDefinition(
  references: WorkspaceReferences | null,
  filesOrTranslate: WorkspaceFiles | null | ((key: string) => string),
  maybeTranslate?: (key: string) => string,
): WorkbenchPanelDefinition {
  const files = typeof filesOrTranslate === 'function' ? null : filesOrTranslate
  const t = typeof filesOrTranslate === 'function' ? filesOrTranslate : maybeTranslate!
  return {
    kind: 'file',
    component: WorkspaceFilePanel,
    supports: item => !!workspaceFileReference(item) || !!workspaceFilePayload(item),
    getHeader: item => {
      const file = workspaceFilePayload(item)
      return {
        title: file?.name || item.title,
        subtitle: file
          ? `${file.path} · ${formatType(file)} · ${formatSize(file.size)} · ${t('workspaceReference.readonly')}`
          : t('workspaceReference.readonly'),
        icon: 'fileText',
      }
    },
    getToolbarItems: () => [{ kind: 'action', id: 'refresh', icon: 'refresh', label: t('workspaceReference.refresh') }],
    getProps: (_item, state) => ({ ...state.runtimeState }),
    createRuntime: (_item, context) => new WorkspaceFileRuntime(references, files, context),
  }
}
