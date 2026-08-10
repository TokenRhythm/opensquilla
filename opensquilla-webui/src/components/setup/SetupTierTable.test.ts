// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import SetupTierTable from './SetupTierTable.vue'
import type {
  DiscoveredModel,
  DiscoveredModelsByProvider,
} from '@/composables/setup/useSetupProviderForm'

const ROWS = [
  {
    name: 'c0',
    provider: 'openrouter',
    model: 'deepseek/deepseek-v4-flash',
    thinkingLevel: 'high',
    supportsImage: false,
  },
  {
    name: 'c1',
    provider: 'openai',
    model: 'test-model-1',
    thinkingLevel: '',
    supportsImage: true,
  },
]

const DISCOVERED: DiscoveredModel[] = [
  {
    id: 'test-vendor/alpha',
    name: 'Alpha',
    contextWindow: 262144,
    maxOutputTokens: 16384,
    capabilities: ['chat'],
    pricing: null,
    capabilitySource: 'provider',
  },
]

const TOKENRHYTHM_DISCOVERED: DiscoveredModel[] = [
  {
    id: 'deepseek-v4-flash',
    name: 'DeepSeek V4 Flash',
    contextWindow: 128000,
    maxOutputTokens: 16384,
    capabilities: ['chat', 'tools'],
    pricing: null,
    capabilitySource: 'provider',
  },
]

async function mountTable(props: Record<string, unknown> = {}, listeners: Record<string, unknown> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SetupTierTable, {
    rows: ROWS,
    tierLabel: (tier: string) => tier,
    providerOptions: [
      { providerId: 'openrouter', label: 'OpenRouter' },
      { providerId: 'openai', label: 'OpenAI' },
    ],
    ...props,
    ...listeners,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

async function mountTableWithAsyncCatalog() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const rows = ref(ROWS.map(row => ({ ...row })))
  const modelsByProvider = ref<DiscoveredModelsByProvider>({})
  const host = defineComponent({
    setup() {
      return () => h(SetupTierTable, {
        rows: rows.value,
        tierLabel: (tier: string) => tier,
        providerOptions: [
          { providerId: 'openrouter', label: 'OpenRouter' },
          { providerId: 'openai', label: 'OpenAI' },
        ],
        modelsByProvider: modelsByProvider.value,
        onUpdateTierField: (name: string, key: string, value: string | boolean) => {
          const row = rows.value.find(item => item.name === name)
          if (row && key === 'model') row.model = String(value)
        },
      })
    },
  })
  const app = createApp(host)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el, modelsByProvider }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('SetupTierTable — editable routing rows', () => {
  it('hides the redundant provider column when every row uses the only configured provider', async () => {
    const rows = ROWS.map(row => ({ ...row, provider: 'openrouter' }))
    const { app, el } = await mountTable({
      rows,
      providerOptions: [{ providerId: 'openrouter', label: 'OpenRouter' }],
    })

    const table = el.querySelector('[role="table"]')
    expect(table?.classList.contains('setup-tier-table--without-provider')).toBe(true)
    expect(el.querySelector('.setup-tier-table__row.is-head')?.textContent)
      .not.toContain('Request entry')
    expect(el.querySelector('[aria-label="c0 request entry"]')).toBeNull()
    expect(el.querySelector('[aria-label="c1 request entry"]')).toBeNull()
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')?.disabled).toBe(false)
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c1 model"]')?.disabled).toBe(false)

    app.unmount()
  })

  it('keeps the provider column when no provider is configured', async () => {
    const { app, el } = await mountTable({ providerOptions: [] })

    expect(el.querySelector('[role="table"]')?.classList.contains('setup-tier-table--without-provider'))
      .toBe(false)
    expect(el.querySelector('.setup-tier-table__row.is-head')?.textContent)
      .toContain('Request entry')
    expect(el.querySelector('[aria-label="c0 request entry"]')).toBeTruthy()

    app.unmount()
  })

  it('renders provider, model, thinking, and image controls', async () => {
    const { app, el } = await mountTable()

    const table = el.querySelector('[role="table"]')
    expect(table).toBeTruthy()
    expect(table?.getAttribute('aria-disabled')).toBeNull()
    const head = el.querySelector('.setup-tier-table__row.is-head')
    expect(head?.textContent).toContain('Tier')
    expect(head?.textContent).toContain('Request entry')
    expect(head?.textContent).toContain('Model')
    expect(head?.textContent).toContain('Thinking')
    expect(head?.textContent).toContain('Image')

    const requestEntry = el.querySelector('[aria-label="c0 request entry"]')
    expect(requestEntry?.tagName).toBe('SELECT')
    expect((requestEntry as HTMLSelectElement)?.value).toBe('openrouter')

    const model = el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')
    expect(model?.value).toBe('deepseek/deepseek-v4-flash')
    expect(model?.disabled).toBe(false)
    expect(el.querySelector<HTMLSelectElement>('select[aria-label="c0 thinking level"]')?.value).toBe('high')
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 supports image"]')).toBeTruthy()

    app.unmount()
  })

  it('emits updateTierField from the model input', async () => {
    const onUpdateTierField = vi.fn()
    const { app, el } = await mountTable({}, { onUpdateTierField })

    const model = el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')!
    model.value = 'new-model'
    model.dispatchEvent(new Event('input', { bubbles: true }))

    expect(onUpdateTierField).toHaveBeenCalledWith('c0', 'model', 'new-model')
    app.unmount()
  })

  it('uses one C3 picker for shared fusion and concrete models', async () => {
    const onUpdateTierField = vi.fn()
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        model: 'glm-5.2',
        ensembleEnabled: true,
      }],
      providerOptions: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'openrouter', label: 'OpenRouter' },
      ],
      modelsByProvider: {
        openai: { source: 'live', models: DISCOVERED },
      },
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'deepseek/deepseek-v4-pro',
      ensemblePlanStatus: 'ready',
      routerProviderRoles: { c3: 'dormant_draft' },
    }, { onUpdateTierField })

    const picker = el.querySelector<HTMLInputElement>(
      'input[aria-label="C3 processing mode or model"]',
    )!
    expect(picker.value).toBe('Multi-model fusion')
    expect(picker.getAttribute('aria-describedby'))
      .toContain('setup-tier-c3-ensemble-details')
    expect(el.querySelector('select[aria-label="c3 execution mode"]')).toBeNull()
    expect(el.querySelector('select[aria-label="c3 request entry"]')).toBeNull()
    expect(el.querySelector('[aria-label="c3 request entry is determined by the Multi-model fusion plan"]')
      ?.textContent).toContain('Determined by Multi-model fusion')
    expect(el.textContent).not.toContain('static_tokenrhythm_b5')
    expect(el.querySelector('.setup-tier-table__model-note')).toBeNull()
    expect(el.querySelector('.setup-tier-table__plan-status')).toBeNull()
    const details = el.querySelector<HTMLButtonElement>(
      'button[aria-label="Show C3 fusion details"]',
    )!
    const tooltip = el.querySelector<HTMLElement>('[role="tooltip"]')!
    expect(details.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(tooltip.textContent).toContain('Current fusion plan is ready')
    expect(tooltip.textContent)
      .toContain('Fixed and fallback model: OpenRouter · deepseek/deepseek-v4-pro')
    expect(tooltip.textContent).toContain('image requests still use the Image model configuration')
    expect(details.dataset.open).toBe('false')
    details.parentElement?.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()
    expect(details.dataset.open).toBe('true')
    expect(tooltip.classList.contains('is-open')).toBe(true)
    details.parentElement?.dispatchEvent(new MouseEvent('mouseleave'))
    details.focus()
    await nextTick()
    expect(document.activeElement).toBe(details)
    expect(details.dataset.open).toBe('true')
    details.blur()
    await nextTick()
    expect(details.dataset.open).toBe('false')
    expect(el.textContent).not.toContain('glm-5.2')
    expect(el.querySelector('select[aria-label="c3 thinking level"]')).toBeNull()
    expect(el.querySelector('[aria-label="c3 thinking is determined by the Multi-model fusion plan"]')
      ?.textContent).toContain('Determined by fusion plan')
    const liveStatus = el.querySelector<HTMLElement>('[role="status"][aria-live="polite"]')
    expect(liveStatus?.getAttribute('aria-atomic')).toBe('true')
    expect(liveStatus?.textContent)
      .toContain('Fixed and fallback model: OpenRouter · deepseek/deepseek-v4-pro')

    picker.dispatchEvent(new Event('focus'))
    picker.value = 'alpha'
    picker.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(onUpdateTierField).not.toHaveBeenCalled()
    const modelOption = Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'))
      .find(option => option.textContent?.includes('test-vendor/alpha'))!
    modelOption.click()
    expect(onUpdateTierField).toHaveBeenCalledWith(
      'c3',
      'ensembleEnabled',
      false,
    )
    expect(onUpdateTierField).toHaveBeenCalledWith(
      'c3',
      'ensembleSelectionMode',
      '',
    )
    expect(onUpdateTierField).toHaveBeenCalledWith('c3', 'model', 'test-vendor/alpha')
    app.unmount()
  })

  it('keeps dynamic-member controls visible and labels shared router_dynamic as compatible', async () => {
    const onUpdateTierField = vi.fn()
    const onMigrateLegacyEnsemble = vi.fn()
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        provider: 'tokenrhythm',
        model: 'quality-model',
        thinkingLevel: 'high',
        ensembleEnabled: true,
      }],
      providerOptions: [
        { providerId: 'openrouter', label: 'OpenRouter' },
        { providerId: 'tokenrhythm', label: 'TokenRhythm' },
      ],
      routerProviderRoles: { c3: 'dynamic_member' },
      effectiveEnsembleSelectionMode: 'router_dynamic',
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'fallback-model',
      ensemblePlanStatus: 'ready',
    }, { onUpdateTierField, onMigrateLegacyEnsemble })

    const provider = el.querySelector<HTMLSelectElement>('select[aria-label="c3 request entry"]')!
    const thinking = el.querySelector<HTMLSelectElement>('select[aria-label="c3 thinking level"]')!
    expect(provider.value).toBe('tokenrhythm')
    expect(provider.disabled).toBe(false)
    expect(thinking.value).toBe('high')
    expect(thinking.disabled).toBe(false)
    expect(el.textContent).toContain('previously saved tier-following fusion plan')
    expect(el.textContent).not.toContain('Determined by Multi-model fusion')
    expect(el.textContent).not.toContain('Determined by fusion plan')
    const migrate = el.querySelector<HTMLButtonElement>('[data-testid="tier-ensemble-migrate-legacy"]')!
    expect(migrate.textContent).toContain('Convert to custom lineup')
    migrate.click()
    expect(onMigrateLegacyEnsemble).toHaveBeenCalledOnce()

    thinking.value = 'xhigh'
    thinking.dispatchEvent(new Event('change', { bubbles: true }))
    expect(onUpdateTierField).toHaveBeenCalledWith('c3', 'thinkingLevel', 'xhigh')
    app.unmount()
  })

  it('treats shared C3 as direct when an older Gateway omits provider roles', async () => {
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        provider: 'openai',
        model: 'saved-c3-model',
        thinkingLevel: 'high',
        ensembleEnabled: true,
      }],
      providerOptions: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'openrouter', label: 'OpenRouter' },
      ],
      fixedFallbackProvider: 'OpenAI',
      fixedFallbackModel: 'fallback-model',
      ensemblePlanStatus: 'ready',
      effectiveEnsembleSelectionMode: 'custom_b5',
    })

    expect(el.querySelector<HTMLSelectElement>('select[aria-label="c3 request entry"]')?.value)
      .toBe('openai')
    expect(el.querySelector('select[aria-label="c3 thinking level"]')).toBeNull()
    expect(el.querySelector('[aria-label="c3 thinking is determined by the Multi-model fusion plan"]')
      ?.textContent).toContain('Determined by fusion plan')
    app.unmount()
  })

  it('keeps blocked shared-plan guidance visible instead of hiding it in the tooltip', async () => {
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        ensembleEnabled: true,
      }],
      routerProviderRoles: { c3: 'blocked' },
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'fallback-model',
      ensemblePlanStatus: 'blocked',
    })

    expect(el.querySelector('.setup-tier-table__model-note')?.textContent)
      .toContain('Fixed and fallback model: OpenRouter · fallback-model')
    const status = el.querySelector('.setup-tier-table__plan-status')
    expect(status?.textContent).toContain('Current fusion plan is unavailable')
    expect(status?.classList.contains('is-blocked')).toBe(true)
    app.unmount()
  })

  it('identifies a member credential failure when the fixed fallback remains ready', async () => {
    const { app, el } = await mountTable({
      rows: [{ ...ROWS[1], name: 'c3', ensembleEnabled: true }],
      routerProviderRoles: { c3: 'dormant_draft' },
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'fallback-model',
      ensemblePlanStatus: 'blocked',
      ensemblePlanBlockedReason: 'credential_missing',
      ensembleFixedFallbackReady: true,
    })

    expect(el.querySelector('.setup-tier-table__blocked-reason')?.textContent)
      .toContain('One or more models in this fusion plan are not ready')
    expect(el.querySelector('.setup-tier-table__blocked-reason')?.textContent)
      .not.toContain('Fixed and fallback model is not ready')
    app.unmount()
  })

  it('identifies an unavailable fixed fallback separately from member failures', async () => {
    const { app, el } = await mountTable({
      rows: [{ ...ROWS[1], name: 'c3', ensembleEnabled: true }],
      routerProviderRoles: { c3: 'dormant_draft' },
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'fallback-model',
      ensemblePlanStatus: 'blocked',
      ensemblePlanBlockedReason: 'fixed_fallback:missing_credential:openrouter',
      ensembleFixedFallbackReady: false,
    })

    expect(el.querySelector('.setup-tier-table__blocked-reason')?.textContent)
      .toContain('Fixed and fallback model is not ready')
    app.unmount()
  })

  it('keeps compact shared details unclipped with an offline catalog in readonly mode', async () => {
    const { app, el } = await mountTable({
      readonly: true,
      rows: [{ ...ROWS[1], name: 'c3', ensembleEnabled: true }],
      routerProviderRoles: { c3: 'dormant_draft' },
      modelsByProvider: {},
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'fallback-model',
      ensemblePlanStatus: 'ready',
    })

    expect(el.querySelector('.setup-tier-table')?.classList.contains('setup-tier-table--open')).toBe(true)
    const trigger = el.querySelector<HTMLButtonElement>('[aria-label="Show C3 fusion details"]')!
    expect(trigger).toBeTruthy()
    trigger.dispatchEvent(new Event('focus'))
    await nextTick()
    expect(el.querySelector<HTMLElement>('[role="tooltip"]')?.classList.contains('is-open')).toBe(true)
    app.unmount()
  })

  it('does not claim a pinned legacy C3 profile follows the current shared plan', async () => {
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        model: 'glm-5.2',
        ensembleSelectionMode: 'static_tokenrhythm_b5',
      }],
      providerOptions: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'openrouter', label: 'OpenRouter' },
      ],
    })

    expect(el.textContent).toContain('previously saved compatible fusion plan')
    expect(el.textContent).toContain('uses the Fixed and fallback model')
    expect(el.textContent).not.toContain('glm-5.2')
    expect(el.textContent).not.toContain('static_tokenrhythm_b5')
    const provider = el.querySelector<HTMLSelectElement>('[aria-label="c3 request entry"]')
    expect(provider?.value).toBe('openai')
    expect(el.querySelector('select[aria-label="c3 thinking level"]')).toBeNull()
    expect(el.querySelector('[aria-label="c3 thinking is determined by the Multi-model fusion plan"]')
      ?.textContent).toContain('Determined by fusion plan')
    app.unmount()
  })

  it('uses the fixed fallback when a shared fusion loads a retired error policy', async () => {
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        model: 'sleeping-single-model',
        ensembleEnabled: true,
      }],
      providerOptions: [{ providerId: 'openai', label: 'OpenAI' }],
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'deepseek/deepseek-v4-pro',
    })

    const summary = el.querySelector('.setup-tier-table__model-note')?.textContent || ''
    expect(summary).toContain('uses the Fixed and fallback model')
    expect(summary).not.toContain('returns an error')
    expect(summary).not.toContain('sleeping-single-model')
    app.unmount()
  })

  it('keeps the legacy provider visible while using the fixed fallback', async () => {
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        model: 'legacy-fallback-model',
        ensembleSelectionMode: 'static_tokenrhythm_b5',
      }],
      providerOptions: [
        { providerId: 'openai', label: 'OpenAI' },
        { providerId: 'openrouter', label: 'OpenRouter' },
      ],
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'deepseek/deepseek-v4-pro',
    })

    const summary = el.querySelector('.setup-tier-table__model-note')?.textContent || ''
    expect(summary).toContain('previously saved compatible fusion plan')
    expect(summary).toContain('OpenRouter · deepseek/deepseek-v4-pro')
    expect(summary).not.toContain('legacy-fallback-model')
    expect(el.querySelector<HTMLSelectElement>('[aria-label="c3 request entry"]')?.value)
      .toBe('openai')
    expect(el.querySelector('select[aria-label="c3 thinking level"]')).toBeNull()
    expect(el.querySelector('[aria-label="c3 thinking is determined by the Multi-model fusion plan"]')
      ?.textContent).toContain('Determined by fusion plan')
    app.unmount()
  })

  it('uses tier-following copy only for the legacy router_dynamic mode', async () => {
    const { app, el } = await mountTable({
      rows: [{
        ...ROWS[1],
        name: 'c3',
        model: 'legacy-dynamic-model',
        ensembleSelectionMode: 'router_dynamic',
      }],
      fixedFallbackProvider: 'OpenRouter',
      fixedFallbackModel: 'deepseek/deepseek-v4-pro',
    })

    expect(el.querySelector('.setup-tier-table__model-note')?.textContent)
      .toContain('previously saved tier-following fusion plan')
    expect(el.querySelector('[data-testid="tier-ensemble-migrate-legacy"]')).toBeTruthy()
    expect(el.querySelector<HTMLSelectElement>('select[aria-label="c3 thinking level"]')?.value)
      .toBe(ROWS[1].thinkingLevel)
    app.unmount()
  })

  it('disables only C3 image input while keeping the dedicated image route editable', async () => {
    const { app, el } = await mountTable({
      rows: [
        {
          ...ROWS[1],
          name: 'c3',
          model: 'glm-5.2',
          supportsImage: true,
          ensembleEnabled: true,
        },
        {
          ...ROWS[1],
          name: 'image_model',
          model: 'vision-model',
          supportsImage: true,
        },
      ],
      providerOptions: [{ providerId: 'openai', label: 'OpenAI' }],
    })

    const c3Image = el.querySelector<HTMLInputElement>('input[aria-label="c3 supports image"]')!
    expect(c3Image.checked).toBe(false)
    expect(c3Image.disabled).toBe(true)
    const imageModel = el.querySelector<HTMLInputElement>('input[aria-label="image_model model"]')!
    expect(imageModel.value).toBe('vision-model')
    expect(imageModel.disabled).toBe(false)
    expect(el.querySelector<HTMLSelectElement>('select[aria-label="image_model thinking level"]')?.disabled).toBe(false)
    const imageModelSwitch = el.querySelector<HTMLInputElement>('input[aria-label="image_model supports image"]')!
    expect(imageModelSwitch.checked).toBe(true)
    expect(imageModelSwitch.disabled).toBe(false)
    expect(el.textContent).toContain('image requests still use the Image model configuration')
    app.unmount()
  })

  it('preserves an unknown stored provider as disabled while allowing configured choices', async () => {
    const onUpdateTierField = vi.fn()
    const { app, el } = await mountTable({
      rows: [{ ...ROWS[0], provider: 'private-gateway' }],
      providerOptions: [
        { providerId: 'openrouter', label: 'OpenRouter' },
      ],
    }, { onUpdateTierField })

    const provider = el.querySelector<HTMLSelectElement>('[aria-label="c0 request entry"]')!
    expect(provider.value).toBe('private-gateway')
    expect(provider.disabled).toBe(false)
    expect(Array.from(provider.options).map(option => option.value)).toEqual([
      'openrouter',
      'private-gateway',
    ])
    expect(provider.options[1]?.disabled).toBe(true)
    expect(provider.options[1]?.textContent).toBe('private-gateway (not configured)')
    expect(provider.options[0]?.disabled).toBe(false)
    expect(el.querySelector('[role="table"]')?.classList.contains('setup-tier-table--without-provider'))
      .toBe(false)
    expect(provider.closest('[role="row"]')?.getAttribute('aria-disabled')).toBeNull()
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')?.disabled).toBe(true)
    expect(el.querySelector<HTMLSelectElement>('select[aria-label="c0 thinking level"]')?.disabled).toBe(true)
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 supports image"]')?.disabled).toBe(true)
    provider.value = 'openrouter'
    provider.dispatchEvent(new Event('change', { bubbles: true }))
    expect(onUpdateTierField).toHaveBeenCalledWith('c0', 'provider', 'openrouter')
    app.unmount()
  })

  it('marks a configured tier provider whose credentials are unavailable', async () => {
    const rows = ROWS.map(row => ({ ...row, provider: 'openrouter' }))
    const { app, el } = await mountTable({
      rows,
      providerOptions: [{ providerId: 'openrouter', label: 'OpenRouter' }],
      providerCredentialStatus: [{ provider: 'openrouter', available: false, source: 'none' }],
    })

    expect(el.querySelector('[role="table"]')?.classList.contains('setup-tier-table--without-provider'))
      .toBe(false)
    expect(el.querySelector('[aria-label="c0 request entry"]')?.getAttribute('aria-invalid')).toBe('true')
    expect(el.querySelector('.setup-tier-table__provider-warning')?.textContent)
      .toContain('OpenRouter credentials needed')
    app.unmount()
  })

  it('disables every editable control and marks the table aria-disabled', async () => {
    const { app, el } = await mountTable({ disabled: true })

    expect(el.querySelector('[role="table"]')?.getAttribute('aria-disabled')).toBe('true')
    expect(el.querySelector<HTMLSelectElement>('select[aria-label="c0 request entry"]')?.disabled).toBe(true)
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')?.disabled).toBe(true)
    expect(el.querySelector<HTMLSelectElement>('select[aria-label="c0 thinking level"]')?.disabled).toBe(true)
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 supports image"]')?.disabled).toBe(true)

    app.unmount()
  })
})

describe('SetupTierTable — combobox swap condition', () => {
  it('upgrades the model cell to the combobox only where the tier provider matches the discovery provider', async () => {
    const { app, el } = await mountTable({
      modelsByProvider: {
        openrouter: { models: DISCOVERED, source: 'live' },
      },
    })

    // c0 routes through openrouter (matches) → combobox.
    expect(el.querySelector('input[role="combobox"][aria-label="c0 model"]')).toBeTruthy()
    expect(el.querySelector('input[aria-label="c0 model"]:not([role="combobox"])')).toBeNull()
    // c1 routes through openai (mismatch) → plain free-text input.
    const c1 = el.querySelector<HTMLInputElement>('input[aria-label="c1 model"]')
    expect(c1?.getAttribute('role')).toBeNull()

    app.unmount()
  })

  it('matches tier providers without case or surrounding-whitespace sensitivity', async () => {
    const { app, el } = await mountTable({
      rows: [
        { ...ROWS[0], provider: ' OpenRouter ' },
        ROWS[1],
      ],
      modelsByProvider: {
        openrouter: { models: DISCOVERED, source: 'live' },
      },
    })

    expect(el.querySelector('input[role="combobox"][aria-label="c0 model"]')).toBeTruthy()
    expect(el.querySelector('input[role="combobox"][aria-label="c1 model"]')).toBeNull()
    app.unmount()
  })

  it('uses each row provider catalog independently in a mixed-provider table', async () => {
    const rows = [
      ROWS[0],
      { ...ROWS[1], provider: 'tokenrhythm', model: 'deepseek-v4-pro' },
      { ...ROWS[1], name: 'c2', provider: 'anthropic', model: 'claude-sonnet-4' },
    ]
    const { app, el } = await mountTable({
      rows,
      providerOptions: [
        { providerId: 'openrouter', label: 'OpenRouter' },
        { providerId: 'tokenrhythm', label: 'TokenRhythm' },
        { providerId: 'anthropic', label: 'Anthropic' },
      ],
      modelsByProvider: {
        openrouter: { models: DISCOVERED, source: 'live' },
        tokenrhythm: { models: TOKENRHYTHM_DISCOVERED, source: 'live' },
        anthropic: { models: [], source: 'none' },
      },
    })

    expect(el.querySelector('input[role="combobox"][aria-label="c0 model"]')).toBeTruthy()
    expect(el.querySelector('input[role="combobox"][aria-label="c1 model"]')).toBeTruthy()
    expect(el.querySelector('input[role="combobox"][aria-label="c2 model"]')).toBeNull()
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c2 model"]')?.value).toBe('claude-sonnet-4')

    app.unmount()
  })

  it('preserves the focused input and typed value when an async catalog arrives', async () => {
    const { app, el, modelsByProvider } = await mountTableWithAsyncCatalog()
    const before = el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')!
    before.focus()
    before.value = 'user/model-being-typed'
    before.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()

    modelsByProvider.value = {
      openrouter: { models: DISCOVERED, source: 'live' },
    }
    await nextTick()

    const after = el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')!
    expect(after).toBe(before)
    expect(document.activeElement).toBe(after)
    expect(after.value).toBe('user/model-being-typed')
    expect(after.getAttribute('role')).toBe('combobox')
    app.unmount()
  })

  it('keeps plain inputs when a provider catalog is empty or absent', async () => {
    const none = await mountTable({
      modelsByProvider: { openrouter: { models: [], source: 'none' } },
    })
    expect(none.el.querySelector('input[role="combobox"]')).toBeNull()
    none.app.unmount()

    const absent = await mountTable({ modelsByProvider: {} })
    expect(absent.el.querySelector('input[role="combobox"]')).toBeNull()
    absent.app.unmount()
  })

  it('fails closed to free text when a non-live source unexpectedly includes models', async () => {
    const { app, el } = await mountTable({
      modelsByProvider: {
        openrouter: { models: DISCOVERED, source: 'none' },
      },
    })

    expect(el.querySelector('input[role="combobox"]')).toBeNull()
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')?.value).toBe(
      'deepseek/deepseek-v4-flash',
    )
    app.unmount()
  })

  it('never renders a combobox while the table is disabled', async () => {
    const { app, el } = await mountTable({
      modelsByProvider: { openrouter: { models: DISCOVERED, source: 'live' } },
      disabled: true,
    })
    expect(el.querySelector('input[role="combobox"]')).toBeNull()
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 model"]')?.disabled).toBe(true)
    app.unmount()
  })
})

describe('SetupTierTable — readonly preview mode', () => {
  it('renders model and thinking as text with no editable inputs', async () => {
    const rows = ROWS.map(row => ({ ...row, provider: 'openrouter' }))
    const { app, el } = await mountTable({
      readonly: true,
      rows,
      providerOptions: [{ providerId: 'openrouter', label: 'OpenRouter' }],
    })

    expect(el.querySelector('.setup-tier-table__row.is-head')?.textContent)
      .toContain('Request entry')
    expect(el.querySelector('[aria-label="c0 request entry"]')?.textContent).toBe('openrouter')
    const model = el.querySelector('[aria-label="c0 model"]')
    expect(model?.tagName).toBe('SPAN')
    expect(model?.textContent).toBe('deepseek/deepseek-v4-flash')
    expect(el.querySelector('[aria-label="c0 thinking level"]')?.tagName).toBe('SPAN')
    expect(el.querySelectorAll('select').length).toBe(0)
    expect(el.querySelector('input[role="combobox"]')).toBeNull()
    // The image switch stays visible (disabled) so the preview shows state.
    expect(el.querySelector<HTMLInputElement>('input[aria-label="c0 supports image"]')?.disabled).toBe(true)

    app.unmount()
  })

  it('renders shared C3 fusion semantics without exposing an internal mode', async () => {
    const { app, el } = await mountTable({
      readonly: true,
      rows: [{
        ...ROWS[1],
        name: 'c3',
        model: 'glm-5.2',
        ensembleEnabled: true,
      }],
      routerProviderRoles: { c3: 'dormant_draft' },
    })

    expect(el.querySelector('[aria-label="C3 processing mode or model"]')?.textContent)
      .toContain('Multi-model fusion')
    expect(el.querySelector('.setup-tier-table__model-note')?.textContent)
      .toContain('uses the Fixed and fallback model')
    expect(el.querySelector('[aria-label="c3 thinking is determined by the Multi-model fusion plan"]')
      ?.textContent).toContain('Determined by fusion plan')
    expect(el.textContent).not.toContain('glm-5.2')
    expect(el.textContent).not.toContain('static_tokenrhythm_b5')
    expect(el.querySelector('select[aria-label="c3 execution mode"]')).toBeNull()
    app.unmount()
  })
})
