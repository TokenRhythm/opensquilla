import { describe, expect, it } from 'vitest'
import { computed, ref } from 'vue'
import {
  buildImagePayload,
  buildSearchPayload,
  imageModelForDisplay,
  imageModelRefForPayload,
  parseImageFallbacks,
  useSetupCapabilitiesForm,
} from './useSetupCapabilitiesForm'

const imageProviders = [
  {
    providerId: 'openai',
    envKey: 'OPENAI_API_KEY',
    requiresApiKey: true,
    defaultBaseUrl: 'https://api.openai.com/v1',
    defaultModel: 'gpt-image-1',
  },
  {
    providerId: 'openrouter',
    envKey: 'OPENROUTER_API_KEY',
    requiresApiKey: true,
    defaultBaseUrl: 'https://openrouter.ai/api/v1',
    defaultModel: 'google/gemini-3.1-flash-image-preview',
  },
]

const baseValues = {
  providerId: 'openrouter',
  primary: 'google/gemini-3.1-flash-image-preview',
  apiKey: '',
  apiKeyEnv: '',
  baseUrl: '',
  size: '1024x1024',
  outputFormat: 'png',
  fallbacks: '',
}

describe('image model references', () => {
  it('removes exactly one routing prefix from the provider-local model name', () => {
    expect(imageModelForDisplay(
      'openrouter',
      'openrouter/google/gemini-3.1-flash-image-preview',
    )).toBe('google/gemini-3.1-flash-image-preview')
    expect(imageModelForDisplay(
      'openrouter',
      'openrouter/openrouter/auto',
    )).toBe('openrouter/auto')
  })

  it('adds exactly one provider prefix to the RPC primary reference', () => {
    expect(imageModelRefForPayload(
      'openrouter',
      'google/gemini-3.1-flash-image-preview',
    )).toBe('openrouter/google/gemini-3.1-flash-image-preview')
    expect(imageModelRefForPayload(
      'openrouter',
      'openrouter/google/gemini-3.1-flash-image-preview',
    )).toBe('openrouter/google/gemini-3.1-flash-image-preview')
    expect(imageModelRefForPayload(
      'openrouter',
      'openrouter/auto',
    )).toBe('openrouter/openrouter/auto')
    expect(imageModelRefForPayload(
      'openai',
      'openai/gpt-image-1',
    )).toBe('openai/gpt-image-1')
  })
})

describe('parseImageFallbacks', () => {
  it('splits on commas and newlines, canonicalizes OpenRouter auto, and drops empties', () => {
    expect(parseImageFallbacks('a/b, c/d\n , e/f')).toEqual(['a/b', 'c/d', 'e/f'])
    expect(parseImageFallbacks('openrouter/auto')).toEqual(['openrouter/openrouter/auto'])
    expect(parseImageFallbacks('   ')).toEqual([])
  })
})

describe('buildImagePayload', () => {
  it('normalizes primary and includes size, format, and explicitly edited fallbacks', () => {
    const payload = buildImagePayload({
      ...baseValues,
      size: '1536x1024',
      outputFormat: 'webp',
      fallbacks: 'openai/gpt-image-1, openrouter/google/gemini-2.5-flash-image',
    }, new Set(['fallbacks'] as const))

    expect(payload).toMatchObject({
      primary: 'openrouter/google/gemini-3.1-flash-image-preview',
      size: '1536x1024',
      outputFormat: 'webp',
      fallbacks: ['openai/gpt-image-1', 'openrouter/google/gemini-2.5-flash-image'],
    })
  })

  it('keeps a pasted canonical primary reference canonical in the payload', () => {
    const payload = buildImagePayload({
      ...baseValues,
      primary: 'openrouter/google/gemini-3.1-flash-image-preview',
    })

    expect(payload.primary).toBe('openrouter/google/gemini-3.1-flash-image-preview')
  })

  it('omits untouched base URL and fallbacks so a save preserves persisted values', () => {
    const payload = buildImagePayload({
      ...baseValues,
      baseUrl: 'https://openrouter.example.test/v1',
      fallbacks: 'openai/gpt-image-1',
    })

    expect(payload).not.toHaveProperty('baseUrl')
    expect(payload).not.toHaveProperty('fallbacks')
  })

  it('sends explicit empty values when base URL and fallbacks are cleared', () => {
    const payload = buildImagePayload(
      baseValues,
      new Set(['baseUrl', 'fallbacks'] as const),
    )

    expect(payload.baseUrl).toBe('')
    expect(payload.fallbacks).toEqual([])
    expect(payload).not.toHaveProperty('clearFallbacks')

    const explicitClear = buildImagePayload(
      baseValues,
      new Set(['fallbacks'] as const),
      { clearFallbacks: true },
    )
    expect(explicitClear.clearFallbacks).toBe(true)
  })

  it('does not send a fallback clear flag when a replacement is nonempty', () => {
    const payload = buildImagePayload({
      ...baseValues,
      fallbacks: 'openai/gpt-image-1',
    }, new Set(['fallbacks'] as const), { clearFallbacks: true })

    expect(payload.fallbacks).toEqual(['openai/gpt-image-1'])
    expect(payload).not.toHaveProperty('clearFallbacks')
  })

  it('never sends both direct and env credentials from inconsistent input', () => {
    const payload = buildImagePayload({
      ...baseValues,
      apiKey: 'sk-direct',
      apiKeyEnv: 'OPENROUTER_API_KEY',
    }, new Set(['apiKey', 'apiKeyEnv'] as const))

    expect(payload.apiKey).toBe('sk-direct')
    expect(payload).not.toHaveProperty('apiKeyEnv')
    expect(payload.credentialMode).toBe('direct')
  })

  it('does not send the legacy enabled switch because configuration enables the capability', () => {
    expect(buildImagePayload(baseValues)).not.toHaveProperty('enabled')
  })

  it('adds a credential mode only when a credential field was edited', () => {
    const untouched = buildImagePayload({
      ...baseValues,
      apiKey: 'sk-untracked',
      apiKeyEnv: 'UNTRACKED_ENV',
    })
    expect(untouched).not.toHaveProperty('credentialMode')
    expect(untouched).not.toHaveProperty('apiKey')
    expect(untouched).not.toHaveProperty('apiKeyEnv')

    const env = buildImagePayload({
      ...baseValues,
      apiKeyEnv: 'OPENROUTER_API_KEY',
    }, new Set(['apiKeyEnv'] as const))
    expect(env).toMatchObject({
      credentialMode: 'env',
      apiKeyEnv: 'OPENROUTER_API_KEY',
    })
  })
})

describe('buildSearchPayload', () => {
  it('preserves hidden advanced values but never sends inline and env keys together', () => {
    const payload = buildSearchPayload({
      providerId: 'brave',
      apiKey: 'test-inline-key',
      apiKeyEnv: 'BRAVE_SEARCH_API_KEY',
      maxResults: 17,
      proxy: 'http://127.0.0.1:7890',
      useEnvProxy: true,
      fallbackPolicy: 'network',
      diagnostics: true,
    })

    expect(payload).toMatchObject({
      providerId: 'brave',
      apiKey: 'test-inline-key',
      maxResults: 17,
      proxy: 'http://127.0.0.1:7890',
      useEnvProxy: true,
      fallbackPolicy: 'network',
      diagnostics: true,
    })
    expect(payload).not.toHaveProperty('apiKeyEnv')
  })
})

describe('useSetupCapabilitiesForm search provider switching', () => {
  const searchProviders = [
    { providerId: 'duckduckgo', requiresApiKey: false },
    { providerId: 'brave', requiresApiKey: true, envKey: 'BRAVE_SEARCH_API_KEY' },
    { providerId: 'tavily', requiresApiKey: true, envKey: 'TAVILY_API_KEY' },
  ]

  it('preserves a saved env reference until the provider is changed', () => {
    const form = useSetupCapabilitiesForm()
    form.initSearchFromConfig({
      search_provider: 'brave',
      search_api_key_env: 'CUSTOM_BRAVE_KEY',
    }, searchProviders)

    expect(form.searchPayload().apiKeyEnv).toBe('CUSTOM_BRAVE_KEY')

    form.updateField('search', 'provider', 'tavily')
    form.onSearchProviderChange(searchProviders[2])

    expect(form.searchPayload()).not.toHaveProperty('apiKeyEnv')
    expect(form.searchPayload()).not.toHaveProperty('apiKey')
  })

  it('does not silently configure a hidden env reference for a new keyed provider', () => {
    const form = useSetupCapabilitiesForm()
    form.initSearchFromConfig({}, searchProviders)

    form.updateField('search', 'provider', 'brave')
    form.onSearchProviderChange(searchProviders[1])

    expect(form.searchPayload()).not.toHaveProperty('apiKeyEnv')
  })
})

describe('useSetupCapabilitiesForm image hydration', () => {
  it('uses the primary provider over a stale credential-provider status', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openai',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.selectedImageProvider.value).toBe('openrouter')
    expect(form.imagePrimaryValue.value).toBe('google/gemini-3.1-flash-image-preview')
    expect(form.imagePayload().primary)
      .toBe('openrouter/google/gemini-3.1-flash-image-preview')
  })

  it('keeps a redacted saved key as boolean state without filling the key input', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: {
          openrouter: {
            api_key: '[redacted]',
            api_key_env: 'STALE_OPENROUTER_ENV',
          },
        },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.imageKeyConfiguredValue.value).toBe(true)
    expect(form.imageCredentialSourceValue.value).toBe('explicit')
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imageApiKeyEnvValue.value).toBe('')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')
    expect(form.imagePayload()).not.toHaveProperty('apiKeyEnv')
    expect(form.imagePayload()).not.toHaveProperty('credentialMode')
  })
})

// A full panel context whose values are irrelevant to the assertion under
// test: createPanel dereferences every context field eagerly, so exposure
// tests need the complete shape even when only form state is inspected.
function stubPanelContext() {
  const text = computed(() => '')
  const flag = computed(() => false)
  const list = computed(() => [] as string[])
  const providers = computed(() => [] as Array<{ providerId: string; label: string }>)
  const imageModels = computed(() => [])
  return {
    searchProviders: providers,
    memoryProviders: providers,
    imageProviders: providers,
    imageSpec: computed(() => null),
    imageModels,
    imageModelSource: computed(() => 'none'),
    searchRequiresKey: flag,
    searchKeyPlaceholder: text,
    searchDraftDirty: flag,
    searchDraftMissingKey: flag,
    searchDraftStatusText: text,
    searchEnvPlaceholder: text,
    searchAdvancedOpen: flag,
    searchNeeds: list,
    searchEnvCommand: text,
    searchStatusText: () => '',
    memoryApiKeyEnabled: flag,
    memoryRemoteOptionsOpen: flag,
    memoryRemoteOptionsSummary: text,
    memoryModelPlaceholder: text,
    memoryBasePlaceholder: text,
    memoryOnnxPlaceholder: text,
    memoryApiKeyLabel: text,
    memoryApiKeyPlaceholder: text,
    memoryEnvPlaceholder: text,
    memoryNeeds: list,
    memoryStatusText: text,
    memoryModeTitle: text,
    memoryModeDescription: text,
    memoryExpandable: flag,
    memoryEnvCommand: text,
    imageNeeds: list,
    imageStatusText: text,
    imageEnvCommand: text,
    capabilityBadgeTone: () => '',
    capabilityBadgeLabel: () => '',
    memoryAutoCapture: ref(false),
    audioEnabled: ref(false),
    audioApiKey: ref(''),
    audioApiKeyEnv: ref(''),
    audioBaseUrl: ref(''),
    audioTtsVoice: ref(''),
    audioTtsModel: ref(''),
    audioLanguageCode: ref(''),
    audioStatusText: text,
    audioBadgeTone: text,
    audioBadgeLabel: text,
    audioKeyPlaceholder: text,
    resettable: () => false,
    resetPending: ref<'search' | 'memory_embedding' | 'image_generation' | 'audio' | ''>(''),
  }
}

describe('useSetupCapabilitiesForm image key state', () => {
  it('tracks the stored-key state per provider draft across switches', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: { openrouter: { api_key: '[redacted]' } },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)
    expect(form.imageKeyConfiguredValue.value).toBe(true)
    expect(form.imageCredentialSourceValue.value).toBe('explicit')

    form.onImageProviderChange(imageProviders[0])
    expect(form.imageKeyConfiguredValue.value).toBe(false)
    expect(form.imageCredentialSourceValue.value).toBe('none')

    form.onImageProviderChange(imageProviders[1])
    expect(form.imageKeyConfiguredValue.value).toBe(true)
    expect(form.imageCredentialSourceValue.value).toBe('explicit')
  })

  it('exposes the stored-key state on the panel without ever exposing the key', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: { openrouter: { api_key: '[redacted]' } },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)
    const panel = form.createPanel(stubPanelContext())

    expect(panel.value.form.imageKeyConfigured).toBe(true)
    expect(panel.value.form.imageCredentialSource).toBe('explicit')
    expect(panel.value.form.imageApiKey).toBe('')

    form.onImageProviderChange(imageProviders[0])
    expect(panel.value.form.imageKeyConfigured).toBe(false)
    expect(panel.value.form.imageCredentialSource).toBe('none')
    expect(panel.value.form.imageApiKey).toBe('')
  })

  it('renders a provider credentialed by a saved env reference as configured', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: { openrouter: { api_key_env: 'CUSTOM_OPENROUTER_ENV' } },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.imageKeyConfiguredValue.value).toBe(true)
    expect(form.imageCredentialSourceValue.value).toBe('env')
    // The env reference stays editable; only the direct key is write-only.
    expect(form.imageApiKeyEnvValue.value).toBe('CUSTOM_OPENROUTER_ENV')
  })

  it('trusts the backend configured status for the matching provider only', () => {
    const form = useSetupCapabilitiesForm()

    // Ambient-environment credential: nothing stored in config, but the
    // status RPC already computed that image generation works.
    form.initImageFromConfig({}, {
      imageGenerationConfigured: true,
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)
    expect(form.imageKeyConfiguredValue.value).toBe(true)
    expect(form.imageCredentialSourceValue.value).toBe('configured')

    // The status describes the active provider; another provider without any
    // stored credential is honestly not configured.
    form.onImageProviderChange(imageProviders[0])
    expect(form.imageKeyConfiguredValue.value).toBe(false)
    expect(form.imageCredentialSourceValue.value).toBe('none')
  })

  it('exposes backend credential-source detail for reusable and broken credentials', () => {
    const reusable = useSetupCapabilitiesForm()
    reusable.initImageFromConfig({}, {
      imageGenerationConfigured: true,
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
      imageGenerationSource: 'llm_fallback',
    }, imageProviders)

    expect(reusable.imageKeyConfiguredValue.value).toBe(true)
    expect(reusable.imageCredentialSourceValue.value).toBe('llm_fallback')

    const broken = useSetupCapabilitiesForm()
    broken.initImageFromConfig({}, {
      imageGenerationConfigured: false,
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
      imageGenerationSource: 'missing_env',
    }, imageProviders)

    expect(broken.imageKeyConfiguredValue.value).toBe(false)
    expect(broken.imageCredentialSourceValue.value).toBe('missing_env')
  })
})

describe('useSetupCapabilitiesForm provider drafts', () => {
  it('restores each provider model and endpoint across the 0.5.0 event order', () => {
    const form = useSetupCapabilitiesForm()
    const openrouter = imageProviders[1]

    form.initImageFromConfig({
      image_generation: {
        providers: {
          openai: { base_url: 'https://saved.openai.example/v1' },
          openrouter: { base_url: 'https://saved.openrouter.example/v1' },
        },
      },
    }, {
      imageGenerationProvider: 'openai',
      imageGenerationPrimary: 'openai/gpt-image-1',
    }, imageProviders)
    form.updateField('image', 'primary', 'custom-openai-image')

    // The old panel first emitted updateField(provider), then this dedicated
    // event. Both now go through the same idempotent switch operation.
    form.updateField('image', 'provider', 'openrouter')
    expect(form.imagePrimaryValue.value).toBe('google/gemini-3.1-flash-image-preview')
    expect(form.imageBaseUrlValue.value).toBe('https://saved.openrouter.example/v1')
    form.onImageProviderChange(openrouter)

    expect(form.selectedImageProvider.value).toBe('openrouter')
    expect(form.imagePrimaryValue.value).toBe('google/gemini-3.1-flash-image-preview')
    expect(form.imageBaseUrlValue.value).toBe('https://saved.openrouter.example/v1')

    form.updateField('image', 'provider', 'openai')
    expect(form.imagePrimaryValue.value).toBe('custom-openai-image')
    expect(form.imageBaseUrlValue.value).toBe('https://saved.openai.example/v1')
  })

  it('does not carry a transient pasted key to another provider or back again', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openai',
      imageGenerationPrimary: 'openai/gpt-image-1',
    }, imageProviders)
    form.updateField('image', 'apiKey', 'sk-transient')
    expect(form.imagePayload().apiKey).toBe('sk-transient')

    form.onImageProviderChange(imageProviders[1])
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')

    form.onImageProviderChange(imageProviders[0])
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')
  })

  it('keeps direct and env credential edits mutually exclusive in both directions', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    form.updateField('image', 'apiKey', 'sk-direct')
    expect(form.imageApiKeyEnvValue.value).toBe('')
    expect(form.imagePayload()).toMatchObject({
      apiKey: 'sk-direct',
      credentialMode: 'direct',
    })
    expect(form.imagePayload()).not.toHaveProperty('apiKeyEnv')

    form.updateField('image', 'apiKeyEnv', 'CUSTOM_OPENROUTER_KEY')
    expect(form.imageApiKeyValue.value).toBe('')
    expect(form.imagePayload()).toMatchObject({
      apiKeyEnv: 'CUSTOM_OPENROUTER_KEY',
      credentialMode: 'env',
    })
    expect(form.imagePayload()).not.toHaveProperty('apiKey')

    // Emptying the key field must fall all the way back to "keep": a touched
    // but empty key would author credentialMode 'direct' and destroy a stored
    // env reference server-side.
    form.updateField('image', 'apiKey', '')
    expect(form.imagePayload()).not.toHaveProperty('credentialMode')
    expect(form.imagePayload()).not.toHaveProperty('apiKey')
  })

  it('treats a key field emptied after typing as untouched, keeping the saved credential', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: { openrouter: { api_key_env: 'CUSTOM_OPENROUTER_ENV' } },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    form.updateField('image', 'apiKey', 'sk-typo')
    expect(form.imagePayload()).toMatchObject({
      credentialMode: 'direct',
      apiKey: 'sk-typo',
    })

    form.updateField('image', 'apiKey', '')
    const payload = form.imagePayload()
    expect(payload).not.toHaveProperty('credentialMode')
    expect(payload).not.toHaveProperty('apiKey')
    expect(payload).not.toHaveProperty('apiKeyEnv')
  })

  it('treats an env field emptied after typing as untouched, keeping the saved credential', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        providers: { openrouter: { api_key: 'sk-saved-direct' } },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    form.updateField('image', 'apiKeyEnv', 'TYPO_ENV')
    expect(form.imagePayload()).toMatchObject({
      credentialMode: 'env',
      apiKeyEnv: 'TYPO_ENV',
    })

    // A touched-but-empty env reference would author credentialMode 'env'
    // and erase the stored direct key server-side, so blank means "keep".
    form.updateField('image', 'apiKeyEnv', '')
    const payload = form.imagePayload()
    expect(payload).not.toHaveProperty('credentialMode')
    expect(payload).not.toHaveProperty('apiKey')
    expect(payload).not.toHaveProperty('apiKeyEnv')
  })

  it('distinguishes untouched optional fields from explicit clearing', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({
      image_generation: {
        fallbacks: ['openai/gpt-image-1'],
        providers: {
          openrouter: { base_url: 'https://saved.openrouter.example/v1' },
        },
      },
    }, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)

    expect(form.imagePayload()).not.toHaveProperty('baseUrl')
    expect(form.imagePayload()).not.toHaveProperty('fallbacks')

    form.updateField('image', 'baseUrl', '')
    form.updateField('image', 'fallbacks', '')
    expect(form.imagePayload()).toMatchObject({
      baseUrl: '',
      fallbacks: [],
      clearFallbacks: true,
    })

    form.onImageProviderChange(imageProviders[0])
    expect(form.imagePayload()).toMatchObject({ fallbacks: [], clearFallbacks: true })
    expect(form.imagePayload()).not.toHaveProperty('baseUrl')
  })

  it('does not mark an initially empty fallback field as an explicit clear', () => {
    const form = useSetupCapabilitiesForm()

    form.initImageFromConfig({}, {
      imageGenerationProvider: 'openrouter',
      imageGenerationPrimary: 'openrouter/google/gemini-3.1-flash-image-preview',
    }, imageProviders)
    form.updateField('image', 'fallbacks', '')

    expect(form.imagePayload().fallbacks).toEqual([])
    expect(form.imagePayload()).not.toHaveProperty('clearFallbacks')
  })
})
