// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { decorateCodeBlocks } from './codeBlockChrome'

const labels = { copy: 'Copy', copied: 'Copied', copyFailed: 'Copy failed' }

afterEach(() => {
  document.body.innerHTML = ''
})

describe('decorateCodeBlocks', () => {
  it('adds the code-block chrome and one copy button per pre', () => {
    const root = document.createElement('div')
    root.innerHTML = '<pre><code class="hljs language-ts">const a = 1</code></pre>'
    decorateCodeBlocks(root, labels)
    const pre = root.querySelector('pre')
    expect(pre?.classList.contains('code-block')).toBe(true)
    expect(root.querySelectorAll('.code-copy-btn')).toHaveLength(1)
    const button = root.querySelector('.code-copy-btn') as HTMLButtonElement
    expect(button.getAttribute('aria-label')).toBe('Copy')
  })

  it('is idempotent across re-runs', () => {
    const root = document.createElement('div')
    root.innerHTML = '<pre><code>hello</code></pre>'
    decorateCodeBlocks(root, labels)
    decorateCodeBlocks(root, labels)
    expect(root.querySelectorAll('.code-copy-btn')).toHaveLength(1)
  })

  it('copies the code text on click', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const root = document.createElement('div')
    root.innerHTML = '<pre><code class="hljs language-py">print(1)</code></pre>'
    decorateCodeBlocks(root, labels)
    const button = root.querySelector('.code-copy-btn') as HTMLButtonElement
    button.click()
    await vi.waitFor(() => {
      expect(writeText).toHaveBeenCalledWith('print(1)')
      expect(button.getAttribute('aria-label')).toBe('Copied')
    })
  })
})
