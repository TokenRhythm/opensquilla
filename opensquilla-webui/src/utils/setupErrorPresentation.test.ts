// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import i18n, { loadLocaleMessages, SUPPORTED_LOCALES } from '@/i18n'
import { SetupWorkflowError } from '@/modules/setupWorkflow'
import {
  setupErrorMessage,
  setupSaveFailedMessage,
} from '@/utils/setupErrorPresentation'

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

describe('setup error presentation', () => {
  it('localizes a semantic provider validation failure', () => {
    const error = new SetupWorkflowError(
      'invalid',
      'model is required',
      'provider-invalid',
    )
    expect(setupErrorMessage(error)).toContain("Couldn't save the provider")
    expect(setupErrorMessage(error)).toContain('model is required')
  })

  it('falls back to the supplied domain message', () => {
    expect(setupErrorMessage(new Error('raw detail'))).toBe('raw detail')
  })

  it.each(SUPPORTED_LOCALES)('shows a safe ensemble validation message in %s', async locale => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    const original = new Error(
      '1 validation error for EnsembleConfig: Value error [input_value={synthetic: private-detail}]',
    )
    const error = new SetupWorkflowError('invalid', original.message, 'ensemble-invalid', original)
    const expected = i18n.global.t('errors.onboarding.ensemble')
    expect(expected).not.toBe('errors.onboarding.ensemble')
    expect(setupErrorMessage(error)).toBe(expected)
    expect(setupSaveFailedMessage(error)).toBe(`${i18n.global.t('errors.saveFailed')}: ${expected}`)
    expect(setupErrorMessage(error)).not.toMatch(/EnsembleConfig|input_value|private-detail/)
    expect(error.cause).toBe(original)
  })

  it.each(SUPPORTED_LOCALES)('renders the ensemble count limits in %s', async locale => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    expect(i18n.global.t('setup.modelStrategy.ensembleMinimum')).toContain('2')
    for (const count of [0, 1]) {
      const message = i18n.global.t('setup.modelStrategy.ensembleMinimumCount', { min: 2, count })
      expect(message).toContain('2')
      expect(message).toContain(String(count))
      expect(message).not.toMatch(/\{(?:min|count)\}/)
    }
    expect(i18n.global.t('setup.modelStrategy.ensembleMaximum', { max: 6 })).toContain('6')
    expect(i18n.global.t('setup.modelStrategy.ensembleAggregatorMaximum', { max: 1 })).toContain('1')
  })

  it.each(['forbidden', 'unavailable', 'invalid'] as const)(
    'keeps the original detail for other %s errors', code => {
      expect(setupErrorMessage(new SetupWorkflowError(code, 'Original detail'))).toBe('Original detail')
    },
  )

  it('uses the localized save-failed prefix', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    expect(setupSaveFailedMessage(new Error('oops'))).toBe('保存失败: oops')
  })
})
