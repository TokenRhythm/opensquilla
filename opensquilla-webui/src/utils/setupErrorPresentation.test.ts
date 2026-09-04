// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
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

  it('uses the localized save-failed prefix', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    expect(setupSaveFailedMessage(new Error('oops'))).toBe('保存失败: oops')
  })
})
