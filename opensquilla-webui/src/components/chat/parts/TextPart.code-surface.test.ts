import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import runTraceSource from '@/components/run/RunTrace.vue?raw'
import textPartSource from './TextPart.vue?raw'

// Vite's ?raw import short-circuits on CSS files under vitest, so read the
// stylesheet from disk directly.
const chatMarkdownSource = readFileSync(
  new URL('../../../styles/chat-markdown.css', import.meta.url),
  'utf8',
)

describe('assistant code block surface', () => {
  // The shared chrome (header gradient, language label, copy button) moved to
  // the global stylesheet so streaming blocks and artifact previews render the
  // identical surface; this locks the token alignment there.
  it('keeps the shared code chrome aligned with the roles', () => {
    expect(chatMarkdownSource).toContain('background: var(--code-block-bg);')
    expect(chatMarkdownSource).toContain('border: 1px solid var(--code-block-border);')
    expect(chatMarkdownSource).toContain('padding-top: 2.375rem')
    expect(chatMarkdownSource).toContain('var(--code-block-header-bg) 1.75rem')
    expect(chatMarkdownSource).toContain('var(--code-block-bg) 100%')
    expect(chatMarkdownSource).toContain('line-height: 1rem')
    expect(chatMarkdownSource).toContain('background: transparent')
  })

  it('keeps the answer text on the shared chrome pass', () => {
    expect(textPartSource).toContain('decorateCodeBlocks')
    expect(textPartSource).not.toContain('.code-copy-btn {')
  })

  it('keeps the run trace text code chrome aligned with the shared roles', () => {
    expect(runTraceSource).toContain('background: var(--code-block-bg);')
    expect(runTraceSource).toContain('border: 1px solid var(--code-block-border);')
    expect(runTraceSource).toContain('padding-top: 2.375rem')
    expect(runTraceSource).toContain('var(--code-block-header-bg) 1.75rem')
    expect(runTraceSource).toContain('var(--code-block-bg) 100%')
    expect(runTraceSource).toContain('line-height: 1rem')
    expect(runTraceSource).toContain('background: transparent')
  })
})
