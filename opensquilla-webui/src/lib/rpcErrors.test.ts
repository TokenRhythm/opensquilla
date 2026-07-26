// @vitest-environment happy-dom
import { describe, it, expect, beforeEach } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
import { localizeRpcError, saveFailedMessage } from '@/lib/rpcErrors'

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

function rpcErr(code: string | undefined, message = 'boom'): Error {
  const e = new Error(message) as Error & { code?: string }
  if (code) e.code = code
  return e
}

describe('localizeRpcError', () => {
  it('maps a known stable code to a localized lead plus the English detail', () => {
    const out = localizeRpcError(rpcErr('onboarding.provider.invalid', 'model is required'))
    expect(out).toContain("Couldn't save the provider")
    expect(out).toContain('model is required')
  })

  it('falls back to the raw message for an unknown or missing code', () => {
    expect(localizeRpcError(rpcErr(undefined, 'raw detail'))).toBe('raw detail')
    expect(localizeRpcError(rpcErr('some.unmapped.code', 'raw detail'))).toBe('raw detail')
  })

  it('localizes the lead in zh-Hans', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    expect(localizeRpcError(rpcErr('onboarding.channel.not_found', 'gone'))).toContain('该渠道已不存在')
  })
})

describe('saveFailedMessage', () => {
  it('prefixes the localized save-failed label', () => {
    expect(saveFailedMessage(rpcErr(undefined, 'oops'))).toBe('Save failed: oops')
  })

  it('uses the localized prefix in zh-Hans', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    expect(saveFailedMessage(rpcErr(undefined, 'oops'))).toBe('保存失败: oops')
  })
})
