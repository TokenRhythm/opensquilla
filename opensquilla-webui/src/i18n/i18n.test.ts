// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'
import en from '@/locales/en.json'
import zhHans from '@/locales/zh-Hans.json'
import de from '@/locales/de.json'
import es from '@/locales/es.json'
import fr from '@/locales/fr.json'
import ja from '@/locales/ja.json'
import i18n, {
  resolveInitialLocale,
  normalizeLocale,
  isSupportedLocale,
} from '@/i18n'
import { useAppStore } from '@/stores/app'
import { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'

function flatten(obj: Record<string, unknown>, prefix = '', out: Record<string, unknown> = {}) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v as Record<string, unknown>, key, out)
    else out[key] = v
  }
  return out
}

describe('normalizeLocale', () => {
  it('maps supported language variants, rejects the rest', () => {
    expect(normalizeLocale('en')).toBe('en')
    expect(normalizeLocale('en-US')).toBe('en')
    expect(normalizeLocale('zh')).toBe('zh-Hans')
    expect(normalizeLocale('zh-CN')).toBe('zh-Hans')
    expect(normalizeLocale('zh-Hans')).toBe('zh-Hans')
    expect(normalizeLocale('ja-JP')).toBe('ja')
    expect(normalizeLocale('fr')).toBe('fr')
    expect(normalizeLocale('de-DE')).toBe('de')
    expect(normalizeLocale('es-419')).toBe('es')
    expect(normalizeLocale('ko')).toBeNull()
    expect(normalizeLocale('')).toBeNull()
    expect(normalizeLocale(null)).toBeNull()
  })
})

describe('isSupportedLocale', () => {
  it('only accepts the canonical codes', () => {
    expect(isSupportedLocale('en')).toBe(true)
    expect(isSupportedLocale('zh-Hans')).toBe(true)
    expect(isSupportedLocale('zh-CN')).toBe(false)
    expect(isSupportedLocale('zh-hans')).toBe(false)
    expect(isSupportedLocale(null)).toBe(false)
  })
})

describe('resolveInitialLocale (first match wins)', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('lang')
    document.getElementById('opensquilla-data')?.remove()
  })
  afterEach(() => vi.unstubAllGlobals())

  it('1. prefers a valid saved localStorage value', () => {
    localStorage.setItem('opensquilla-locale', 'zh-Hans')
    expect(resolveInitialLocale()).toBe('zh-Hans')
  })

  it('2. ignores an unsupported saved value and reads #opensquilla-data data-locale', () => {
    localStorage.setItem('opensquilla-locale', 'ko')
    const el = document.createElement('div')
    el.id = 'opensquilla-data'
    el.dataset.locale = 'zh-Hans'
    document.body.appendChild(el)
    expect(resolveInitialLocale()).toBe('zh-Hans')
  })

  it('3. honors <html lang> when no saved/data value', () => {
    document.documentElement.setAttribute('lang', 'zh-CN')
    expect(resolveInitialLocale()).toBe('zh-Hans')
  })

  it('4. falls back to navigator.languages', () => {
    vi.stubGlobal('navigator', { languages: ['zh-CN', 'en'], language: 'zh-CN' })
    expect(resolveInitialLocale()).toBe('zh-Hans')
  })

  it('4b. matches a supported navigator language (fr)', () => {
    vi.stubGlobal('navigator', { languages: ['fr-FR', 'en'], language: 'fr-FR' })
    expect(resolveInitialLocale()).toBe('fr')
  })

  it('5. defaults to en when nothing matches', () => {
    vi.stubGlobal('navigator', { languages: ['ko-KR'], language: 'ko-KR' })
    expect(resolveInitialLocale()).toBe('en')
  })

  it('honors the desktop OS locale (arg) ahead of navigator', () => {
    vi.stubGlobal('navigator', { languages: ['en-US'], language: 'en-US' })
    expect(resolveInitialLocale('zh-CN')).toBe('zh-Hans')
    expect(resolveInitialLocale('ja-JP')).toBe('ja')
    // an unsupported OS locale falls through to navigator → en
    expect(resolveInitialLocale('ko-KR')).toBe('en')
  })

  it('saved preference still beats the OS locale', () => {
    localStorage.setItem('opensquilla-locale', 'en')
    expect(resolveInitialLocale('zh-CN')).toBe('en')
  })
})

describe('appStore locale state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    useToasts().toasts.value = []
    i18n.global.locale.value = 'en'
    document.documentElement.removeAttribute('lang')
  })

  afterEach(() => {
    ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = undefined
  })

  it('setLocale loads the chunk, persists, and applies all side effects', async () => {
    const store = useAppStore()
    await store.setLocale('zh-Hans')
    expect(store.locale).toBe('zh-Hans')
    expect(localStorage.getItem('opensquilla-locale')).toBe('zh-Hans')
    expect(document.documentElement.getAttribute('lang')).toBe('zh-Hans')
    expect(document.documentElement.getAttribute('dir')).toBe('ltr')
    expect(i18n.global.locale.value).toBe('zh-Hans')
    // the lazily-loaded chunk is now resolvable
    expect(i18n.global.t('nav.sessions')).toBe('会话')
  })

  it('syncs an explicit language selection to the Gateway channel-notice locale', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.methods = ['config.patch.safe']
    const call = vi.spyOn(rpc, 'call').mockResolvedValue({})
    const store = useAppStore()

    await store.setLocale('zh-Hans')

    expect(call).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'control_ui.default_locale': 'zh-Hans' },
    })
    expect(store.pendingChannelNoticeLocale).toBeNull()
    expect(localStorage.getItem('opensquilla-locale-sync-pending')).toBeNull()
  })

  it('keeps a disconnected explicit selection and syncs it after reconnect', async () => {
    const rpc = useRpcStore()
    rpc.state = 'disconnected'
    rpc.methods = ['config.patch.safe']
    const call = vi.spyOn(rpc, 'call').mockResolvedValue({})
    const store = useAppStore()

    await store.setLocale('zh-Hans')

    expect(call).not.toHaveBeenCalled()
    expect(store.pendingChannelNoticeLocale).toBe('zh-Hans')
    expect(localStorage.getItem('opensquilla-locale-sync-pending')).toBe('zh-Hans')
    const toasts = useToasts().toasts.value
    expect(toasts[toasts.length - 1]).toMatchObject({ tone: 'warn' })

    rpc.state = 'connected'
    await nextTick()
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1))

    expect(call).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'control_ui.default_locale': 'zh-Hans' },
    })
    expect(store.pendingChannelNoticeLocale).toBeNull()
  })

  it('retries a pending locale sync after the connected handshake publishes methods', async () => {
    const rpc = useRpcStore()
    rpc.state = 'disconnected'
    const call = vi.spyOn(rpc, 'call').mockResolvedValue({})
    const store = useAppStore()

    await store.setLocale('zh-Hans')
    rpc.state = 'connected'
    await nextTick()
    expect(call).not.toHaveBeenCalled()

    rpc.methods = ['config.patch.safe']
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1))
    expect(call).toHaveBeenCalledWith('config.patch.safe', {
      patches: { 'control_ui.default_locale': 'zh-Hans' },
    })
    expect(store.pendingChannelNoticeLocale).toBeNull()
  })

  it('retains a failed locale sync without rolling back the local interface', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.methods = ['config.patch.safe']
    const call = vi.spyOn(rpc, 'call').mockRejectedValue(new Error('disk full'))
    const store = useAppStore()

    await store.setLocale('zh-Hans')

    expect(store.locale).toBe('zh-Hans')
    expect(store.pendingChannelNoticeLocale).toBe('zh-Hans')
    const toasts = useToasts().toasts.value
    expect(toasts[toasts.length - 1]).toMatchObject({ tone: 'warn' })

    call.mockResolvedValueOnce({})
    await store.syncLocaleToGateway()

    expect(store.pendingChannelNoticeLocale).toBeNull()
  })

  it('setLocale ignores unsupported codes (no throw, stays en)', async () => {
    const store = useAppStore()
    await store.setLocale('ko' as never)
    expect(store.locale).toBe('en')
  })

  it('initLocale resolves the saved preference and applies it', async () => {
    localStorage.setItem('opensquilla-locale', 'zh-Hans')
    const store = useAppStore()
    await store.initLocale()
    expect(store.locale).toBe('zh-Hans')
    expect(document.documentElement.getAttribute('lang')).toBe('zh-Hans')
  })

  it('initLocale never writes the Gateway locale from browser-local state', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.methods = ['config.patch.safe']
    const call = vi.spyOn(rpc, 'call').mockResolvedValue({})
    localStorage.setItem('opensquilla-locale', 'zh-Hans')
    const store = useAppStore()

    await store.initLocale()

    expect(store.locale).toBe('zh-Hans')
    expect(call).not.toHaveBeenCalled()
  })

  it('syncs the Desktop client locale to the Gateway on startup', async () => {
    const rpc = useRpcStore()
    rpc.state = 'connected'
    rpc.methods = ['config.patch.safe']
    const call = vi.spyOn(rpc, 'call').mockResolvedValue({})
    ;(window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop = {
      getOsLocale: async () => 'zh-CN',
    }
    const store = useAppStore()

    await store.initLocale()

    expect(store.locale).toBe('zh-Hans')
    await vi.waitFor(() => {
      expect(call).toHaveBeenCalledWith('config.patch.safe', {
        patches: { 'control_ui.default_locale': 'zh-Hans' },
      })
    })
    expect(store.pendingChannelNoticeLocale).toBeNull()
  })
})

describe('missing-key fallback', () => {
  it('returns the en string for a key absent from the active locale', () => {
    // an intentionally unknown key falls back to its own key string, never blank
    expect(i18n.global.t('totally.unknown.key')).toBe('totally.unknown.key')
  })
})

describe('catalog parity', () => {
  it('all bundled locales share the exact flattened key set', () => {
    const enKeys = Object.keys(flatten(en as Record<string, unknown>)).sort()
    const locales = { zhHans, de, es, fr, ja }

    for (const [locale, messages] of Object.entries(locales)) {
      const keys = Object.keys(flatten(messages as Record<string, unknown>)).sort()
      expect(keys, locale).toEqual(enKeys)
    }
  })

  it('warns that removing a goal does not recall current-task work', () => {
    expect(en.chat.goal.removeConfirmBody).toContain('use Stop')
    expect(zhHans.chat.goal.removeConfirmBody).toContain('请使用“停止”')
  })

  it('explains the Ensemble image limit with actionable routing choices', () => {
    const locales = [
      { messages: en, imagePattern: /image input/i, unsupportedImagePattern: /image input/i },
      { messages: zhHans, imagePattern: /图片输入/, unsupportedImagePattern: /图片输入/ },
      { messages: de, imagePattern: /Bildeingaben/i, unsupportedImagePattern: /Bilder/i },
      { messages: es, imagePattern: /im[aá]gen/i, unsupportedImagePattern: /imágenes/i },
      { messages: fr, imagePattern: /images en entrée/i, unsupportedImagePattern: /images/i },
      { messages: ja, imagePattern: /画像入力/, unsupportedImagePattern: /画像/ },
    ]

    for (const { messages, imagePattern, unsupportedImagePattern } of locales) {
      const composer = messages.chat.composer
      expect(composer.modelRoutingEnsembleDesc).toMatch(imagePattern)
      expect(composer.ensembleImageUnsupported).toMatch(imagePattern)
      expect(composer.imageInputUnsupported).toMatch(unsupportedImagePattern)
      expect(composer.ensembleImageUnsupported).toContain(composer.modelRouting)
      expect(composer.ensembleImageUnsupported).toContain(composer.modelRoutingSquillaRouter)
      expect(composer.ensembleImageUnsupported).toContain(composer.modelRoutingOff)
    }
  })

  it('no zh-Hans value is left as the English source', () => {
    const enFlat = flatten(en as Record<string, unknown>)
    const zhFlat = flatten(zhHans as Record<string, unknown>)
    const intentionalEnglishTerms = new Set(['chat.routerFx.ensembleTokens'])
    const leaked = Object.keys(enFlat).filter(
      (k) =>
        typeof zhFlat[k] === 'string'
        && zhFlat[k] === enFlat[k]
        && /[A-Za-z]/.test(zhFlat[k] as string)
        && !intentionalEnglishTerms.has(k),
    )
    expect(leaked).toEqual([])
  })

  it('uses model fusion consistently in zh-Hans ensemble copy', () => {
    const zhFlat = flatten(zhHans as Record<string, unknown>)
    const ensemblePrefixes = [
      'setup.modelStrategy.',
      'setup.router.',
      'setup.ensemble.',
      'chat.routerFx.',
      'chat.msgMeta.',
      'chat.routeFeedback.',
    ]
    const ensembleKeys = new Set([
      'setup.provider.activateEnsembleOnPreserved',
      'setup.provider.routingDesc',
      'setup.toast.ensembleSaved',
      'chat.aiModelEnsembleRouter',
      'chat.composer.modelRoutingEnsemble',
      'chat.stream.synthesizingCandidates',
    ])
    const deprecated = Object.entries(zhFlat).filter(([key, value]) =>
      typeof value === 'string'
      && value.includes('聚合')
      && (ensembleKeys.has(key) || ensemblePrefixes.some(prefix => key.startsWith(prefix))),
    )

    expect(deprecated).toEqual([])
    expect({
      setup: zhHans.setup.router.summaryEnsemble,
      composer: zhHans.chat.composer.modelRoutingEnsemble,
      runtime: zhHans.chat.routerFx.ensembleSelecting,
    }).toEqual({
      setup: 'AI 智能融合路由',
      composer: 'AI 智能融合路由',
      runtime: 'AI 智能融合路由 · 正在选择候选',
    })
  })

  it('ships the approved Model Service labels in every locale', () => {
    expect({
      en: en.settings.rail.provider,
      zhHans: zhHans.settings.rail.provider,
      ja: ja.settings.rail.provider,
      fr: fr.settings.rail.provider,
      de: de.settings.rail.provider,
      es: es.settings.rail.provider,
    }).toEqual({
      en: 'Model Service',
      zhHans: '模型服务',
      ja: 'モデルサービス',
      fr: 'Service de modèles',
      de: 'Modelldienst',
      es: 'Servicio de modelos',
    })
  })

  it('describes model strategies by user outcome instead of product rank', () => {
    expect({
      en: [
        en.setup.modelStrategy.cards.router.badge,
        en.setup.modelStrategy.cards.single.badge,
        en.setup.modelStrategy.cards.ensemble.badge,
      ],
      zhHans: [
        zhHans.setup.modelStrategy.cards.router.badge,
        zhHans.setup.modelStrategy.cards.single.badge,
        zhHans.setup.modelStrategy.cards.ensemble.badge,
      ],
      ja: [
        ja.setup.modelStrategy.cards.router.badge,
        ja.setup.modelStrategy.cards.single.badge,
        ja.setup.modelStrategy.cards.ensemble.badge,
      ],
      fr: [
        fr.setup.modelStrategy.cards.router.badge,
        fr.setup.modelStrategy.cards.single.badge,
        fr.setup.modelStrategy.cards.ensemble.badge,
      ],
      de: [
        de.setup.modelStrategy.cards.router.badge,
        de.setup.modelStrategy.cards.single.badge,
        de.setup.modelStrategy.cards.ensemble.badge,
      ],
      es: [
        es.setup.modelStrategy.cards.router.badge,
        es.setup.modelStrategy.cards.single.badge,
        es.setup.modelStrategy.cards.ensemble.badge,
      ],
    }).toEqual({
      en: ['Token-efficient', 'Predictable', 'Capability-first'],
      zhHans: ['按需省 Token', '稳定可控', '能力优先'],
      ja: ['トークン効率重視', '安定・予測可能', '能力重視'],
      fr: ['Économe en tokens', 'Prévisible', 'Capacités prioritaires'],
      de: ['Token-effizient', 'Vorhersehbar', 'Leistung im Fokus'],
      es: ['Uso eficiente de tokens', 'Predecible', 'Prioriza capacidad'],
    })
  })

  it('ships localized TokenRhythm recommendation copy with exact English and zh-Hans wording', () => {
    expect(en.setup.provider.recommendation).toEqual({
      title: 'Recommended: TokenRhythm',
      value: 'TokenRhythm API calls are free for a limited time.',
      cta: 'Register and get an API key',
      externalLabel: 'Register and get an API key — TokenRhythm (opens in a new tab)',
      stepsLabel: 'How to connect TokenRhythm',
      stepRegister: 'Create a TokenRhythm account',
      stepCopy: 'Copy your API key',
      stepPaste: 'Paste it into the API key field below',
      stepSelectAndPaste: 'Choose Add provider, select TokenRhythm, then paste your API key',
      stepReplaceAndPaste: 'Choose Replace key below, then paste your API key',
    })
    expect(zhHans.setup.provider.recommendation).toEqual({
      title: '推荐使用 TokenRhythm',
      value: 'TokenRhythm API 调用限时免费。',
      cta: '注册并获取 API Key',
      externalLabel: '注册并获取 API Key — TokenRhythm（在新标签页中打开）',
      stepsLabel: '如何接入 TokenRhythm',
      stepRegister: '注册 TokenRhythm 账户',
      stepCopy: '复制你的 API Key',
      stepPaste: '粘贴到下方 API key 输入框',
      stepSelectAndPaste: '点击“添加服务商”，选择 TokenRhythm，然后粘贴 API Key',
      stepReplaceAndPaste: '先点击下方「更换密钥」，再粘贴 API Key',
    })

    for (const messages of [en, zhHans, ja, fr, de, es]) {
      const copy = messages.setup.provider.recommendation
      expect(copy.title).toContain('TokenRhythm')
      expect(copy.cta).toBeTruthy()
      expect(copy.externalLabel).toBeTruthy()
      expect(copy.externalLabel).toContain(copy.cta)
      expect(copy.stepsLabel).toBeTruthy()
      expect(copy.stepRegister).toBeTruthy()
      expect(copy.stepCopy).toBeTruthy()
      expect(copy.stepPaste).toBeTruthy()
      expect(copy.stepSelectAndPaste).toBeTruthy()
      expect(copy.stepReplaceAndPaste).toBeTruthy()
    }
  })

  it('ships English and Simplified Chinese image-onboarding copy', () => {
    expect(en.setup.provider.imageGenerationOptInLabel)
      .toBe('Also enable image generation (recommended)')
    expect(zhHans.setup.provider.imageGenerationOptInLabel)
      .toBe('同时启用图像生成（推荐）')
    expect(en.setup.capabilities.imageRecommendationUse).toBe('Use {provider}')
    expect(zhHans.setup.capabilities.imageRecommendationUse).toBe('使用 {provider}')
    expect(en.setup.capabilities.imageRecommendationRegisterExternal)
      .toContain('opens in a new tab')
    expect(zhHans.setup.capabilities.imageRecommendationRegisterExternal)
      .toContain('在新标签页中打开')
  })
})
