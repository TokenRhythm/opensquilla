import { describe, expect, it } from 'vitest'
import { localizedSkillDescription } from './useSkillsCatalog'

describe('localizedSkillDescription', () => {
  const skill = { description: 'English summary', description_zh: '中文摘要' }

  it('returns the Chinese description under a zh locale', () => {
    expect(localizedSkillDescription(skill, 'zh-Hans')).toBe('中文摘要')
    expect(localizedSkillDescription(skill, 'zh')).toBe('中文摘要')
  })

  it('returns the English description under non-zh locales', () => {
    expect(localizedSkillDescription(skill, 'en')).toBe('English summary')
    expect(localizedSkillDescription(skill, 'ja')).toBe('English summary')
  })

  it('falls back to English when no Chinese variant exists, even under zh', () => {
    expect(localizedSkillDescription({ description: 'Only English' }, 'zh-Hans')).toBe('Only English')
  })

  it('returns an empty string when nothing is available', () => {
    expect(localizedSkillDescription({}, 'zh-Hans')).toBe('')
    expect(localizedSkillDescription({}, 'en')).toBe('')
  })
})
