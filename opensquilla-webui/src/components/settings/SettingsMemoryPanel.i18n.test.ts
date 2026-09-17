import { describe, expect, it } from 'vitest'
import en from '@/locales/en.json'
import zhHans from '@/locales/zh-Hans.json'
import ja from '@/locales/ja.json'
import fr from '@/locales/fr.json'
import de from '@/locales/de.json'
import es from '@/locales/es.json'

const locales = { en, 'zh-Hans': zhHans, ja, fr, de, es }

describe('profile import locale contract', () => {
  it('ships a complete profile-export-v1 prompt in every supported locale', () => {
    for (const [code, messages] of Object.entries(locales)) {
      const prompt = messages.settings.memoryImport.exportPrompt
      // CJK expresses the same contract with substantially fewer characters
      // than the Latin-script locales, so this is a completeness floor rather
      // than an English-length proxy.
      expect(prompt.length, code).toBeGreaterThan(450)
      expect(prompt, code).toContain('[YYYY-MM-DD]')
      expect(prompt, code).toContain('[unknown]')
      const lines = prompt.trim().split('\n')
      expect(lines[lines.length - 1], code).toMatch(/^Imported from: <.+>$/)
      expect(messages.settings.rail.memory.length, code).toBeGreaterThan(0)
      expect(messages.settings.memoryImport.diffLineAdded.length, code).toBeGreaterThan(0)
      expect(messages.settings.memoryImport.diffLineRemoved.length, code).toBeGreaterThan(0)
      for (const key of [
        'stepsLabel',
        'sourceStepTitle',
        'sourceStepDescription',
        'pasteStepTitle',
        'pasteStepDescription',
      ] as const) {
        expect(messages.settings.memoryImport[key].length, `${code}:${key}`).toBeGreaterThan(0)
      }
    }
  })

  it('asks the source assistant for every merge category and evidence', () => {
    const prompt = en.settings.memoryImport.exportPrompt
    for (const category of [
      'Instructions',
      'Identity',
      'Profession',
      'Projects',
      'Preferences',
      'Relationships',
      'Dated events and plans',
    ]) {
      expect(prompt).toContain(category)
    }
    expect(prompt).toContain('Evidence:')
    expect(prompt).toContain('Do not infer')
    expect(prompt).toContain('Exclude passwords')
    expect(prompt).toContain('available past conversations')
    expect(prompt).toContain('current conversation context')
    expect(prompt).toContain('skip unavailable sources instead of guessing')
    expect(prompt).toContain('this export request or its formatting instructions')
    expect(prompt).toContain('system/developer/app instructions')
    expect(prompt).toContain('already present in saved or long-term memory')
    expect(prompt).toContain('even when a section is empty')
    expect(prompt).toContain('stored memory; exact wording unavailable')
  })
})
