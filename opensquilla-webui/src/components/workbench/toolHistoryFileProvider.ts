import { copyTextWithFallback } from '@/utils/browser'
import { downloadToolHistoryContent, toolHistoryFileFromItem, toolHistoryFileSessionKey } from '@/workbench/toolHistoryFileItems'
import { normalizeToolFileSnapshot } from '@/utils/chat/toolFileSnapshot'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import type { WorkbenchItem, WorkbenchPanelDefinition, WorkbenchPanelRuntime, WorkbenchRuntimeContext } from '@/workbench/types'
import HistoricalToolFilePanel from './HistoricalToolFilePanel.vue'

class ToolHistoryFileRuntime implements WorkbenchPanelRuntime {
  private generation = 0
  private copyTimer: ReturnType<typeof setTimeout> | undefined

  constructor(private readonly context: WorkbenchRuntimeContext, private readonly contentAccess?: ArtifactContentAccess) {}

  performAction(actionId: string, item: WorkbenchItem) {
    if (actionId === 'wrap') {
      this.context.updateRenderState({ wrapLines: !this.context.getRenderState().wrapLines })
    } else if (actionId === 'copy') {
      void this.copy(item)
    } else if (actionId === 'download') {
      void this.download(item)
    }
  }

  private async copy(item: WorkbenchItem) {
    const view = toolHistoryFileFromItem(item)
    if (!view || view.truncated) return
    this.resetCopy()
    const generation = this.generation
    let copyState: 'copied' | 'error'
    try { await copyTextWithFallback(view.content); copyState = 'copied' }
    catch { copyState = 'error' }
    if (generation !== this.generation || !this.context.isItemOpen()) return
    this.context.updateRenderState({ copyState })
    this.copyTimer = setTimeout(() => {
      this.copyTimer = undefined
      if (generation === this.generation && this.context.isItemOpen()) this.context.updateRenderState({ copyState: 'idle' })
    }, 1600)
  }

  private async download(item: WorkbenchItem) {
    const view = toolHistoryFileFromItem(item)
    if (!view || item.scope.type !== 'session') return
    this.resetCopy()
    const generation = this.generation
    try {
      const snapshot = normalizeToolFileSnapshot(item.payload.fileSnapshot)
      const content = typeof item.payload.fullContent === 'string' ? item.payload.fullContent : view.content
      if (view.truncated && !snapshot && typeof item.payload.fullContent !== 'string') throw new Error('Full record is unavailable')
      await downloadToolHistoryContent({ ...view, content }, { sessionKey: toolHistoryFileSessionKey(item), fileSnapshot: snapshot }, this.contentAccess)
    } catch {
      if (generation === this.generation && this.context.isItemOpen()) this.context.updateRenderState({ downloadError: true })
    }
  }

  private resetCopy() {
    this.generation += 1
    if (this.copyTimer !== undefined) clearTimeout(this.copyTimer)
    this.copyTimer = undefined
    this.context.updateRenderState({ copyState: 'idle', downloadError: false })
  }

  update() { this.resetCopy() }
  suspend() { this.resetCopy() }
  dispose() { this.resetCopy() }
}

/** Uses the app's one Workbench host. No filesystem or resource resolver is involved. */
export function createToolHistoryFileDefinition(t: (key: string) => string, contentAccess?: ArtifactContentAccess): WorkbenchPanelDefinition {
  return {
    kind: 'tool-file-history',
    component: HistoricalToolFilePanel,
    supports: item => Boolean(toolHistoryFileFromItem(item)),
    getHeader: item => ({
      title: item.title,
      subtitle: t(`workbench.toolHistory.${toolHistoryFileFromItem(item)?.kind || 'read'}`),
      icon: 'fileCode',
    }),
    getProps: (item, state) => ({
      view: toolHistoryFileFromItem(item),
      wrapLines: state.runtimeState.wrapLines === true,
      copyState: state.runtimeState.copyState || 'idle',
      downloadError: state.runtimeState.downloadError === true,
    }),
    getToolbarItems: (item, state) => {
      const copyState = state.runtimeState.copyState
      const wrapLines = state.runtimeState.wrapLines === true
      const truncated = toolHistoryFileFromItem(item)?.truncated === true
      return [
        { kind: 'action', id: 'wrap', icon: 'fileText', pressed: wrapLines,
          label: t(wrapLines ? 'chat.toolModal.preserveLines' : 'chat.toolModal.wrapLines') },
        { kind: 'action', id: truncated ? 'download' : 'copy', icon: truncated ? (state.runtimeState.downloadError ? 'x' : 'download') : copyState === 'copied' ? 'check' : copyState === 'error' ? 'x' : 'copy',
          label: t(truncated ? (state.runtimeState.downloadError ? 'shared.runTrace.downloadContentFailed' : toolHistoryFileFromItem(item)?.partial ? 'chat.download' : 'shared.runTrace.downloadFullContent') : copyState === 'error' ? 'workspaceReference.copyFailed' : 'chat.copy') },
      ]
    },
    createRuntime: (_item, context) => new ToolHistoryFileRuntime(context, contentAccess),
  }
}
