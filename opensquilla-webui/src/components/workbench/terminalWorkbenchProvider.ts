import { normalizeTerminalTarget, type TerminalAccess } from '@/modules/terminal'
import type { WorkbenchItem, WorkbenchPanelDefinition } from '@/workbench/types'
import TerminalPanel from './TerminalPanel.vue'

export function createTerminalWorkbenchDefinition(
  terminal: TerminalAccess | null,
  t: (key: string) => string,
): WorkbenchPanelDefinition {
  return {
    kind: 'terminal',
    component: TerminalPanel,
    supports: item => item.kind === 'terminal' && item.scope.type === 'session' && Boolean(terminal),
    getHeader: () => ({ title: t('workbench.terminal.title'), subtitle: t('workbench.terminal.subtitle'), icon: 'terminal' }),
    getProps: (item: WorkbenchItem) => {
      const target = normalizeTerminalTarget(item.payload.target)
      return target ? { target } : { sessionKey: item.scope.type === 'session' ? item.scope.id : '' }
    },
  }
}
