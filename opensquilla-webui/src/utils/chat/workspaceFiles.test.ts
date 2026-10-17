// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { clearWorkspaceFileLinks, decorateWorkspaceFileLinks, workspaceFileCandidates, workspaceFilePath } from './workspaceFiles'

describe('workspace file references', () => {
  it('keeps relative Markdown paths inert until a session resolver approves them', () => {
    const root = document.createElement('div')
    root.innerHTML = useChatTextRendering().renderMarkdown('[打开游戏](flappy-bird.html)')
    expect(root.querySelector('a')?.textContent).toBe('打开游戏')
    expect(root.querySelector('a')?.hasAttribute('href')).toBe(false)
    expect(root.querySelector('a')?.hasAttribute('role')).toBe(false)
    expect(workspaceFileCandidates(root)).toEqual(['flappy-bird.html'])
    expect(root.querySelector('button')).toBeNull()
  })

  it('finds complete inline and markdown paths, including Chinese and spaces in tables', () => {
    const root = document.createElement('div')
    root.innerHTML = useChatTextRendering().renderMarkdown([
      '| Output | File |', '| --- | --- |', '| Image | `outputs/中文 图.svg` |',
      '', '[PNG](<C:/task/中文 图.png>)', '', 'Plain: missing.pdf', '',
      '```text', 'secrets.txt', '```', '', '`https://example.com/report.pdf`',
    ].join('\n'))
    expect(workspaceFileCandidates(root)).toEqual(['outputs/中文 图.svg', 'C:/task/中文 图.png'])
    expect(root.querySelector('a')?.hasAttribute('href')).toBe(false)
  })

  it.each(['https://example.com/a.svg', 'file:///tmp/a.svg', 'javascript:alert.svg',
    'data:text/html,evil.html', '//remote/a.svg', '\\\\remote\\a.svg', 'one.svg\ntwo.svg',
    'report.pdf#page=2', 'report.pdf?download=1', 'bad"><img src=x>.html'])('rejects nonlocal candidate %s', path => {
    expect(workspaceFilePath(path)).toBeNull()
  })

  it('only decorates resolved paths, restores originals, and does not trust forged buttons', () => {
    const root = document.createElement('div')
    root.innerHTML = '<code>result.svg</code><code>missing.svg</code><button class="workspace-file-link">forged.svg</button>'
    const file = { requestedPath: 'result.svg', path: 'result.svg', name: 'result.svg', mime: 'image/svg+xml', size: 10,
      kind: 'text' as const, workspaceBinding: 'binding' }
    const activate = vi.fn()
    decorateWorkspaceFileLinks(root, [file], activate, file => file.name)
    decorateWorkspaceFileLinks(root, [file], activate, file => file.name)
    root.querySelector<HTMLButtonElement>('button')!.click()
    expect(activate).toHaveBeenCalledExactlyOnceWith(file)
    expect(root.querySelectorAll('button')).toHaveLength(2)
    clearWorkspaceFileLinks(root)
    expect(root.querySelectorAll('code')).toHaveLength(2)
    expect(root.querySelector('button')?.textContent).toBe('forged.svg')
  })
})
