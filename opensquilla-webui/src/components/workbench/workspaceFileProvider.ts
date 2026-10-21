import type { WorkspaceReferences } from '@/modules/workspaceReferences'
import { WorkspaceReferenceError, workspaceReferenceErrorKey } from '@/modules/workspaceReferences'
import type { WorkspaceFiles } from '@/modules/workspaceFiles'
import { WorkspaceFilesChangedError, WorkspaceTextLimitError } from '@/modules/workspaceFiles'
import { workspaceFilePayload, workspaceFileReference, workspaceFileSessionKey } from '@/workbench/workspaceFileItems'
import type { WorkbenchItem, WorkbenchPanelDefinition, WorkbenchPanelRuntime, WorkbenchRuntimeContext } from '@/workbench/types'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import { copyTextWithFallback } from '@/utils/browser'
import { resourcePreviewKind, resourcePreviewLimit } from '@/workbench/resourcePreview'
import { completeWorkspaceMarkdown, isWorkspaceMarkdown, WORKSPACE_MARKDOWN_PREVIEW_LIMIT } from '@/workbench/workspaceTextPreview'
import { workspaceReadingIdentity, workspaceReadingState } from './workspaceReadingState'

interface WorkspaceFileViewSnapshot {
  relativePath: string
  content: string
  totalLines: number
  startLine: number
  endLine: number
  paged?: boolean
  focusLine?: number
}
export interface WorkspaceFileAnnotationOptions {
  openCurrentDocument?(item: WorkbenchItem, isCurrent: () => boolean): Promise<boolean>
  onAnnotate?(item: WorkbenchItem, isCurrent: () => boolean): void | Promise<void>
  annotationAvailable?(item: WorkbenchItem): boolean
}
function annotationFormat(item: WorkbenchItem): boolean {
  const file = workspaceFilePayload(item)
  const path = file?.path || workspaceFileReference(item)?.locator.relativePath || ''
  return /\.(?:html?|docx|pptx|xlsx)$/i.test(path)
    || Boolean(file && ['html', 'docx', 'pptx', 'xlsx'].includes(resourcePreviewKind(file)))
}
function annotationAvailable(item: WorkbenchItem, options: WorkspaceFileAnnotationOptions): boolean {
  return Boolean(options.onAnnotate) && annotationFormat(item)
    && workspaceFilePayload(item)?.annotationCapability?.supported !== false
    && options.annotationAvailable?.(item) !== false
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
  private restoring = false
  private restoreEpoch = 0
  private annotationEpoch = 0
  constructor(
    private readonly references: WorkspaceReferences | null,
    private readonly files: WorkspaceFiles | null,
    private readonly context: WorkbenchRuntimeContext,
    private readonly annotation: WorkspaceFileAnnotationOptions = {},
  ) {}
  // Network work must not block the manager queue that delivers cancellation.
  activate(item: WorkbenchItem) { this.restore(item) }
  resume(item: WorkbenchItem) {
    const state = this.context.getRenderState()
    if (!this.restoring && !state.snapshot && !state.loading && !state.errorKey) this.restore(item)
  }
  update(item: WorkbenchItem) { this.restore(item) }
  private identity(item: WorkbenchItem) {
    const file = workspaceFilePayload(item)
    return file ? workspaceReadingIdentity(workspaceFileSessionKey(item), file)
      : JSON.stringify([workspaceFileSessionKey(item), workspaceFileReference(item)])
  }
  private restore(item: WorkbenchItem) {
    this.annotationEpoch += 1
    this.context.updateRenderState({ annotationPreparing: false })
    const epoch = ++this.restoreEpoch
    this.restoring = true
    const saved = this.context.getViewState?.()
    const readingState = saved?.identity === this.identity(item) ? workspaceReadingState(saved.reading) : undefined
    this.context.updateRenderState({ readingState })
    const load = async () => {
      if (epoch === this.restoreEpoch) await this.load(item, readingState?.startLine ?? 1, readingState?.focusLine, true)
    }
    void (this.context.runRestore ? this.context.runRestore(load) : load()).finally(() => {
      if (epoch === this.restoreEpoch) this.restoring = false
    })
  }
  performAction(action: string, item: WorkbenchItem) {
    if (action === 'annotate') { void this.prepareAnnotation(item); return }
    if (action === 'refresh') {
      this.annotationEpoch++
      this.context.updateRenderState({ annotationPreparing: false })
      this.restoreEpoch++
      this.restoring = false
      this.context.updateRenderState({ readingState: undefined })
      void this.load(item, 1, undefined, true)
    }
  }
  private async prepareAnnotation(item: WorkbenchItem) {
    if (!annotationAvailable(item, this.annotation) || this.context.getRenderState().annotationPreparing) return
    const epoch = ++this.annotationEpoch
    const isCurrent = () => epoch === this.annotationEpoch && this.context.isItemOpen()
    this.context.updateRenderState({ annotationPreparing: true })
    try { await this.annotation.onAnnotate!(item, isCurrent) }
    catch (error) { if (isCurrent()) this.context.reportError(error instanceof Error ? error : new Error(String(error))) }
    finally { if (isCurrent()) this.context.updateRenderState({ annotationPreparing: false }) }
  }
  handleComponentEvent(event: { type: string; payload?: unknown }, item: WorkbenchItem) {
    if (item.scope.type !== 'session') return
    const payload = event.payload
    const value = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {}
    if (event.type === 'workspace-file-reading-state') {
      const reading = workspaceReadingState(value)
      if (reading) this.context.saveViewState?.({ identity: this.identity(item), reading })
      return
    }
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
    this.annotationEpoch += 1
    this.restoreEpoch++
    this.restoring = false
    this.pending?.abort()
    this.pending = null
    this.cancelCopy()
    this.context.updateRenderState({ snapshot: null, previewBlob: null, previewErrorKey: '', loading: false, errorKey: '', copying: false,
      copied: false, copyErrorKey: '', searchStatus: 'idle', searchQuery: '', searchErrorKey: '', annotationPreparing: false })
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
        const blob = await this.files.read(workspaceFileSessionKey(item), file, request.signal)
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
      const result = await this.files.search(workspaceFileSessionKey(item), file, query, request.signal)
      if (!this.current(request)) return
      if (result.matchLine === null) {
        this.context.updateRenderState({ searchStatus: 'not-found' })
        return
      }
      const startLine = Math.floor((result.matchLine - 1) / 200) * 200 + 1
      const page = await this.files.readPage(workspaceFileSessionKey(item), file, startLine, startLine + 199, request.signal)
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
    if (error instanceof WorkspaceFilesChangedError) return 'workbench.workspaceFiles.changed'
    if (error instanceof WorkspaceTextLimitError) return error.limit === 'line'
      ? 'workbench.workspaceFiles.textLineLimit' : 'workbench.workspaceFiles.textPageLimit'
    if (error instanceof Error && ['workbench.artifactPreview.tooLarge', 'workbench.artifactPreview.unsupported'].includes(error.message)) return error.message
    return error instanceof Error && error.message.startsWith('workspaceReference.')
      ? error.message : workspaceReferenceErrorKey(error)
  }
  private async load(item: WorkbenchItem, requestedStartLine = 1, focusLine?: number, reset = false) {
    this.pending?.abort()
    this.pending = null
    this.cancelCopy()
    const reference = workspaceFileReference(item)
    const file = workspaceFilePayload(item)
    const previewKind = file ? resourcePreviewKind(file) : 'source'
    const richPreview = previewKind !== 'source' && previewKind !== 'unsupported'
    const canPreviewResource = file && richPreview && file.size <= resourcePreviewLimit(previewKind)
    const markdown = file?.kind === 'text' && isWorkspaceMarkdown(file)
    let previewBlob = !reset && canPreviewResource
      ? this.context.getRenderState().previewBlob as Blob | null : null
    let previewErrorKey = file && ((richPreview && !canPreviewResource)
      || (markdown && file.size > WORKSPACE_MARKDOWN_PREVIEW_LIMIT))
      ? 'workbench.artifactPreview.tooLarge' : ''
    this.context.updateRenderState({ loading: false, errorKey: '', snapshot: null, copying: false,
      previewBlob, previewKind, previewErrorKey,
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
      if (annotationFormat(item) && this.annotation.openCurrentDocument) {
        if (await this.annotation.openCurrentDocument(item, () => this.current(request))) return
        if (!this.current(request)) return
      }
      let snapshot: WorkspaceFileViewSnapshot | null = null
      if (reference && this.references) {
        snapshot = await this.references.read(workspaceFileSessionKey(item), reference, request.signal)
      } else if (file && this.files) {
        if (file.kind !== 'text') {
          if (!richPreview) throw new Error('workbench.artifactPreview.unsupported')
          if (!canPreviewResource) throw new Error('workbench.artifactPreview.tooLarge')
          previewBlob = await this.files.read(workspaceFileSessionKey(item), file, request.signal)
          if (previewBlob.size > resourcePreviewLimit(previewKind)) throw new Error('workbench.artifactPreview.tooLarge')
        } else if (markdown && file.size <= WORKSPACE_MARKDOWN_PREVIEW_LIMIT) {
          try {
            const blob = await this.files.read(workspaceFileSessionKey(item), file, request.signal)
            snapshot = await completeWorkspaceMarkdown(file.path, blob)
          } catch (error) {
            if (!this.current(request)) return
            if (error instanceof WorkspaceFilesChangedError || error instanceof WorkspaceReferenceError) throw error
            previewErrorKey = error instanceof Error && error.message === 'workbench.artifactPreview.tooLarge'
              ? error.message : 'workbench.artifactPreview.unsupported'
          }
        }
        if (!this.current(request)) return
        if (file.kind === 'text' && !snapshot && file.textPaging === true && this.files.readPage) {
          try {
            snapshot = { ...(await this.files.readPage(
              workspaceFileSessionKey(item), file, requestedStartLine, requestedStartLine + 199, request.signal,
            )), paged: true, ...(focusLine ? { focusLine } : {}) }
          } catch (error) {
            // Source-line limits must not prevent a bounded complete HTML/CSV
            // preview that works through the workspace tree. Authority failures
            // still reject both presentations.
            if (!canPreviewResource || !(error instanceof WorkspaceTextLimitError)) throw error
            previewErrorKey = this.errorKey(error)
          }
          if (!this.current(request)) return
          // A source page is not a complete document/table. Obtain the same
          // bounded full resource used by the workspace tree's shared preview.
          if (canPreviewResource && !previewBlob) {
            try {
              const blob = await this.files.read(workspaceFileSessionKey(item), file, request.signal)
              if (!this.current(request)) return
              if (blob.size <= resourcePreviewLimit(previewKind)) previewBlob = blob
              else previewErrorKey = 'workbench.artifactPreview.tooLarge'
            } catch (error) {
              if (!this.current(request)) return
              // Optional preview failure must not discard a successfully read
              // source page. Explicit revision/authority failures still reject
              // the page rather than presenting a contradicted snapshot.
              if (error instanceof WorkspaceFilesChangedError || error instanceof WorkspaceReferenceError) throw error
              previewErrorKey = 'workbench.artifactPreview.unsupported'
            }
          }
          if (!snapshot && !previewBlob) throw new Error('workspaceReference.unsupported')
        } else if (file.kind === 'text' && !snapshot) {
          // Hosts without text paging retain the bounded, small-file fallback.
          const limit = canPreviewResource ? resourcePreviewLimit(previewKind) : 2 * 1024 * 1024
          if (file.size > limit) throw new Error('workspaceReference.unsupported')
          const blob = await this.files.read(workspaceFileSessionKey(item), file, request.signal)
          if (!this.current(request)) return
          if (blob.size > limit) throw new Error('workspaceReference.unsupported')
          const content = await decodeText(blob)
          const totalLines = lineCount(content)
          snapshot = { relativePath: file.path, content, totalLines, startLine: 1, endLine: totalLines }
          if (canPreviewResource) previewBlob = blob
        }
      } else {
        throw new Error('workspaceReference.unavailable')
      }
      if (this.current(request)) this.context.updateRenderState({ snapshot, previewBlob, previewErrorKey })
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
  options?: WorkspaceFileAnnotationOptions,
): WorkbenchPanelDefinition
export function createWorkspaceFileDefinition(
  references: WorkspaceReferences | null,
  filesOrTranslate: WorkspaceFiles | null | ((key: string) => string),
  maybeTranslate?: (key: string) => string,
  options: WorkspaceFileAnnotationOptions = {},
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
          ? `${file.path} · ${formatType(file)} · ${formatSize(file.size)}${file.kind === 'text' ? ` · ${t('workspaceReference.readonly')}` : ''}`
          : t('workspaceReference.readonly'),
        icon: 'fileText',
      }
    },
    getToolbarItems: (item, state) => [
      ...(options.onAnnotate && annotationFormat(item) ? [{
        kind: 'action' as const, id: 'annotate', icon: 'pencil' as const, label: t('workbench.annotationUnified.start'),
        disabled: !annotationAvailable(item, options) || state.runtimeState.annotationPreparing === true,
        ...(!annotationAvailable(item, options) ? { tooltip: t(
          /\.html?$/i.test(workspaceFilePayload(item)?.path || workspaceFileReference(item)?.locator.relativePath || '')
            && options.annotationAvailable?.(item) === false
            ? 'workbench.artifactAnnotation.desktopEditingOnly'
            : 'workbench.annotationUnified.unavailable',
        ) } : {}),
      }] : []),
      { kind: 'action', id: 'refresh', icon: 'refresh', label: t('workspaceReference.refresh') },
    ],
    getProps: (item, state) => ({ ...state.runtimeState,
      file: workspaceFilePayload(item), sessionKey: workspaceFileSessionKey(item) }),
    createRuntime: (_item, context) => new WorkspaceFileRuntime(references, files, context, options),
  }
}
