// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import HistoricalToolFilePanel from './HistoricalToolFilePanel.vue'
import type { ToolHistoryFileView } from '@/workbench/toolHistoryFileItems'

const cleanups: Array<() => void> = []
afterEach(() => cleanups.splice(0).forEach(cleanup => cleanup()))

async function mount(view: ToolHistoryFileView) {
  const root = document.createElement('div')
  const app = createApp({ render: () => h(HistoricalToolFilePanel, { view }) })
  app.use(i18n).mount(root)
  cleanups.push(() => app.unmount())
  await nextTick()
  return root
}

describe('recorded file full view', () => {
  it('keeps the workbench bounded for an oversized historical file', async () => {
    const root = await mount({ kind: 'write', path: 'bundle.js', content: 'x'.repeat(2_000_000) })
    expect(root.querySelector('code')!.textContent!.length).toBe(8192)
    expect(root.querySelector('[data-testid="tool-content-truncated"]')).not.toBeNull()
    expect(root.querySelector('button')).toBeNull()
  })
  it('renders the complete source as safe text without a second details toolbar or disclosure', async () => {
    const content = '<script>window.historySideEffect = true</script>\n' + 'retained source\n'.repeat(500) + 'END_OF_RECORD'
    const root = await mount({ kind: 'write', path: 'index.html', language: 'html', content })
    expect(root.querySelector('script')).toBeNull()
    expect(root.textContent).toContain('<script>window.historySideEffect = true</script>')
    expect(root.textContent).toContain('END_OF_RECORD')
    expect(root.querySelector('details, button')).toBeNull()
    expect(root.querySelector('.activity-code-details--full')).not.toBeNull()
  })

  it('keeps recorded diff text intact without synthesizing file content', async () => {
    const content = '--- before\n+++ after\n@@\n-<old>\n+<new>\n'
    const root = await mount({ kind: 'edit', path: 'src/a.ts', content })
    expect(root.querySelector('pre')?.textContent).toBe(content)
    expect(root.querySelector('old, new')).toBeNull()
  })
})
