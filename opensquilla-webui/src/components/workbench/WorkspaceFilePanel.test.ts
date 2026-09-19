// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import type { WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'

describe('workspace source panel', () => {
  it('renders source as text and highlights the exact inclusive line range', async () => {
    const el = document.createElement('div')
    const snapshot = {
      relativePath: 'src/sample.py', content: 'one\n<script>alert(1)</script>\nthree\nfour\n',
      startLine: 2, endLine: 3, totalLines: 4,
    } as WorkspaceSourceSnapshot
    const app = createApp(WorkspaceFilePanel, { snapshot })
    app.use(i18n).mount(el)
    await nextTick()
    expect(el.querySelector('script')).toBeNull()
    expect(el.textContent).toContain('<script>alert(1)</script>')
    expect([...el.querySelectorAll('.is-selected')].map(node => node.getAttribute('data-line'))).toEqual(['2', '3'])
    expect(el.querySelectorAll('[data-line]')).toHaveLength(4)
    expect(el.querySelector('pre')?.getAttribute('aria-label')).toBe('src/sample.py')
    app.unmount()
  })
  it('matches Unicode line boundaries and bounds the number of rendered lines', async () => {
    const el = document.createElement('div')
    const snapshot = {
      relativePath: 'a.txt', content: 'one\u2028two\u0085' + 'line\n'.repeat(1000),
      startLine: 2, endLine: 1002, totalLines: 1002,
    } as WorkspaceSourceSnapshot
    const app = createApp(WorkspaceFilePanel, { snapshot })
    app.use(i18n).mount(el)
    await nextTick()
    expect(el.querySelector('[data-line="2"]')?.textContent).toContain('two')
    expect(el.querySelectorAll('[data-line]')).toHaveLength(200)
    expect(el.querySelector('.workspace-file__more')).not.toBeNull()
    app.unmount()
  })
})
