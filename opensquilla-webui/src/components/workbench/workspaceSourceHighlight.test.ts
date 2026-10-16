// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { highlightWorkspaceLines } from './workspaceSourceHighlight'

describe('workspace source highlighting', () => {
  it('preserves multiline token spans and exact source text without executable markup', () => {
    const lines = ['/* first', 'second */ const value = "<img src=x onerror=alert(1)>";']
    const html = highlightWorkspaceLines('example.js', lines)
    html.forEach((line, index) => {
      const element = document.createElement('div')
      element.innerHTML = line
      expect(element.textContent).toBe(lines[index])
      expect(element.querySelector('img')).toBeNull()
      expect(element.querySelector('.hljs-comment')).not.toBeNull()
    })
  })
  it('uses escaped plain text for large pages and unknown formats', () => {
    expect(highlightWorkspaceLines('unknown.bin', ['<script>'])).toEqual(['&lt;script&gt;'])
    expect(highlightWorkspaceLines('large.json', ['x'.repeat(30_001), '<script>'])[1]).toBe('&lt;script&gt;')
  })
})
