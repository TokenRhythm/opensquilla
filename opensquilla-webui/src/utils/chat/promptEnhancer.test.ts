// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'

import { enhancePrompt } from './promptEnhancer'

describe('enhancePrompt', () => {
  it('returns a no-op result for empty or whitespace input', () => {
    const empty = enhancePrompt('   \n  ')
    expect(empty.enhanced).toBe(false)
    expect(empty.text).toBe('')

    const blank = enhancePrompt('')
    expect(blank.enhanced).toBe(false)
    expect(blank.text).toBe('')
  })

  it('produces a structured prompt for a plain Chinese task', () => {
    const result = enhancePrompt('帮我写一个 Python 函数读取 CSV')
    expect(result.enhanced).toBe(true)
    expect(result.text).toContain('资深软件工程师')
    expect(result.text).toContain('读取 CSV')
    expect(result.text).toContain('输出要求')
    expect(result.dimensions).toContain('角色定义')
    expect(result.dimensions).toContain('任务目标')
  })

  it('produces English scaffolding when locale is en', () => {
    const result = enhancePrompt('Write a python function to read a csv', { locale: 'en' })
    expect(result.enhanced).toBe(true)
    expect(result.text).toContain('Act as')
    expect(result.text).toContain('Output requirements')
    expect(result.text).toContain('read a csv')
  })

  it('strips a leading imperative so the body reads as a clean objective', () => {
    const result = enhancePrompt('帮我 总结这篇文章的要点')
    expect(result.enhanced).toBe(true)
    expect(result.text).toContain('总结这篇文章的要点')
    // The leading "帮我" should be dropped from the body line.
    expect(result.text).not.toContain('帮我\n')
    expect(result.text).not.toMatch(/请完成以下任务：\n帮我/)
  })

  it('keeps question-like input readable', () => {
    const result = enhancePrompt('如何优化 SQL 查询性能？')
    expect(result.enhanced).toBe(true)
    expect(result.text).toContain('如何优化 SQL 查询性能')
    expect(result.text).toContain('输出要求')
  })

  it('strips a trailing full stop before adding scaffolding', () => {
    const result = enhancePrompt('总结这篇文章的要点。')
    expect(result.enhanced).toBe(true)
    expect(result.text).toContain('总结这篇文章的要点')
    // No sentence-period directly before the closing quote/line.
    expect(result.text).not.toMatch(/要点。\n\n/)
  })

  it('never injects an unsolicited role when the input is generic', () => {
    const result = enhancePrompt('帮我看看这个链接', { locale: 'zh' })
    expect(result.enhanced).toBe(true)
    expect(result.dimensions).not.toContain('角色定义')
  })

  it('is deterministic for the same input', () => {
    const a = enhancePrompt('写一个抽奖程序', { locale: 'zh' })
    const b = enhancePrompt('写一个抽奖程序', { locale: 'zh' })
    expect(a.text).toBe(b.text)
  })
})
