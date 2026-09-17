import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('RunTrace activity narration spacing', () => {
  it('keeps adjacent narration blocks visually separated', () => {
    const source = readFileSync('src/components/run/RunTrace.vue', 'utf8')

    expect(source).toContain('.tool-timeline--activity .msg-ai-text + .msg-ai-text')
    expect(source).not.toContain('margin-top: -0.125rem')
  })
})
