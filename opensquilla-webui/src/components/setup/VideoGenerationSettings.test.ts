// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import { APP_SETTINGS_KEY, type AppSettings } from '@/modules/appSettings'
import { SETUP_WORKFLOW_KEY, type SetupWorkflow } from '@/modules/setupWorkflow'
import VideoGenerationSettings from './VideoGenerationSettings.vue'

let app: App | undefined

afterEach(() => {
  app?.unmount()
  document.body.innerHTML = ''
})

const videoConfig = {
  enabled: false,
  provider: '',
  primary: '',
  duration_seconds: null,
  max_duration_seconds: 8,
  aspect_ratio: '16:9',
  allowed_aspect_ratios: ['16:9', '9:16'],
  resolution: '720p',
  allowed_resolutions: ['720p', '1080p'],
  providers: {
    openrouter: { base_url: 'https://openrouter.ai/api/v1', api_key_env: 'OPENROUTER_API_KEY' },
    gemini: { base_url: 'https://generativelanguage.googleapis.com/v1beta', api_key_env: 'GEMINI_API_KEY' },
    xai: { base_url: 'https://api.x.ai/v1', api_key_env: 'XAI_API_KEY' },
    qwen: { base_url: 'https://dashscope.aliyuncs.com/api/v1', api_key_env: 'DASHSCOPE_API_KEY' },
    qwen_token_plan: { base_url: 'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1', api_key_env: 'QWEN_TOKEN_PLAN_API_KEY' },
    tokenrhythm: { base_url: 'https://tokenrhythm.studio/v1', api_key_env: 'TOKENRHYTHM_API_KEY' },
  },
}

async function mountSettings(section: unknown = videoConfig, workflow?: Partial<SetupWorkflow>) {
  const readAll = vi.fn().mockResolvedValue(section === null
    ? {}
    : { video_generation: section })
  const patch = vi.fn().mockResolvedValue({ restartRequired: false })
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(VideoGenerationSettings)
  app.use(i18n)
  app.provide(APP_SETTINGS_KEY, { readAll, patch } as unknown as AppSettings)
  if (workflow) app.provide(SETUP_WORKFLOW_KEY, workflow as SetupWorkflow)
  app.mount(el)
  await vi.waitFor(() => expect(
    el.querySelector('[name="setup_video_enabled"]')
    || el.textContent?.includes(i18n.global.t('setup.video.unsupported')),
  ).toBeTruthy())
  await nextTick()
  return { el, readAll, patch }
}

async function input(el: HTMLElement, name: string, value: string) {
  const field = el.querySelector<HTMLInputElement>(`[name="${name}"]`)!
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}

async function selectProvider(el: HTMLElement, provider: string) {
  const field = el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')!
  field.value = provider
  field.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
}

function saveButton(el: HTMLElement): HTMLButtonElement {
  return el.querySelector<HTMLButtonElement>('.video-settings__actions button')!
}

describe('video generation settings', () => {
  it('shows a disclosure chevron beside the status badge', async () => {
    const { el } = await mountSettings()
    const summary = el.querySelector<HTMLElement>('.video-settings__summary')
    expect(summary?.querySelector('.control-pill')).not.toBeNull()
    expect(summary?.querySelector('.video-settings__chevron path')?.getAttribute('d'))
      .toBe('m6 8 4 4 4-4')
  })

  it('describes the switch state without implying that credentials were checked', async () => {
    const { el } = await mountSettings({
      ...videoConfig,
      enabled: true,
      provider: 'openrouter',
      primary: 'google/veo-3.1-fast',
    })
    expect(el.querySelector('.video-settings__summary .control-pill')?.textContent?.trim())
      .toBe(i18n.global.t('setup.video.enabled'))
    expect(el.querySelector('.video-settings__hint')?.textContent)
      .toContain(i18n.global.t('setup.video.credentialVerificationHint'))
    expect(el.querySelector('.video-settings__hint')?.textContent).toContain('OPENROUTER_API_KEY')
  })

  it('loads an optional model duration and patches only changed fields', async () => {
    const { el, patch } = await mountSettings()
    expect(el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')?.value).toBe('')
    expect(el.querySelector('[name="setup_provider_video_primary"]')).toBeNull()
    const duration = el.querySelector<HTMLInputElement>('[name="setup_video_duration"]')
    expect(duration?.value).toBe('')
    expect(duration?.placeholder).toBe('Shortest supported duration')
    expect(el.textContent).toContain(
      "Leave blank to choose the model's shortest supported duration within the configured maximum.",
    )
    el.querySelector<HTMLDetailsElement>('details')!.open = true
    await selectProvider(el, 'openrouter')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.placeholder)
      .toBe('google/veo-3.1-fast')
    expect(el.textContent).toContain(i18n.global.t('setup.video.modelHint'))
    await input(el, 'setup_provider_video_primary', 'google/veo-3.1-fast')
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()

    expect(saveButton(el).disabled).toBe(false)
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()

    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.enabled', value: true },
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
    ])
    expect(el.textContent).toContain(i18n.global.t('setup.video.saved'))
  })

  it('writes null when a configured default duration is cleared', async () => {
    const { el, patch } = await mountSettings({ ...videoConfig, duration_seconds: 8 })
    await input(el, 'setup_video_duration', '')
    saveButton(el).click()
    await Promise.resolve()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.duration_seconds', value: null },
    ])
  })

  it('blocks invalid model and duration limits before RPC', async () => {
    const { el, patch } = await mountSettings()
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(saveButton(el).disabled).toBe(true)
    await selectProvider(el, 'openrouter')
    expect(saveButton(el).disabled).toBe(false)
    await input(el, 'setup_provider_video_primary', 'invalid model')
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_provider_video_primary', 'google/veo-3.1-fast')
    await input(el, 'setup_video_duration', '9')
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps edits visible when a save fails', async () => {
    const { el, patch } = await mountSettings()
    patch.mockRejectedValueOnce(new Error('synthetic save failure'))
    await selectProvider(el, 'openrouter')
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('synthetic save failure')
    expect(saveButton(el).disabled).toBe(false)
    expect(el.textContent).toContain(i18n.global.t('setup.video.unsaved'))
  })

  it('does not offer a save against an older gateway without video config', async () => {
    const { el, patch } = await mountSettings(null)
    expect(el.textContent).toContain(i18n.global.t('setup.video.unsupported'))
    expect(el.querySelector('.video-settings__actions button')).toBeNull()
    expect(patch).not.toHaveBeenCalled()
  })

  it('offers Google Gemini models and saves its raw model ID', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'gemini')
    const models = el.querySelector<HTMLSelectElement>('[name="setup_video_gemini_model"]')!
    const modelHint = models.closest('label')?.querySelector('.control-row__desc')?.textContent
    expect(modelHint).toBe(i18n.global.t('setup.video.geminiModelHint'))
    expect(modelHint).not.toContain('OpenRouter')
    expect(Array.from(models.options, option => option.value)).toEqual([
      '',
      'veo-3.1-fast-generate-preview',
      'veo-3.1-generate-preview',
      'veo-3.1-lite-generate-preview',
    ])
    expect(el.textContent).toContain('GEMINI_API_KEY')
    models.value = 'veo-3.1-fast-generate-preview'
    models.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.enabled', value: true },
      { path: 'video_generation.provider', value: 'gemini' },
      { path: 'video_generation.primary', value: 'veo-3.1-fast-generate-preview' },
    ])
  })

  it('validates Google Gemini duration and 1080p constraints before saving', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'gemini')
    const models = el.querySelector<HTMLSelectElement>('[name="setup_video_gemini_model"]')!
    models.value = 'veo-3.1-fast-generate-preview'
    models.dispatchEvent(new Event('change', { bubbles: true }))
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(saveButton(el).disabled).toBe(false)

    await input(el, 'setup_video_max_duration', '3')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGeminiMaxDuration'))
    await input(el, 'setup_video_max_duration', '8')
    await input(el, 'setup_video_duration', '5')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGeminiDuration'))
    await input(el, 'setup_video_duration', '6')
    expect(saveButton(el).disabled).toBe(false)

    const resolution = el.querySelector<HTMLSelectElement>('[name="setup_video_resolution"]')!
    resolution.value = '1080p'
    resolution.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGemini1080p'))
    await input(el, 'setup_video_duration', '8')
    expect(saveButton(el).disabled).toBe(false)
    await input(el, 'setup_video_max_duration', '7')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGemini1080p'))
    await input(el, 'setup_video_max_duration', '8')
    await input(el, 'setup_video_duration', '')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(saveButton(el).disabled).toBe(false)
    expect(patch).not.toHaveBeenCalled()
  })

  it('validates Gemini settings even while video generation is disabled', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'gemini')
    await input(el, 'setup_video_max_duration', '3')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGeminiMaxDuration'))
    expect(saveButton(el).disabled).toBe(true)

    await input(el, 'setup_video_max_duration', '8')
    await input(el, 'setup_video_duration', '5')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGeminiDuration'))

    await input(el, 'setup_video_duration', '6')
    const resolution = el.querySelector<HTMLSelectElement>('[name="setup_video_resolution"]')!
    resolution.value = '1080p'
    resolution.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidGemini1080p'))
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('does not mark provider-only selection dirty on a gateway without that field', async () => {
    const legacyVideo = Object.fromEntries(
      Object.entries(videoConfig).filter(([key]) => key !== 'provider' && key !== 'providers'),
    )
    const { el, patch } = await mountSettings(legacyVideo)
    await selectProvider(el, 'openrouter')
    expect(saveButton(el).disabled).toBe(true)
    expect(el.textContent).not.toContain(i18n.global.t('setup.video.unsaved'))
    expect(patch).not.toHaveBeenCalled()

    await input(el, 'setup_provider_video_primary', 'google/veo-3.1-fast')
    expect(saveButton(el).disabled).toBe(false)
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
    ])
    expect(el.textContent).not.toContain(i18n.global.t('setup.video.unsaved'))
  })

  it('shows an old raw OpenRouter model without offering unsupported providers', async () => {
    const legacyVideo = Object.fromEntries(
      Object.entries(videoConfig).filter(([key]) => key !== 'provider' && key !== 'providers'),
    )
    const { el, patch } = await mountSettings({ ...legacyVideo, primary: 'google/veo-3.1-fast' })
    expect(el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')?.value)
      .toBe('openrouter')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.value)
      .toBe('google/veo-3.1-fast')
    expect(el.querySelector('option[value="gemini"]')).toBeNull()
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_provider_video_primary', 'google/veo-3.1')
    saveButton(el).click()
    await Promise.resolve()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.primary', value: 'google/veo-3.1' },
    ])
  })

  it('offers every supported video provider with its own model and endpoint settings', async () => {
    const { el } = await mountSettings()
    const options = el.querySelectorAll<HTMLOptionElement>('[name="setup_video_provider"] option')
    expect(Array.from(options, option => option.value)).toEqual([
      '', 'openrouter', 'gemini', 'xai', 'qwen', 'qwen_token_plan', 'tokenrhythm',
    ])

    await selectProvider(el, 'xai')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.placeholder)
      .toBe('grok-imagine-video-1.5')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_base_url"]')?.value)
      .toBe('https://api.x.ai/v1')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key_env"]')?.value)
      .toBe('XAI_API_KEY')

    await selectProvider(el, 'qwen')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.placeholder)
      .toBe('wan2.7-t2v')
    await selectProvider(el, 'qwen_token_plan')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.placeholder)
      .toBe('happyhorse-1.1-t2v')
    await selectProvider(el, 'tokenrhythm')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.placeholder)
      .toBe('wan3.0-video')
  })

  it('saves a custom endpoint only with its own environment key name', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'xai')
    await input(el, 'setup_provider_video_primary', 'grok-imagine-video')
    await input(el, 'setup_video_base_url', 'https://video.example.test/v1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.customEndpointNeedsEnv'))
    expect(saveButton(el).disabled).toBe(true)

    await input(el, 'setup_video_api_key_env', 'VIDEO_PROXY_API_KEY')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'xai' },
      { path: 'video_generation.primary', value: 'grok-imagine-video' },
      { path: 'video_generation.providers.xai.base_url', value: 'https://video.example.test/v1' },
      { path: 'video_generation.providers.xai.api_key_env', value: 'VIDEO_PROXY_API_KEY' },
    ])
  })

  it('keeps URL and key-name drafts separate when switching providers', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'xai')
    await input(el, 'setup_video_base_url', 'https://xai-proxy.example.test/v1')
    await input(el, 'setup_video_api_key_env', 'XAI_PROXY_KEY')
    await selectProvider(el, 'qwen')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_base_url"]')?.value)
      .toBe('https://dashscope.aliyuncs.com/api/v1')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key_env"]')?.value)
      .toBe('DASHSCOPE_API_KEY')
    await selectProvider(el, 'xai')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_base_url"]')?.value)
      .toBe('https://xai-proxy.example.test/v1')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key_env"]')?.value)
      .toBe('XAI_PROXY_KEY')
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'xai' },
      { path: 'video_generation.primary', value: 'grok-imagine-video-1.5' },
      { path: 'video_generation.providers.xai.base_url', value: 'https://xai-proxy.example.test/v1' },
      { path: 'video_generation.providers.xai.api_key_env', value: 'XAI_PROXY_KEY' },
    ])
  })

  it('saves edits to an unselected provider before clearing the dirty state', async () => {
    const { el, patch } = await mountSettings({
      ...videoConfig,
      provider: 'xai',
      primary: 'grok-imagine-video-1.5',
    })
    await input(el, 'setup_video_base_url', 'https://xai-proxy.example.test/v1')
    await input(el, 'setup_video_api_key_env', 'XAI_PROXY_KEY')
    await selectProvider(el, 'qwen')
    expect(saveButton(el).disabled).toBe(false)
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'qwen' },
      { path: 'video_generation.primary', value: 'wan2.7-t2v' },
      { path: 'video_generation.providers.xai.base_url', value: 'https://xai-proxy.example.test/v1' },
      { path: 'video_generation.providers.xai.api_key_env', value: 'XAI_PROXY_KEY' },
    ])
    expect(saveButton(el).disabled).toBe(true)
    expect(el.textContent).not.toContain(i18n.global.t('setup.video.unsaved'))
    await selectProvider(el, 'xai')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_base_url"]')?.value)
      .toBe('https://xai-proxy.example.test/v1')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key_env"]')?.value)
      .toBe('XAI_PROXY_KEY')
  })

  it('labels an invalid endpoint in an unselected provider draft', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'xai')
    await input(el, 'setup_video_base_url', 'http://remote.example.test/v1')
    await selectProvider(el, 'qwen')
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('xAI')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toContain(i18n.global.t('setup.video.invalidBaseUrl'))
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('rejects a video provider endpoint on another provider official origin', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'xai')
    await input(el, 'setup_video_base_url', 'https://openrouter.ai:443/api/v1')
    await input(el, 'setup_video_api_key_env', 'XAI_PROXY_KEY')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidBaseUrl'))
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('blocks a blank endpoint even after switching to another provider', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'xai')
    await input(el, 'setup_video_base_url', '')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidBaseUrl'))
    await selectProvider(el, 'qwen')
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('xAI')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toContain(i18n.global.t('setup.video.invalidBaseUrl'))
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('shows the chosen environment key and suppresses the official key hint for custom URLs', async () => {
    const { el } = await mountSettings()
    await selectProvider(el, 'xai')
    expect(el.querySelector('.video-settings__hint')?.textContent).toContain('XAI_API_KEY')
    await input(el, 'setup_video_base_url', 'https://xai-proxy.example.test/v1')
    expect(el.querySelector('.video-settings__hint')?.textContent)
      .toContain(i18n.global.t('setup.video.customCredentialMissingHint'))
    expect(el.querySelector('.video-settings__hint')?.textContent).not.toContain('XAI_API_KEY')
    await input(el, 'setup_video_api_key_env', 'XAI_PROXY_KEY')
    expect(el.querySelector('.video-settings__hint')?.textContent)
      .toContain(i18n.global.t('setup.video.customCredentialHint', { name: 'XAI_PROXY_KEY' }))
    expect(el.querySelector('.video-settings__hint')?.textContent).not.toContain('XAI_API_KEY')
  })

  it('does not expose newer provider settings to an older gateway', async () => {
    const legacyVideo = Object.fromEntries(
      Object.entries(videoConfig).filter(([key]) => key !== 'providers'),
    )
    const { el } = await mountSettings(legacyVideo)
    expect(el.querySelector('option[value="xai"]')).toBeNull()
    expect(el.querySelector('option[value="qwen"]')).toBeNull()
    expect(el.querySelector('[name="setup_video_base_url"]')).toBeNull()
    expect(el.querySelector('[name="setup_video_api_key_env"]')).toBeNull()
  })

  it('rejects invalid endpoint URLs and environment variable names', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'openrouter')
    await input(el, 'setup_video_base_url', 'http://remote.example.test/v1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidBaseUrl'))
    await input(el, 'setup_video_base_url', 'http://127.evil.example/v1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidBaseUrl'))
    for (const endpoint of [
      'https://openrouter.ai/api/../v1',
      'https://openrouter.ai/api/v1?',
      'https://openrouter.ai/api/v1#',
      'https://openrouter.ai/api/%76%31',
    ]) {
      await input(el, 'setup_video_base_url', endpoint)
      expect(el.querySelector('[role="alert"]')?.textContent)
        .toBe(i18n.global.t('setup.video.invalidBaseUrl'))
    }
    await input(el, 'setup_video_base_url', 'https://openrouter.ai/api/v1')
    await input(el, 'setup_video_api_key_env', 'INVALID-NAME')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidApiKeyEnv'))
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('validates HappyHorse minimum durations for Qwen video models', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'qwen_token_plan')
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    await input(el, 'setup_provider_video_primary', 'happyhorse-1.1-t2v')
    await input(el, 'setup_video_max_duration', '2')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidHappyHorseMaxDuration'))
    await input(el, 'setup_video_max_duration', '8')
    await input(el, 'setup_video_duration', '2')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidHappyHorseDuration'))
    await input(el, 'setup_video_duration', '3')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(saveButton(el).disabled).toBe(false)
    expect(patch).not.toHaveBeenCalled()
  })

  it('prefills the documented TokenRhythm video model so enabling can be saved', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'tokenrhythm')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.value)
      .toBe('wan3.0-video')
    expect(el.querySelector('[data-testid="video-model-status"]')?.textContent)
      .toContain(i18n.global.t('setup.video.tokenrhythmModelUnverified'))
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(saveButton(el).disabled).toBe(false)
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.enabled', value: true },
      { path: 'video_generation.provider', value: 'tokenrhythm' },
      { path: 'video_generation.primary', value: 'wan3.0-video' },
    ])
  })

  it('uses the gateway video catalog while keeping a documented model unverified', async () => {
    const workflow = {
      catalog: vi.fn().mockResolvedValue({
        videoGenerationProviders: [{
          providerId: 'xai', label: 'xAI Video', runtimeSupported: true,
          defaultModel: 'grok-imagine-video-catalog',
          suggestedModels: ['grok-imagine-video-catalog'],
        }],
      }),
      status: vi.fn().mockResolvedValue({}),
      discoverVideoGenerationModels: vi.fn().mockResolvedValue({
        ok: true, providerId: 'tokenrhythm', source: 'documented',
        models: [{ id: 'wan3.0-video', name: 'Wan 3.0', verified: false }],
      }),
    }
    const { el } = await mountSettings(videoConfig, workflow)
    await selectProvider(el, 'xai')
    expect(el.querySelector<HTMLOptionElement>('option[value="xai"]')?.textContent)
      .toBe('xAI Video')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.value)
      .toBe('grok-imagine-video-catalog')
    await selectProvider(el, 'tokenrhythm')
    await vi.waitFor(() => expect(workflow.discoverVideoGenerationModels)
      .toHaveBeenCalledWith('tokenrhythm'))
    expect(el.querySelector('[data-testid="video-model-status"]')?.textContent)
      .toContain(i18n.global.t('setup.video.tokenrhythmModelUnverified'))
  })

  it('sends a pasted video key only on save and never keeps it across provider switches', async () => {
    const { el, patch } = await mountSettings()
    await selectProvider(el, 'tokenrhythm')
    await input(el, 'setup_video_api_key', 'sk-synthetic-video-key')
    expect(el.textContent).not.toContain('sk-synthetic-video-key')
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'tokenrhythm' },
      { path: 'video_generation.primary', value: 'wan3.0-video' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: 'sk-synthetic-video-key' },
    ])
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key"]')?.value).toBe('')
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.directKeyStoredUnverified'))
    await input(el, 'setup_video_api_key', 'sk-unsaved-video-key')
    await selectProvider(el, 'xai')
    await selectProvider(el, 'tokenrhythm')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key"]')?.value).toBe('')
    expect(el.textContent).not.toContain('sk-unsaved-video-key')
  })

  it('shows a reusable image credential for TokenRhythm without asking for another key', async () => {
    const workflow = {
      catalog: vi.fn().mockResolvedValue({}),
      status: vi.fn().mockResolvedValue({
        videoGenerationState: {
          credentialOptions: [{
            providerId: 'tokenrhythm', available: true, source: 'image_direct',
            owner: 'image', envKey: '',
          }],
        },
      }),
    }
    const { el } = await mountSettings(videoConfig, workflow)
    await selectProvider(el, 'tokenrhythm')
    await vi.waitFor(() => expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.imageDirectKeyReuse')))
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key"]')?.value).toBe('')
  })

  it('does not reuse an image credential after the TokenRhythm endpoint changes', async () => {
    const workflow = {
      catalog: vi.fn().mockResolvedValue({}),
      status: vi.fn().mockResolvedValue({
        videoGenerationState: {
          credentialOptions: [{
            providerId: 'tokenrhythm', available: true, source: 'image_direct',
            owner: 'image', envKey: '',
          }],
        },
      }),
    }
    const { el } = await mountSettings(videoConfig, workflow)
    await selectProvider(el, 'tokenrhythm')
    await vi.waitFor(() => expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.imageDirectKeyReuse')))
    await input(el, 'setup_video_base_url', 'https://video-proxy.example.test/v1')
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .not.toContain(i18n.global.t('setup.video.imageDirectKeyReuse'))
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.customEndpointNeedsEnv'))
    await input(el, 'setup_video_api_key', 'sk-proxy-only-key')
    expect(el.querySelector('[role="alert"]')).toBeNull()
  })

  it('requires a replacement key when a provider with a saved direct key changes endpoint', async () => {
    const configured = {
      ...videoConfig,
      provider: 'tokenrhythm',
      primary: 'wan3.0-video',
      providers: {
        ...videoConfig.providers,
        tokenrhythm: { ...videoConfig.providers.tokenrhythm, api_key_configured: true },
      },
    }
    const { el, patch } = await mountSettings(configured)
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.directKeyStoredUnverified'))
    await input(el, 'setup_video_base_url', 'https://video-proxy.example.test/v1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.existingDirectKeyEndpointChanged'))
    await input(el, 'setup_video_api_key', 'sk-replacement-key')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: 'https://video-proxy.example.test/v1' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: 'sk-replacement-key' },
    ])
  })

  it('allows changing the endpoint with a new dedicated environment reference', async () => {
    const configured = {
      ...videoConfig,
      provider: 'tokenrhythm', primary: 'wan3.0-video',
      providers: {
        ...videoConfig.providers,
        tokenrhythm: { ...videoConfig.providers.tokenrhythm, api_key_configured: true },
      },
    }
    const { el, patch } = await mountSettings(configured)
    await input(el, 'setup_video_base_url', 'https://video-proxy.example.test/v1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.existingDirectKeyEndpointChanged'))
    await input(el, 'setup_video_api_key_env', 'VIDEO_PROXY_KEY')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: 'https://video-proxy.example.test/v1' },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'VIDEO_PROXY_KEY' },
    ])
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.customCredentialHint', { name: 'VIDEO_PROXY_KEY' }))
  })

  it('does not call a stored direct key usable when status rejects its binding', async () => {
    const configured = {
      ...videoConfig,
      provider: 'tokenrhythm', primary: 'wan3.0-video',
      providers: {
        ...videoConfig.providers,
        tokenrhythm: { ...videoConfig.providers.tokenrhythm, api_key_configured: true },
      },
    }
    const workflow = {
      catalog: vi.fn().mockResolvedValue({}),
      status: vi.fn().mockResolvedValue({
        videoGenerationState: {
          credentialOptions: [{
            providerId: 'tokenrhythm', available: false, source: 'none', owner: 'none', envKey: '',
          }],
        },
      }),
    }
    const { el } = await mountSettings(configured, workflow)
    await vi.waitFor(() => expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.directKeyStoredUnavailable')))
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .not.toContain(i18n.global.t('setup.video.directKeyConfigured'))
  })

  it('allows switching from a saved direct key to an environment reference', async () => {
    const configured = {
      ...videoConfig,
      provider: 'tokenrhythm', primary: 'wan3.0-video',
      providers: {
        ...videoConfig.providers,
        tokenrhythm: { ...videoConfig.providers.tokenrhythm, api_key_configured: true },
      },
    }
    const status = vi.fn()
      .mockResolvedValueOnce({ videoGenerationState: { credentialOptions: [{
        providerId: 'tokenrhythm', available: true, source: 'video_direct', owner: 'video', envKey: '',
      }] } })
      .mockResolvedValue({ videoGenerationState: { credentialOptions: [{
        providerId: 'tokenrhythm', available: true, source: 'video_env', owner: 'video', envKey: 'VIDEO_KEY',
      }] } })
    const { el, patch } = await mountSettings(configured, {
      catalog: vi.fn().mockResolvedValue({}), status,
    })
    await vi.waitFor(() => expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.directKeyConfigured')))
    await input(el, 'setup_video_api_key_env', 'VIDEO_KEY')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .not.toContain(i18n.global.t('setup.video.directKeyConfigured'))
    saveButton(el).click()
    await vi.waitFor(() => expect(status).toHaveBeenCalledTimes(2))
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'VIDEO_KEY' },
    ])
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.videoEnvKeyAvailable', { name: 'VIDEO_KEY' }))
  })

  it('clears a locally saved direct key only after the explicit clear action is saved', async () => {
    const configured = {
      ...videoConfig,
      provider: 'tokenrhythm', primary: 'wan3.0-video',
      providers: {
        ...videoConfig.providers,
        tokenrhythm: { ...videoConfig.providers.tokenrhythm, api_key_configured: true },
      },
    }
    const status = vi.fn()
      .mockResolvedValueOnce({ videoGenerationState: { credentialOptions: [{
        providerId: 'tokenrhythm', available: true, source: 'video_direct',
        owner: 'video', envKey: '', clearable: true,
      }] } })
      .mockResolvedValue({ videoGenerationState: { credentialOptions: [{
        providerId: 'tokenrhythm', available: false, source: 'none',
        owner: 'none', envKey: '', clearable: false,
      }] } })
    const { el, patch } = await mountSettings(configured, {
      catalog: vi.fn().mockResolvedValue({}), status,
    })
    await vi.waitFor(() => expect(el.querySelector<HTMLButtonElement>('.video-settings__credential-control button'))
      .not.toBeNull())
    await input(el, 'setup_video_api_key', 'sk-unsaved-replacement')
    el.querySelector<HTMLButtonElement>('.video-settings__credential-control button')!.click()
    await nextTick()
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key"]')?.value).toBe('')
    expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.directKeyClearPending'))
    saveButton(el).click()
    await vi.waitFor(() => expect(status).toHaveBeenCalledTimes(2))
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.api_key', value: '' },
    ])
    expect(el.querySelector('.video-settings__credential-control button')).toBeNull()
  })

  it('does not offer a clear action for a video key injected by the Gateway environment', async () => {
    const configured = {
      ...videoConfig,
      provider: 'tokenrhythm', primary: 'wan3.0-video',
      providers: {
        ...videoConfig.providers,
        tokenrhythm: { ...videoConfig.providers.tokenrhythm, api_key_configured: true },
      },
    }
    const { el } = await mountSettings(configured, {
      catalog: vi.fn().mockResolvedValue({}),
      status: vi.fn().mockResolvedValue({ videoGenerationState: { credentialOptions: [{
        providerId: 'tokenrhythm', available: true, source: 'video_env_injected_direct',
        owner: 'video', envKey: 'OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY',
        clearable: false,
      }] } }),
    })
    await vi.waitFor(() => expect(el.querySelector('.video-settings__credential-status')?.textContent)
      .toContain(i18n.global.t('setup.video.directKeyManagedByEnvironment', {
        name: 'OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY',
      })))
    expect(el.querySelector('.video-settings__credential-control button')).toBeNull()
  })

  it('enforces native model IDs and provider video duration ranges', async () => {
    const { el } = await mountSettings()
    await selectProvider(el, 'tokenrhythm')
    const toggle = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
    toggle.checked = true
    toggle.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    await input(el, 'setup_provider_video_primary', 'tokenrhythm/wan3.0-video')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidNativeModel'))
    await input(el, 'setup_provider_video_primary', 'wan3.0-video')
    await input(el, 'setup_video_max_duration', '1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidProviderMaxDuration', { minimum: 2, maximum: 30 }))
    await input(el, 'setup_video_max_duration', '31')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await input(el, 'setup_video_max_duration', '30')
    await input(el, 'setup_video_duration', '1')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidProviderDuration', { minimum: 2, maximum: 30 }))
    await input(el, 'setup_video_duration', '')
    await selectProvider(el, 'qwen')
    await input(el, 'setup_video_max_duration', '16')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await input(el, 'setup_video_duration', '16')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidProviderDuration', { minimum: 2, maximum: 15 }))
    await input(el, 'setup_video_duration', '')
    await selectProvider(el, 'xai')
    await input(el, 'setup_video_max_duration', '60')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await input(el, 'setup_video_duration', '16')
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidProviderDuration', { minimum: 1, maximum: 15 }))
    await input(el, 'setup_video_duration', '')
    await input(el, 'setup_provider_video_primary', 'grok-imagine-video')
    const resolution = el.querySelector<HTMLSelectElement>('[name="setup_video_resolution"]')!
    resolution.value = '1080p'
    resolution.dispatchEvent(new Event('change', { bubbles: true }))
    await nextTick()
    expect(el.querySelector('[role="alert"]')?.textContent)
      .toBe(i18n.global.t('setup.video.invalidXai1080p'))
  })

  it('allows editing a disabled legacy video draft with provider-specific values outside current limits', async () => {
    const disabledTokenRhythm = {
      ...videoConfig,
      provider: 'tokenrhythm', primary: 'wan3.0-video',
      duration_seconds: 1, max_duration_seconds: 1,
    }
    const { el, patch } = await mountSettings(disabledTokenRhythm)
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await input(el, 'setup_video_api_key_env', 'VIDEO_TEST_KEY')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    saveButton(el).click()
    await Promise.resolve()
    await nextTick()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'VIDEO_TEST_KEY' },
    ])
  })
})
