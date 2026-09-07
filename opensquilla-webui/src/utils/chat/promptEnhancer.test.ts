// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'

import { detectTaskType, enhancePrompt } from './promptEnhancer'

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

describe('detectTaskType', () => {
  it('detects analyze', () => {
    expect(detectTaskType('分析一下这个月的销售数据为什么会下降')).toBe('analyze')
    expect(detectTaskType('总结这篇文章的要点')).toBe('analyze')
  })

  it('detects design', () => {
    expect(detectTaskType('帮我设计一个微服务架构方案')).toBe('design')
    expect(detectTaskType('规划一个数据平台的迁移路线')).toBe('design')
  })

  it('detects implement', () => {
    expect(detectTaskType('写一个 Python 函数读取 CSV')).toBe('implement')
    expect(detectTaskType('帮我实现一个登录接口')).toBe('implement')
  })

  it('detects review', () => {
    expect(detectTaskType('审查一下这个代码有没有问题')).toBe('review')
    expect(detectTaskType('帮我 review 这个 PR')).toBe('review')
  })

  it('detects write', () => {
    expect(detectTaskType('写一份项目周报')).toBe('write')
    expect(detectTaskType('帮我起草一封邮件')).toBe('write')
  })

  it('detects translate', () => {
    expect(detectTaskType('把这段话翻译成英文')).toBe('translate')
    expect(detectTaskType('translate this into Japanese')).toBe('translate')
  })

  it('detects query', () => {
    expect(detectTaskType('查一下今天北京天气')).toBe('query')
    expect(detectTaskType('帮我看看这个链接')).toBe('query')
    expect(detectTaskType('什么是 MCP 协议？')).toBe('query')
  })

  it('detects orchestrate', () => {
    expect(detectTaskType('安排一下这个项目的推进计划')).toBe('orchestrate')
    expect(detectTaskType('帮我跟进这个任务的进度')).toBe('orchestrate')
  })
})

describe('enhancePrompt type-specific scaffolding', () => {
  it('injects review deliverables for a review task', () => {
    const r = enhancePrompt('审查这个代码有没有问题', { locale: 'zh' })
    expect(r.dimensions).toContain('类型-审查')
    expect(r.text).toContain('问题清单')
    expect(r.text).toContain('修复建议')
  })

  it('injects design deliverables for a design task', () => {
    const r = enhancePrompt('设计一个微服务架构', { locale: 'zh' })
    expect(r.dimensions).toContain('类型-设计')
    expect(r.text).toContain('候选方案')
    expect(r.text).toContain('备选退路')
  })

  it('injects translate deliverables and terminology guard', () => {
    const r = enhancePrompt('把这段话翻译成英文', { locale: 'zh' })
    expect(r.dimensions).toContain('类型-翻译')
    expect(r.text).toContain('术语')
    expect(r.text).toContain('不要逐字硬翻')
  })

  it('injects query deliverables with a source requirement', () => {
    const r = enhancePrompt('查一下今天北京天气', { locale: 'zh' })
    expect(r.dimensions).toContain('类型-查询')
    expect(r.text).toContain('来源')
    expect(r.text).toContain('不要编造来源')
  })

  it('injects English type scaffold when locale is en', () => {
    const r = enhancePrompt('Review this code', { locale: 'en' })
    expect(r.dimensions).toContain('type-review')
    expect(r.text).toContain('severity')
    expect(r.text).toContain('fix')
  })
})

