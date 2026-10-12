import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import runTraceSource from '@/components/run/RunTrace.vue?raw'
import textPartSource from './TextPart.vue?raw'
import narrationSource from '@/components/chat/ActivityNarration.vue?raw'

// Read the stylesheet directly: Vite raw CSS imports short-circuit in Vitest.
const chatMarkdownSource = readFileSync(
  new URL('../../../styles/chat-markdown.css', import.meta.url),
  'utf8',
)

describe('assistant code block surface', () => {
  it('keeps shared code chrome aligned with the theme roles', () => {
    expect(chatMarkdownSource).toContain('var(--code-block-bg)')
    expect(chatMarkdownSource).toContain('var(--code-block-border)')
    expect(chatMarkdownSource).toContain('var(--code-block-header-bg)')
  })

  it.each([
    ['answer text', textPartSource],
    ['run trace text', narrationSource],
  ])('keeps the %s code chrome on a single shared owner', (_name, source) => {
    expect(source).toContain('background: var(--code-block-bg);')
    expect(source).toContain('border: 1px solid var(--code-block-border);')
    expect(source).toContain('useMarkdownEnhancements(')
    expect(source).not.toContain('useMarkdownCodeCopy(')
    expect(source).not.toContain('useMarkdownDiagrams(')
    expect(source).not.toContain('.code-copy-btn {')
  })

  it('limits trace enhancements to narration without decorating tool detail code', () => {
    expect(runTraceSource).toContain('<ActivityNarration')
    expect(runTraceSource).toContain(':item="item"')
    expect(runTraceSource).not.toContain('useMarkdownEnhancements(')
  })
})
