// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'

import { decorateCodeBlocks } from './codeBlockChrome'
import { closeImageLightbox, decorateMarkdownImages, openImageLightbox } from '@/utils/markdown/imageLightbox'

const labels = { copy: 'Copy', copied: 'Copied', copyFailed: 'Copy failed' }
const closeLabel = 'Close preview'

afterEach(() => {
  document.body.innerHTML = ''
  closeImageLightbox()
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

describe('markdown image lightbox', () => {
  it('decorates images and opens a dialog with the image source', () => {
    const root = document.createElement('div')
    root.innerHTML = '<img src="https://example.com/a.png" alt="diagram">'
    decorateMarkdownImages(root, closeLabel)
    const img = root.querySelector('img')
    expect(img?.classList.contains('md-img')).toBe(true)

    img?.click()
    const overlay = document.querySelector('.md-img-lightbox')
    expect(overlay).not.toBeNull()
    expect(overlay?.getAttribute('role')).toBe('dialog')
    const shown = overlay?.querySelector('img')
    expect(shown?.getAttribute('src')).toBe('https://example.com/a.png')
    expect(overlay?.querySelector('.md-img-lightbox__close')?.getAttribute('aria-label')).toBe(closeLabel)
  })

  it('closes on Escape and is idempotent across re-decoration', () => {
    const root = document.createElement('div')
    root.innerHTML = '<img src="https://example.com/a.png" alt="">'
    decorateMarkdownImages(root, closeLabel)
    decorateMarkdownImages(root, closeLabel)
    expect(root.querySelectorAll('img')).toHaveLength(1)

    openImageLightbox('https://example.com/a.png', 'alt text', closeLabel)
    expect(document.querySelector('.md-img-lightbox')).not.toBeNull()
    document.querySelector('.md-img-lightbox')?.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    )
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
  })

  it('replaces an open lightbox instead of stacking', () => {
    openImageLightbox('https://example.com/1.png', '', closeLabel)
    openImageLightbox('https://example.com/2.png', '', closeLabel)
    expect(document.querySelectorAll('.md-img-lightbox')).toHaveLength(1)
    const shown = document.querySelector('.md-img-lightbox img') as HTMLImageElement
    expect(shown.getAttribute('src')).toBe('https://example.com/2.png')
    closeImageLightbox()
    expect(document.querySelector('.md-img-lightbox')).toBeNull()
  })
})
