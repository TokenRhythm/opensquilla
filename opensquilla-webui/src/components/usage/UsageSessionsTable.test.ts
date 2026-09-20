// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import UsageSessionsTable from './UsageSessionsTable.vue'

const apps: ReturnType<typeof createApp>[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})
describe('UsageSessionsTable sorting controls', () => {
  it('exposes native buttons and the current sort direction only on sortable columns', async () => {
    const sort = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(UsageSessionsTable, {
      tableColumns: [{ key: 'session', label: 'Task' }, { key: 'cost', label: 'Cost' }],
      sortableCols: ['cost'], sortCol: 'cost', sortAsc: false,
      sortedRows: [], sessionsMeta: '', expandedSessions: new Set<string>(),
      fmtCost: () => '$0', costSourceLabel: () => '', costSourceTooltip: () => '',
      costSourceClasses: () => ({}), costSourceClassesForBreakdown: () => ({}),
      costSourceLabelForBreakdown: () => '', costSourceTooltipForBreakdown: () => '',
      modelDisplayLabel: () => '', rowKey: () => '', rowBreakdown: () => [],
      rowBreakdownTotalTokens: () => 0, rowBreakdownTotalCost: () => 0,
      rowBreakdownAnyProrated: () => false, onSort: sort,
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)
    await nextTick()
    const headers = host.querySelectorAll('th')
    expect(headers[0]?.hasAttribute('aria-sort')).toBe(false)
    expect(headers[0]?.querySelector('button')).toBeNull()
    expect(headers[1]?.getAttribute('aria-sort')).toBe('descending')
    const button = headers[1]!.querySelector<HTMLButtonElement>('button')!
    button.focus()
    expect(document.activeElement).toBe(button)
    button.click()
    expect(sort).toHaveBeenCalledExactlyOnceWith('cost')
  })
})
