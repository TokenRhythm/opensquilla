// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { ImageCredentialSource } from '@/composables/setup/useSetupCapabilitiesForm'
import SetupCapabilitiesPanel from './SetupCapabilitiesPanel.vue'

const mounted: App[] = []

function panel() {
  return {
    form: {
      searchProvider: 'duckduckgo',
      searchApiKey: '',
      imageProvider: 'openrouter',
      imagePrimary: 'google/gemini-image',
      imageApiKey: '',
      imageEnabled: true,
      imageKeyConfigured: false,
      imageCredentialSource: 'none' as ImageCredentialSource,
      audioApiKey: '',
    },
    options: {
      searchProviders: [
        {
          providerId: 'duckduckgo',
          label: 'DuckDuckGo',
          requiresApiKey: false,
        },
        {
          providerId: 'brave',
          label: 'Brave Search',
          requiresApiKey: true,
        },
      ],
      imageProviders: [{ providerId: 'openrouter', label: 'OpenRouter' }],
      imageCredentialOptions: [] as Array<{
        providerId: string
        available: boolean
        source: string
        owner: string
      }>,
      imageRecommendation: null as {
        providerId: string
        label: string
        canReuseCredential: boolean
        actionRequired: boolean
        registrationUrl: string
      } | null,
      imageModels: [
        {
          id: 'google/gemini-image',
          name: 'Gemini Image',
          contextWindow: null,
          maxOutputTokens: null,
          capabilities: [],
          pricing: null,
          capabilitySource: '',
        },
        {
          id: 'vendor/image-pro',
          name: 'Image Pro',
          contextWindow: null,
          maxOutputTokens: null,
          capabilities: [],
          pricing: null,
          capabilitySource: '',
        },
      ],
    },
    state: {
      searchRequiresKey: false,
      searchKeyPlaceholder: 'Paste API key',
      searchDraftDirty: false,
      searchDraftMissingKey: false,
      searchDraftStatusText: '',
      searchStatusText: 'Search is ready.',
      memoryStatusText: 'Keyword search remains available.',
      memoryModeTitle: 'Find related content in saved memory',
      memoryModeDescription: 'Runs on this device',
      memoryExpandable: false,
      imageStatusText: 'Add a key.',
      imageModelSource: 'catalog',
      audioStatusText: 'Add an ElevenLabs key.',
      audioKeyPlaceholder: 'Paste key',
      capabilityBadgeTone: (name: string): string => name === 'search' ? 'is-ok' : 'is-muted',
      capabilityBadgeLabel: (name: string): string => name === 'search' ? 'Available' : 'Set up',
      resettable: (name: string): boolean => name === 'image_generation',
      resetPending: '',
    },
  }
}

async function mountPanel(panelValue = panel()) {
  const resetCapability = vi.fn()
  const updateField = vi.fn()
  const useImageRecommendation = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SetupCapabilitiesPanel, {
    panel: panelValue,
    onResetCapability: resetCapability,
    onUpdateField: updateField,
    onUseImageRecommendation: useImageRecommendation,
  })
  app.use(i18n)
  app.mount(el)
  mounted.push(app)
  await nextTick()
  return { el, resetCapability, updateField, useImageRecommendation }
}

afterEach(() => {
  while (mounted.length) mounted.pop()!.unmount()
  document.body.innerHTML = ''
})

describe('SetupCapabilitiesPanel', () => {
  it('starts fully collapsed and keeps a single-open accessible accordion', async () => {
    i18n.global.locale.value = 'en'
    const { el } = await mountPanel()
    const triggers = el.querySelectorAll<HTMLButtonElement>('.capability-card__trigger')
    const chevrons = el.querySelectorAll<SVGElement>('svg.capability-card__chevron')

    expect(triggers).toHaveLength(3)
    expect(chevrons).toHaveLength(3)
    expect(chevrons[0]?.querySelector('path')?.getAttribute('d')).toBe('m6 8 4 4 4-4')
    expect(el.textContent).not.toContain('⌄')
    expect(triggers[0]?.getAttribute('aria-expanded')).toBe('false')
    expect(triggers[1]?.getAttribute('aria-expanded')).toBe('false')
    expect(triggers[2]?.getAttribute('aria-expanded')).toBe('false')
    expect(el.querySelector('#capability-search-panel')?.getAttribute('style')).toContain('display: none')
    expect(el.querySelector('#capability-image_generation-panel')?.getAttribute('style')).toContain('display: none')
    expect(el.querySelector('#capability-audio-panel')?.getAttribute('style')).toContain('display: none')
    expect(el.querySelector('[name="setup_image_enabled"]')).not.toBeNull()
    expect(el.querySelector('[name="setup_audio_enabled"]')).toBeNull()
    expect(el.querySelector('[name="setup_search_proxy"]')).toBeNull()
    expect(el.querySelector('[name="setup_provider_image_model_identifier"]')).not.toBeNull()
    expect(el.querySelector('[name="setup_audio_tts_model"]')).toBeNull()

    triggers[1]!.click()
    await nextTick()

    expect(triggers[0]?.getAttribute('aria-expanded')).toBe('false')
    expect(triggers[1]?.getAttribute('aria-expanded')).toBe('true')
    expect(el.querySelector('#capability-search-panel')?.getAttribute('style')).toContain('display: none')
    expect(el.querySelector('#capability-image_generation-panel')?.getAttribute('role')).toBe('region')

    triggers[1]!.click()
    await nextTick()

    expect(triggers[1]?.getAttribute('aria-expanded')).toBe('false')
  })

  it('labels key-free and key-required search providers before selection', async () => {
    i18n.global.locale.value = 'en'
    const { el } = await mountPanel()
    const options = Array.from(
      el.querySelectorAll<HTMLOptionElement>('[name="setup_search_provider"] option'),
    )

    expect(options.map(option => option.textContent)).toEqual([
      'DuckDuckGo · No key needed',
      'Brave Search · API key required',
    ])
  })

  it('renders default memory as a static built-in summary without a chevron', async () => {
    i18n.global.locale.value = 'en'
    const { el } = await mountPanel()

    expect(el.textContent).toContain('Find related content in saved memory')
    expect(el.textContent).toContain('Runs on this device')
    expect(el.textContent).not.toContain('BGE-small-zh-v1.5')
    expect(el.querySelector('#capability-memory_embedding-trigger')).toBeNull()
    expect(el.querySelector('[name="setup_memory_provider"]')).toBeNull()
  })

  it('makes managed custom memory expandable so it can be restored', async () => {
    i18n.global.locale.value = 'en'
    const custom = panel()
    custom.state.memoryExpandable = true
    custom.state.memoryModeTitle = 'Custom embedding · OpenAI'
    custom.state.memoryModeDescription = 'Managed through CLI or TOML.'
    custom.state.resettable = name => name === 'memory_embedding'
    const { el } = await mountPanel(custom)
    const trigger = el.querySelector<HTMLButtonElement>('#capability-memory_embedding-trigger')

    expect(trigger).not.toBeNull()
    trigger!.click()
    await nextTick()

    expect(el.textContent).toContain('Custom embedding · OpenAI')
    expect(el.textContent).toContain('Restore built-in settings')
  })

  it('separates an unsaved search draft from current status and marks a missing key', async () => {
    i18n.global.locale.value = 'en'
    const draft = panel()
    draft.form.searchProvider = 'brave'
    draft.state.searchRequiresKey = true
    draft.state.searchDraftDirty = true
    draft.state.searchDraftMissingKey = true
    draft.state.searchDraftStatusText =
      'Currently using DuckDuckGo; add an API key before switching to Brave Search.'
    draft.state.capabilityBadgeLabel = name => (
      name === 'search' ? 'Currently available' : 'Set up'
    )
    const { el } = await mountPanel(draft)
    const input = el.querySelector<HTMLInputElement>('[name="setup_search_api_key"]')

    expect(el.textContent).toContain('Currently available')
    expect(el.textContent).toContain('Currently using DuckDuckGo')
    expect(input?.getAttribute('aria-invalid')).toBe('true')
    expect(input?.getAttribute('aria-describedby')).toBe('setup-search-api-key-error')
    expect(el.textContent).toContain('Enter an API key before saving')
  })

  it('offers provider image models while preserving custom model entry', async () => {
    i18n.global.locale.value = 'en'
    const { el, updateField } = await mountPanel()
    const model = el.querySelector<HTMLInputElement>(
      '[name="setup_provider_image_model_identifier"]',
    )
    const apiKey = el.querySelector<HTMLInputElement>('[name="setup_image_api_key"]')

    expect(model?.value).toBe('google/gemini-image')
    expect(model?.getAttribute('role')).toBe('combobox')
    expect(model?.getAttribute('autocomplete')).toBe('one-time-code')
    expect(model?.getAttribute('aria-autocomplete')).toBe('list')
    expect(model?.getAttribute('data-form-type')).toBe('other')
    expect(model?.getAttribute('data-lpignore')).toBe('true')
    expect(apiKey?.getAttribute('autocomplete')).toBe('off')
    expect(apiKey?.getAttribute('data-form-type')).toBe('other')

    model!.dispatchEvent(new Event('focus'))
    await nextTick()
    const options = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    expect(options.map(option => option.textContent)).toEqual(
      expect.arrayContaining([
        expect.stringContaining('google/gemini-image'),
        expect.stringContaining('vendor/image-pro'),
      ]),
    )

    model!.value = 'custom/image-model'
    model!.dispatchEvent(new Event('input'))
    expect(updateField).toHaveBeenCalledWith('image', 'primary', 'custom/image-model')
  })

  it('groups the recommendation, configured model providers, and other image providers', async () => {
    i18n.global.locale.value = 'en'
    const grouped = panel()
    grouped.options.imageProviders = [
      { providerId: 'tokenrhythm', label: 'TokenRhythm Images' },
      { providerId: 'openrouter', label: 'OpenRouter Images' },
      { providerId: 'openai', label: 'OpenAI Images' },
    ]
    grouped.options.imageRecommendation = {
      providerId: 'tokenrhythm',
      label: 'TokenRhythm Images',
      canReuseCredential: true,
      actionRequired: false,
      registrationUrl: '',
    }
    grouped.options.imageCredentialOptions = [
      {
        providerId: 'openrouter',
        available: true,
        source: 'llm_fallback',
        owner: 'primary',
      },
      {
        providerId: 'openai',
        available: true,
        source: 'env',
        owner: 'image',
      },
    ]

    const { el } = await mountPanel(grouped)
    const groups = Array.from(
      el.querySelectorAll<HTMLOptGroupElement>('[name="setup_image_provider"] optgroup'),
    )

    expect(groups.map(group => group.label)).toEqual([
      'Recommended',
      'Configured in Model providers',
      'Other providers',
    ])
    expect(groups[1]?.textContent).toContain('OpenRouter Images')
    expect(groups[2]?.textContent).toContain('OpenAI Images')
  })

  it('emits the explicit image-generation enable switch', async () => {
    i18n.global.locale.value = 'en'
    const { el, updateField } = await mountPanel()
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_image_enabled"]')

    toggle!.checked = false
    toggle!.dispatchEvent(new Event('change'))

    expect(updateField).toHaveBeenCalledWith('image', 'enabled', false)
  })

  it('renders a catalog-backed image recommendation without selecting it', async () => {
    i18n.global.locale.value = 'en'
    const recommended = panel()
    recommended.form.imageProvider = ''
    recommended.form.imagePrimary = ''
    recommended.options.imageProviders = [
      { providerId: 'openrouter', label: 'OpenRouter Images' },
      { providerId: 'tokenrhythm', label: 'TokenRhythm Images' },
    ]
    recommended.options.imageRecommendation = {
      providerId: 'tokenrhythm',
      label: 'TokenRhythm Images',
      canReuseCredential: false,
      actionRequired: true,
      registrationUrl: 'https://tokenrhythm.studio/register',
    }
    const { el, useImageRecommendation } = await mountPanel(recommended)

    const select = el.querySelector<HTMLSelectElement>('[name="setup_image_provider"]')
    const card = el.querySelector<HTMLElement>('[data-testid="image-provider-recommendation"]')
    const groups = Array.from(select?.querySelectorAll('optgroup') || [])
    const useButton = Array.from(card?.querySelectorAll<HTMLButtonElement>('button') || [])
      .find(button => button.textContent?.includes('Use TokenRhythm Images'))
    const registration = card?.querySelector<HTMLAnchorElement>('a')

    expect(select?.value).toBe('')
    expect(groups.map(group => group.label)).toEqual(['Recommended', 'Other providers'])
    expect(card?.getAttribute('role')).toBe('note')
    expect(card?.textContent).toContain('Recommended: TokenRhythm Images')
    expect(registration?.getAttribute('target')).toBe('_blank')
    expect(registration?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(registration?.getAttribute('aria-label')).toContain('opens in a new tab')

    useButton!.click()
    expect(useImageRecommendation).toHaveBeenCalledWith('tokenrhythm')
  })

  it('keeps a non-actionable recommendation in the picker without showing a setup card', async () => {
    i18n.global.locale.value = 'en'
    const recommended = panel()
    recommended.options.imageProviders = [
      { providerId: 'openrouter', label: 'OpenRouter Images' },
      { providerId: 'tokenrhythm', label: 'TokenRhythm Images' },
    ]
    recommended.options.imageRecommendation = {
      providerId: 'tokenrhythm',
      label: 'TokenRhythm Images',
      canReuseCredential: false,
      actionRequired: false,
      registrationUrl: 'https://tokenrhythm.studio/register',
    }
    const { el } = await mountPanel(recommended)

    const select = el.querySelector<HTMLSelectElement>('[name="setup_image_provider"]')
    const groups = Array.from(select?.querySelectorAll('optgroup') || [])

    expect(select?.value).toBe('openrouter')
    expect(groups.map(group => group.label)).toEqual(['Recommended', 'Other providers'])
    expect(groups[0]?.textContent).toContain('TokenRhythm Images')
    expect(el.querySelector('[data-testid="image-provider-recommendation"]')).toBeNull()
  })

  it('does not ask for registration when the recommended provider can reuse the LLM key', async () => {
    i18n.global.locale.value = 'en'
    const recommended = panel()
    recommended.form.imageProvider = ''
    recommended.form.imagePrimary = ''
    recommended.options.imageProviders = [
      { providerId: 'tokenrhythm', label: 'TokenRhythm Images' },
    ]
    recommended.options.imageRecommendation = {
      providerId: 'tokenrhythm',
      label: 'TokenRhythm Images',
      canReuseCredential: true,
      actionRequired: true,
      registrationUrl: 'https://tokenrhythm.studio/register',
    }
    const { el } = await mountPanel(recommended)

    const card = el.querySelector<HTMLElement>('[data-testid="image-provider-recommendation"]')
    expect(card).not.toBeNull()
    expect(card?.querySelector('a')).toBeNull()
  })

  it('shows a reusable model-provider credential without duplicating the key field', async () => {
    i18n.global.locale.value = 'en'
    const reused = panel()
    reused.form.imageKeyConfigured = true
    reused.form.imageCredentialSource = 'llm_fallback'
    const { el } = await mountPanel(reused)

    expect(el.textContent).toContain('Using the OpenRouter key from Model providers')
    expect(el.textContent).toContain('Source: Model providers · OpenRouter')
    expect(el.querySelector('[name="setup_image_api_key"]')).toBeNull()

    const action = el.querySelector<HTMLButtonElement>('.capability-card__credential-action')
    expect(action?.textContent).toContain('Set separately')
    action!.click()
    await nextTick()

    const input = el.querySelector<HTMLInputElement>('[name="setup_image_api_key"]')
    expect(input?.getAttribute('aria-label')).toBe('Dedicated image-generation API key')
    expect(input?.getAttribute('autocomplete')).toBe('off')
  })

  it('keeps a dedicated image key summarized until the user chooses to replace it', async () => {
    i18n.global.locale.value = 'en'
    const direct = panel()
    direct.form.imageKeyConfigured = true
    direct.form.imageCredentialSource = 'explicit'
    const { el } = await mountPanel(direct)

    expect(el.textContent).toContain('Image key configured separately')
    expect(el.textContent).toContain('leave blank to keep it')
    expect(el.querySelector('[name="setup_image_api_key"]')).toBeNull()
    expect(el.querySelector('.capability-card__credential-action')?.textContent).toContain('Replace')
  })

  it('shows the image key field immediately when no usable credential exists', async () => {
    i18n.global.locale.value = 'en'
    const missing = panel()
    missing.form.imageCredentialSource = 'missing_env'
    const { el } = await mountPanel(missing)

    expect(el.textContent).toContain('Environment key unavailable')
    expect(el.textContent).toContain('cannot be read')
    expect(el.querySelector('[name="setup_image_api_key"]')).not.toBeNull()
    expect(el.querySelector('.capability-card__credential-source.is-missing')).not.toBeNull()
  })

  it('only exposes reset actions explicitly advertised by the gateway', async () => {
    i18n.global.locale.value = 'en'
    const { el, resetCapability } = await mountPanel()
    const resetButtons = Array.from(
      el.querySelectorAll<HTMLButtonElement>('.capability-card__actions button'),
    )

    expect(resetButtons).toHaveLength(1)
    expect(resetButtons[0]?.textContent).toContain('Remove configuration')
    resetButtons[0]!.click()
    expect(resetCapability).toHaveBeenCalledWith('image_generation')
  })
})
