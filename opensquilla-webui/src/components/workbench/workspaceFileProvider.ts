import type { WorkspaceReferences } from '@/modules/workspaceReferences'
import { workspaceReferenceErrorKey } from '@/modules/workspaceReferences'
import type { WorkspaceFiles } from '@/modules/workspaceFiles'
import { workspaceFilePayload, workspaceFileReference } from '@/workbench/workspaceFileItems'
import type { WorkbenchItem, WorkbenchPanelDefinition, WorkbenchPanelRuntime, WorkbenchRuntimeContext } from '@/workbench/types'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'

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
  constructor(
    private readonly references: WorkspaceReferences | null,
    private readonly files: WorkspaceFiles | null,
    private readonly context: WorkbenchRuntimeContext,
  ) {}
  activate(item: WorkbenchItem) { void this.load(item) }
  resume(item: WorkbenchItem) {
    const state = this.context.getRenderState()
    if (!state.snapshot && !state.loading) void this.load(item)
  }
  update(item: WorkbenchItem) { void this.load(item) }
  performAction(action: string, item: WorkbenchItem) { if (action === 'refresh') void this.load(item) }
  handleComponentEvent(event: { type: string; payload?: unknown }, item: WorkbenchItem) {
    if (item.scope.type !== 'session') return
    const payload = event.payload
    const value = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {}
    if (event.type === 'workspace-file-page') {
      const startLine = Number(value.startLine)
      const focusLine = Number(value.focusLine)
      if (!Number.isSafeInteger(startLine) || startLine < 1) return
      void this.load(item, startLine, Number.isSafeInteger(focusLine) ? focusLine : undefined)
    } else if (event.type === 'workspace-file-search' && typeof value.query === 'string') {
      void this.search(item, value.query)
    }
  }
  suspend() {
    this.pending?.abort()
    this.pending = null
    this.context.updateRenderState({ snapshot: null, loading: false, errorKey: '' })
  }
  dispose() { this.suspend() }
  private async search(item: WorkbenchItem, query: string) {
    if (item.scope.type !== 'session') return
    const file = workspaceFilePayload(item)
    if (!file || file.kind !== 'text' || !this.files?.readPage || !query.trim()) return
    this.pending?.abort()
    const request = new AbortController()
    this.pending = request
    try {
      const sessionKey = item.scope.id
      let startLine = 1
      let totalLines = Number.MAX_SAFE_INTEGER
      const needle = query.trim().toLocaleLowerCase()
      while (!request.signal.aborted && startLine <= totalLines) {
        const page = await this.files.readPage(
          sessionKey, file, startLine, startLine + 199, request.signal,
        )
        totalLines = page.totalLines
        const lines = page.content.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/)
        const match = lines.findIndex(line => line.toLocaleLowerCase().includes(needle))
        if (match >= 0) {
          await this.load(item, startLine, startLine + match)
          return
        }
        startLine += 200
      }
    } catch (error) {
      if (!request.signal.aborted) this.context.updateRenderState({ errorKey: workspaceReferenceErrorKey(error) })
    } finally {
      if (this.pending === request) this.pending = null
    }
  }

  private async load(item: WorkbenchItem, requestedStartLine = 1, focusLine?: number) {
    this.pending?.abort()
    this.pending = null
    const reference = workspaceFileReference(item)
    const file = workspaceFilePayload(item)
    if ((!reference && !file) || item.scope.type !== 'session') {
      this.context.updateRenderState({ loading: false, errorKey: 'workspaceReference.invalid', snapshot: null })
      return
    }
    const request = new AbortController()
    this.pending = request
    this.context.updateRenderState({ loading: true, errorKey: '', snapshot: null })
    try {
      let snapshot: WorkspaceFileViewSnapshot
      if (reference && this.references) {
        snapshot = await this.references.read(item.scope.id, reference, request.signal)
      } else if (file && this.files) {
        if (file.kind !== 'text') throw new Error('workspaceReference.unsupported')
        if (this.files.readPage) {
          try {
            snapshot = {
              ...(await this.files.readPage(
                item.scope.id, file, requestedStartLine, requestedStartLine + 199, request.signal,
              )),
              paged: true,
              ...(focusLine ? { focusLine } : {}),
            }
          } catch (error) {
            // Older Gateways predate the page endpoint. Preserve the existing
            // bounded full-read fallback for small files only.
            if (request.signal.aborted || file.size > 2 * 1024 * 1024) throw error
            const blob = await this.files.read(item.scope.id, file, request.signal)
            const content = await blob.text()
            if (content.includes('\u0000')) throw new Error('workspaceReference.unsupported')
            const totalLines = lineCount(content)
            snapshot = { relativePath: file.path, content, totalLines, startLine: 1, endLine: totalLines }
          }
        } else {
          if (file.size > 2 * 1024 * 1024) throw new Error('workspaceReference.unsupported')
          const blob = await this.files.read(item.scope.id, file, request.signal)
          const content = await blob.text()
          if (content.includes('\u0000')) throw new Error('workspaceReference.unsupported')
          const totalLines = lineCount(content)
          snapshot = {
            relativePath: file.path,
            content,
            totalLines,
            startLine: 1,
            endLine: totalLines,
          }
        }
      } else {
        throw new Error('workspaceReference.unavailable')
      }
      if (!request.signal.aborted && this.context.isItemOpen()) {
        const sessionKey = item.scope.type === 'session' ? item.scope.id : ''
        const copyContents = file && this.files
          ? async () => (await this.files!.read(sessionKey, file)).text()
          : undefined
        this.context.updateRenderState({ snapshot, copyContents })
      }
    } catch (error) {
      if (!request.signal.aborted) {
        const errorKey = error instanceof Error && error.message.startsWith('workspaceReference.')
          ? error.message
          : workspaceReferenceErrorKey(error)
        this.context.updateRenderState({ errorKey })
      }
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
