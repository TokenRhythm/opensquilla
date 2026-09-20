import type { WorkspaceReferences } from '@/modules/workspaceReferences'
import { workspaceReferenceErrorKey } from '@/modules/workspaceReferences'
import { workspaceFileReference } from '@/workbench/workspaceFileItems'
import type { WorkbenchItem, WorkbenchPanelDefinition, WorkbenchPanelRuntime, WorkbenchRuntimeContext } from '@/workbench/types'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'

class WorkspaceFileRuntime implements WorkbenchPanelRuntime {
  private pending: AbortController | null = null
  constructor(private readonly access: WorkspaceReferences, private readonly context: WorkbenchRuntimeContext) {}
  activate(item: WorkbenchItem) { void this.load(item) }
  resume(item: WorkbenchItem) {
    const state = this.context.getRenderState()
    if (!state.snapshot && !state.loading) void this.load(item)
  }
  update(item: WorkbenchItem) { void this.load(item) }
  performAction(action: string, item: WorkbenchItem) { if (action === 'refresh') void this.load(item) }
  suspend() {
    this.pending?.abort()
    this.pending = null
    this.context.updateRenderState({ snapshot: null, loading: false, errorKey: '' })
  }
  dispose() { this.suspend() }
  private async load(item: WorkbenchItem) {
    this.pending?.abort()
    this.pending = null
    const reference = workspaceFileReference(item)
    if (!reference || item.scope.type !== 'session') {
      this.context.updateRenderState({ loading: false, errorKey: 'workspaceReference.invalid', snapshot: null })
      return
    }
    const request = new AbortController()
    this.pending = request
    this.context.updateRenderState({ loading: true, errorKey: '', snapshot: null })
    try {
      const snapshot = await this.access.read(item.scope.id, reference, request.signal)
      if (!request.signal.aborted && this.context.isItemOpen()) this.context.updateRenderState({ snapshot })
    } catch (error) {
      if (!request.signal.aborted) this.context.updateRenderState({ errorKey: workspaceReferenceErrorKey(error) })
    } finally {
      if (this.pending === request) {
        this.pending = null
        this.context.updateRenderState({ loading: false })
      }
    }
  }
}

export function createWorkspaceFileDefinition(access: WorkspaceReferences, t: (key: string) => string): WorkbenchPanelDefinition {
  return {
    kind: 'file',
    component: WorkspaceFilePanel,
    supports: item => !!workspaceFileReference(item),
    getHeader: item => ({ title: item.title, subtitle: t('workspaceReference.readonly'), icon: 'fileText' }),
    getToolbarItems: () => [{ kind: 'action', id: 'refresh', icon: 'refresh', label: t('workspaceReference.refresh') }],
    getProps: (_item, state) => ({ ...state.runtimeState }),
    createRuntime: (_item, context) => new WorkspaceFileRuntime(access, context),
  }
}
