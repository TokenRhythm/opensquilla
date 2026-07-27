import { describe, it, expect } from 'vitest'
import { sectionFromRouteParam, isKnownSectionParam, parseProviderHash } from './useSettingsSection'
import { SETTINGS_SECTIONS } from './settingsSections'
import en from '@/locales/en.json'
import zhHans from '@/locales/zh-Hans.json'

describe('settings section IA', () => {
  it('has one Model Strategy section instead of split Router and Ensemble sections', () => {
    const ids = SETTINGS_SECTIONS.map(s => s.id)
    expect(ids).toContain('modelStrategy')
    expect(ids).not.toContain('router')
    expect(ids).not.toContain('ensemble')
    expect(ids.indexOf('provider')).toBeLessThan(ids.indexOf('modelStrategy'))
    expect(ids.indexOf('modelStrategy')).toBeLessThan(ids.indexOf('capabilities'))
  })

  it('keeps data maintenance as a nested route instead of a first-level rail section', () => {
    expect(SETTINGS_SECTIONS.map(s => s.id)).not.toContain('dataMigration')
    expect(sectionFromRouteParam('dataMigration')).toBe('dataMigration')
    expect(isKnownSectionParam('dataMigration')).toBe(true)
  })

  it('retires the obsolete approval-policy Safety section', () => {
    const ids = SETTINGS_SECTIONS.map(s => s.id)
    expect(ids).not.toContain('safety')
    expect(sectionFromRouteParam('safety')).toBe('provider')
    expect(isKnownSectionParam('safety')).toBe(false)
  })

  it('keeps Channels out of Settings while the router owns its legacy deep link', () => {
    expect(SETTINGS_SECTIONS.map(s => s.id)).not.toContain('channels')
    expect(sectionFromRouteParam('channels')).toBe('provider')
    expect(isKnownSectionParam('channels')).toBe(false)
  })

  it('does not ship copy for retired approval-policy destinations', () => {
    expect(en.settings.rail).not.toHaveProperty('safety')
    expect(en.settings).not.toHaveProperty('safety')
    expect(en.console).not.toHaveProperty('approvals')
    expect(en.nav).not.toHaveProperty('approvals')
  })

  it('passes through every canonical section id unchanged', () => {
    for (const s of SETTINGS_SECTIONS) {
      expect(sectionFromRouteParam(s.id)).toBe(s.id)
      expect(isKnownSectionParam(s.id)).toBe(true)
    }
  })

  it('has an English rail label for every canonical section id', () => {
    for (const s of SETTINGS_SECTIONS) {
      expect(en.settings.rail).toHaveProperty(s.id)
    }
  })

  it('labels the provider-backed Settings section as Model Service', () => {
    expect(en.settings.rail.provider).toBe('Model Service')
    expect(zhHans.settings.rail.provider).toBe('模型服务')
    expect(SETTINGS_SECTIONS.find(s => s.id === 'provider')?.label).toBe('Model Service')
  })

  it('aliases stale Router and Ensemble deep links to Model Strategy', () => {
    expect(sectionFromRouteParam('router')).toBe('modelStrategy')
    expect(sectionFromRouteParam('ensemble')).toBe('modelStrategy')
    expect(isKnownSectionParam('router')).toBe(true)
    expect(isKnownSectionParam('ensemble')).toBe(true)
  })

  it('aliases Chat Model deep links to the provider-backed section', () => {
    expect(sectionFromRouteParam('chatModel')).toBe('provider')
    expect(sectionFromRouteParam('provider')).toBe('provider')
    expect(isKnownSectionParam('chatModel')).toBe(true)
  })

  it('falls back to Provider for unknown, missing, and sentinel params', () => {
    expect(sectionFromRouteParam('auto')).toBe('provider')
    expect(sectionFromRouteParam('does-not-exist')).toBe('provider')
    expect(sectionFromRouteParam(undefined)).toBe('provider')
    expect(sectionFromRouteParam('')).toBe('provider')
    expect(sectionFromRouteParam(['provider'])).toBe('provider')
  })
})

describe('parseProviderHash', () => {
  it('extracts the provider id from #provider-<id> deep-link hashes', () => {
    expect(parseProviderHash('#provider-openrouter')).toBe('openrouter')
    expect(parseProviderHash('#provider-lm_studio')).toBe('lm_studio')
    // vue-router's route.hash always carries the '#', but tolerate a bare value.
    expect(parseProviderHash('provider-ollama')).toBe('ollama')
  })

  it('decodes URL-encoded provider ids', () => {
    expect(parseProviderHash('#provider-my%20provider')).toBe('my provider')
  })

  it('returns empty for other anchors and malformed values', () => {
    expect(parseProviderHash('')).toBe('')
    expect(parseProviderHash('#')).toBe('')
    expect(parseProviderHash('#providers-openai')).toBe('')
    expect(parseProviderHash('#provider-')).toBe('')
    expect(parseProviderHash('#other-anchor')).toBe('')
    expect(parseProviderHash(undefined)).toBe('')
    expect(parseProviderHash(42)).toBe('')
  })
})
