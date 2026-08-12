import { describe, expect, it } from 'vitest'

import runTraceSource from '@/components/run/RunTrace.vue?raw'
import textPartSource from './TextPart.vue?raw'

describe('assistant code block surface', () => {
  it.each([
    ['answer text', textPartSource],
    ['run trace text', runTraceSource],
  ])('keeps the %s code chrome aligned with the shared roles', (_name, source) => {
    expect(source).toContain('background: var(--code-block-bg);')
    expect(source).toContain('border: 1px solid var(--code-block-border);')
    expect(source).toContain('padding-top: 2.375rem')
    expect(source).toContain('var(--code-block-header-bg) 1.75rem')
    expect(source).toContain('var(--code-block-bg) 100%')
    expect(source).toContain('line-height: 1rem')
    expect(source).toContain('background: transparent')
  })
})
