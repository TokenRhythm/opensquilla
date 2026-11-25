import { describe, expect, it } from 'vitest'
import { mentionMatchRank, mentionQueryAt, mentionTarget, replaceMentionQuery } from './mentionQuery'

describe('mention query detection', () => {
  it('returns no token for empty text, plain prose and e-mail addresses', () => {
    expect(mentionQueryAt('')).toBeNull()
    expect(mentionQueryAt('hello world')).toBeNull()
    expect(mentionQueryAt('a@b.com')).toBeNull()
    expect(mentionQueryAt('foo@bar')).toBeNull()
  })

  it('opens an empty query right after an @ at the start of the line', () => {
    expect(mentionQueryAt('@')).toEqual({ start: 0, end: 1, query: '' })
  })

  it('captures the typed path up to the caret', () => {
    expect(mentionQueryAt('hello @src/ma')).toEqual({ start: 6, end: 13, query: 'src/ma' })
  })

  it('stops the token at the caret instead of the end of the line', () => {
    expect(mentionQueryAt('hello @src/main.ts', 10)).toEqual({ start: 6, end: 10, query: 'src' })
    expect(mentionQueryAt('hello @src/main.ts', 0)).toBeNull()
  })

  it('clamps a caret past the end of the text', () => {
    expect(mentionQueryAt('@src', 99)).toEqual({ start: 0, end: 4, query: 'src' })
  })

  it('closes the token once whitespace or a newline follows the @', () => {
    expect(mentionQueryAt('@src ma')).toBeNull()
    expect(mentionQueryAt('@src\nma')).toBeNull()
    expect(mentionQueryAt('@src\tma')).toBeNull()
    expect(mentionQueryAt('@src ma', 4)).toEqual({ start: 0, end: 4, query: 'src' })
  })

  it('accepts an @ that follows whitespace or Chinese punctuation', () => {
    expect(mentionQueryAt('use @src/ma')).toEqual({ start: 4, end: 11, query: 'src/ma' })
    expect(mentionQueryAt('(@main')).toEqual({ start: 1, end: 6, query: 'main' })
    expect(mentionQueryAt('请查看，@src/ma')).toEqual({ start: 4, end: 11, query: 'src/ma' })
    expect(mentionQueryAt('「@main')).toEqual({ start: 1, end: 6, query: 'main' })
  })

  it('rejects a token glued to a preceding letter or digit', () => {
    expect(mentionQueryAt('x@main')).toBeNull()
    expect(mentionQueryAt('9@main')).toBeNull()
  })

  it('rejects control characters and over-long queries', () => {
    expect(mentionQueryAt('@sr\u0001c')).toBeNull()
    expect(mentionQueryAt('@' + 'a'.repeat(201))).toBeNull()
    expect(mentionQueryAt('@' + 'a'.repeat(200))).toEqual({ start: 0, end: 201, query: 'a'.repeat(200) })
  })
})

describe('mention targets', () => {
  it('splits a nested path into directory and fragment', () => {
    expect(mentionTarget('src/components')).toEqual({ directory: 'src', fragment: 'components' })
    expect(mentionTarget('src/components/App')).toEqual({ directory: 'src/components', fragment: 'App' })
  })

  it('keeps a bare name as a root-level fragment', () => {
    expect(mentionTarget('main')).toEqual({ directory: '', fragment: 'main' })
    expect(mentionTarget('')).toEqual({ directory: '', fragment: '' })
  })

  it('normalizes Windows separators', () => {
    expect(mentionTarget('src\\main')).toEqual({ directory: 'src', fragment: 'main' })
    expect(mentionTarget('src\\components\\App')).toEqual({ directory: 'src/components', fragment: 'App' })
  })

  it('treats a trailing separator as an empty fragment', () => {
    expect(mentionTarget('src/')).toEqual({ directory: 'src', fragment: '' })
    expect(mentionTarget('src\\')).toEqual({ directory: 'src', fragment: '' })
  })
})

describe('mention match ranking', () => {
  it('ranks exact, prefix, substring and misses in order', () => {
    expect(mentionMatchRank('main.ts', '')).toBe(0)
    expect(mentionMatchRank('main.ts', '   ')).toBe(0)
    expect(mentionMatchRank('main.ts', 'main.ts')).toBe(0)
    expect(mentionMatchRank('main.ts', 'main')).toBe(1)
    expect(mentionMatchRank('spreadsheets', 'spread')).toBe(1)
    expect(mentionMatchRank('src/main.ts', 'main')).toBe(2)
    expect(mentionMatchRank('src/components/main.ts', 'main')).toBe(2)
    expect(mentionMatchRank('main.ts', 'zzz')).toBe(-1)
  })

  it('ignores case on both sides of the comparison', () => {
    expect(mentionMatchRank('Main.TS', 'main.ts')).toBe(0)
    expect(mentionMatchRank('README.md', 'read')).toBe(1)
    expect(mentionMatchRank('src/Readme.md', 'README')).toBe(2)
    expect(mentionMatchRank('src/components/README.md', 'readme')).toBe(2)
  })
})

describe('mention replacement', () => {
  it('swaps the token for the resolved text and reports the caret after it', () => {
    const text = 'hello @src/ma world'
    const range = mentionQueryAt(text, 13)!
    const result = replaceMentionQuery(text, range, '@src/main.ts')
    expect(result.text).toBe('hello @src/main.ts world')
    expect(result.caret).toBe(6 + '@src/main.ts'.length)
    expect(result.text.slice(result.caret)).toBe(' world')
  })

  it('preserves text typed after the token when the caret sits mid-draft', () => {
    const text = '请 @main 并总结'
    const range = mentionQueryAt(text, 7)!
    expect(range.query).toBe('main')
    expect(replaceMentionQuery(text, range, '@README.md')).toEqual({
      text: '请 @README.md 并总结',
      caret: 2 + '@README.md'.length,
    })
  })

  it('replaces an empty query with the insertion text', () => {
    const result = replaceMentionQuery('@', { start: 0, end: 1, query: '' }, '@docs/')
    expect(result).toEqual({ text: '@docs/', caret: 6 })
  })
})
