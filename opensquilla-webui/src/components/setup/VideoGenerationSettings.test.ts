// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import { APP_SETTINGS_KEY, type AppSettings } from '@/modules/appSettings'
import { SETUP_WORKFLOW_KEY, type SetupWorkflow } from '@/modules/setupWorkflow'
import VideoGenerationSettings from './VideoGenerationSettings.vue'

let app: App | undefined
afterEach(() => { app?.unmount(); document.body.innerHTML = '' })

const providers = {
  openrouter: { base_url: 'https://openrouter.ai/api/v1', api_key_env: 'OPENROUTER_API_KEY' },
  tokenrhythm: { base_url: 'https://tokenrhythm.studio/v1', api_key_env: 'TOKENRHYTHM_API_KEY' },
  gemini: { base_url: 'https://generativelanguage.googleapis.com/v1beta', api_key_env: 'GEMINI_API_KEY' },
  xai: { base_url: 'https://api.x.ai/v1', api_key_env: 'XAI_API_KEY' },
  qwen: { base_url: 'https://dashscope.aliyuncs.com/api/v1', api_key_env: 'DASHSCOPE_API_KEY' },
  qwen_token_plan: { base_url: 'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1', api_key_env: 'QWEN_TOKEN_PLAN_API_KEY' },
}
const videoConfig = {
  enabled: false, provider: '', primary: '', duration_seconds: null, max_duration_seconds: 8,
  aspect_ratio: '16:9', allowed_aspect_ratios: ['16:9', '9:16'],
  resolution: '720p', allowed_resolutions: ['720p', '1080p'], providers,
}
const selectedConfig = { ...videoConfig, provider: 'tokenrhythm', primary: 'wan3.0-video' }
const directConfig = {
  ...selectedConfig,
  providers: { ...providers, tokenrhythm: { ...providers.tokenrhythm, api_key: '[redacted]' } },
}
function statusResult(options: Record<string, unknown>[]) {
  return { videoGenerationState: { credentialOptions: options } }
}
function credential(source = 'llm_fallback', overrides: Record<string, unknown> = {}) {
  return {
    providerId: 'tokenrhythm', available: true, source, owner: 'primary', envKey: '',
    clearable: source === 'video_direct', baseUrl: providers.tokenrhythm.base_url,
    apiKeyEnvAuthored: false, ...overrides,
  }
}
async function mountSettings(section: unknown = videoConfig, workflow?: Partial<SetupWorkflow>) {
  const readAll = vi.fn().mockResolvedValue(section === null ? {} : { video_generation: section })
  const patch = vi.fn().mockResolvedValue({ restartRequired: false })
  const el = document.createElement('div')
  document.body.appendChild(el)
  app = createApp(VideoGenerationSettings)
  app.use(i18n)
  app.provide(APP_SETTINGS_KEY, { readAll, patch } as unknown as AppSettings)
  if (workflow) app.provide(SETUP_WORKFLOW_KEY, workflow as SetupWorkflow)
  const vm = app.mount(el) as unknown as {
    save(): Promise<boolean>; discard(): void; refreshCredentialStatus(): Promise<void>
  }
  await vi.waitFor(() => expect(el.querySelector('[name="setup_video_enabled"]')
    || el.textContent?.includes(i18n.global.t('setup.video.unsupported'))).toBeTruthy())
  await nextTick()
  return { el, patch, vm }
}
function workflowFor(option: Record<string, unknown>) {
  return { catalog: vi.fn().mockResolvedValue({}), status: vi.fn().mockResolvedValue(statusResult([option])) }
}
async function input(el: HTMLElement, name: string, value: string) {
  const field = el.querySelector<HTMLInputElement>(`[name="${name}"]`)!
  expect(field, name).not.toBeNull()
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
}
async function select(el: HTMLElement, name: string, value: string) {
  const field = el.querySelector<HTMLSelectElement>(`[name="${name}"]`)!
  field.value = value
  field.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
}
async function toggle(el: HTMLElement, enabled: boolean) {
  const field = el.querySelector<HTMLInputElement>('[name="setup_video_enabled"]')!
  field.checked = enabled
  field.dispatchEvent(new Event('change', { bubbles: true }))
  await nextTick()
}
async function editCredential(el: HTMLElement, mode: 'key' | 'env' = 'key') {
  el.querySelector<HTMLButtonElement>('[data-testid="video-edit-credential"]')?.click()
  await nextTick()
  await select(el, 'setup_video_credential_mode', mode)
}
function saveButton(el: HTMLElement) { return el.querySelector<HTMLButtonElement>('.video-settings__actions button')! }
function sourceText(el: HTMLElement) { return el.querySelector('.video-settings__credential-status')?.textContent || '' }
async function restore(el: HTMLElement) {
  el.querySelector<HTMLButtonElement>('[data-testid="video-restore-shared-credential"]')!.click()
  await nextTick()
}

describe('video generation settings', () => {
  it('offers only TokenRhythm and OpenRouter as selectable providers', async () => {
    const { el } = await mountSettings()
    expect(Array.from(el.querySelectorAll<HTMLOptionElement>('[name="setup_video_provider"] option'), option => option.value))
      .toEqual(['', 'openrouter', 'tokenrhythm'])
    expect(el.querySelector('.video-settings__chevron path')).not.toBeNull()
  })

  it('patches only the selected provider, model and enabled state on first setup', async () => {
    const { el, patch, vm } = await mountSettings()
    await select(el, 'setup_video_provider', 'tokenrhythm')
    expect(el.querySelector<HTMLInputElement>('[name="setup_provider_video_primary"]')?.value).toBe('wan3.0-video')
    await toggle(el, true)
    expect(await vm.save()).toBe(true)
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.enabled', value: true },
      { path: 'video_generation.provider', value: 'tokenrhythm' },
      { path: 'video_generation.primary', value: 'wan3.0-video' },
    ])
  })

  it.each([
    ['llm_fallback', 'modelServiceKeyReuse'], ['image_direct', 'imageDirectKeyReuse'],
    ['image_env', 'imageEnvKeyReuse'], ['video_env', 'videoEnvKeyAvailable'],
  ])('shows %s credentials without repeated key or environment inputs', async (source, messageKey) => {
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential(source, { envKey: 'DUMMY_VIDEO_KEY' })))
    await vi.waitFor(() => expect(sourceText(el)).toContain(i18n.global.t(`setup.video.${messageKey}`, { name: 'DUMMY_VIDEO_KEY' })))
    expect(el.querySelector('[name="setup_video_api_key"]')).toBeNull()
    expect(el.querySelector('[name="setup_video_api_key_env"]')).toBeNull()
    await toggle(el, true)
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([{ path: 'video_generation.enabled', value: true }])
  })

  it('supports old gateway credential metadata without endpoint or provenance fields', async () => {
    const { el } = await mountSettings(selectedConfig, workflowFor({
      providerId: 'tokenrhythm', available: true, source: 'image_direct', owner: 'image', envKey: '',
    }))
    await vi.waitFor(() => expect(sourceText(el)).toContain(i18n.global.t('setup.video.imageDirectKeyReuse')))
    expect(el.querySelector('[name="setup_video_api_key"]')).toBeNull()
  })

  it('renders an inherited Provider endpoint without writing schema defaults', async () => {
    const endpoint = 'https://media-provider.example.test/v1'
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential('llm_fallback', {
      baseUrl: endpoint, baseUrlSource: 'profile', baseUrlAuthored: false,
    })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-effective-base-url"]')?.textContent).toBe(endpoint))
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_base_url"]')?.value).toBe(endpoint)
    await input(el, 'setup_video_duration', '6')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([{ path: 'video_generation.duration_seconds', value: 6 }])
  })

  it('binds a new dedicated key to the effective inherited endpoint', async () => {
    const endpoint = 'https://media-provider.example.test/v1'
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential('llm_fallback', { baseUrl: endpoint })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el)
    await input(el, 'setup_video_api_key', 'synthetic-video-key')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: endpoint },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: '' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: 'synthetic-video-key' },
    ])
    expect(el.textContent).not.toContain('synthetic-video-key')
  })

  it('opens one credential input at a time and blank key keeps the saved source', async () => {
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential()))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el)
    expect(el.querySelector('[name="setup_video_api_key"]')).not.toBeNull()
    expect(el.querySelector('[name="setup_video_api_key_env"]')).toBeNull()
    await input(el, 'setup_video_api_key', 'synthetic-unsaved-key')
    await input(el, 'setup_video_api_key', '')
    expect(saveButton(el).disabled).toBe(true)
    await editCredential(el, 'env')
    expect(el.querySelector('[name="setup_video_api_key"]')).toBeNull()
    expect(el.querySelector('[name="setup_video_api_key_env"]')).not.toBeNull()
    expect(el.textContent).not.toContain(i18n.global.t('setup.video.directApiKeyHint'))
    await input(el, 'setup_video_api_key_env', 'DUMMY_DEDICATED_VIDEO_KEY')
    await editCredential(el, 'key')
    expect(saveButton(el).disabled).toBe(true)
    await vm.save()
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps a typed credential bound to the address shown when editing began', async () => {
    const endpoint = 'https://first-provider.example.test/v1'
    const status = vi.fn().mockResolvedValueOnce(statusResult([credential('llm_fallback', { baseUrl: endpoint })]))
      .mockResolvedValue(statusResult([credential('llm_fallback', { baseUrl: 'https://second-provider.example.test/v1' })]))
    const { el, patch, vm } = await mountSettings(selectedConfig, { catalog: vi.fn().mockResolvedValue({}), status })
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el)
    await input(el, 'setup_video_api_key', 'synthetic-address-bound-key')
    await vm.refreshCredentialStatus()
    expect(el.querySelector('[data-testid="video-effective-base-url"]')?.textContent).toBe(endpoint)
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: endpoint },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: '' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: 'synthetic-address-bound-key' },
    ])
  })

  it('keeps an inherited endpoint with an unselected dedicated environment draft', async () => {
    const endpoint = 'https://media-provider.example.test/v1'
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential('llm_fallback', { baseUrl: endpoint })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'DUMMY_PROVIDER_VIDEO_KEY')
    await select(el, 'setup_video_provider', 'openrouter')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
      { path: 'video_generation.providers.tokenrhythm.base_url', value: endpoint },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'DUMMY_PROVIDER_VIDEO_KEY' },
    ])
  })

  it.each(['blank env', 'empty key mode'])('cancels %s after switching providers without authoring an inherited address', async action => {
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential('llm_fallback', {
      baseUrl: 'https://inherited-provider.example.test/v1',
    })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'DUMMY_PENDING_KEY')
    await select(el, 'setup_video_provider', 'openrouter')
    await select(el, 'setup_video_provider', 'tokenrhythm')
    if (action === 'blank env') await input(el, 'setup_video_api_key_env', '')
    else await editCredential(el, 'key')
    expect(saveButton(el).disabled).toBe(true)
    await vm.save()
    expect(patch).not.toHaveBeenCalled()
  })

  it('authors the same environment name as a default and replaces the direct source', async () => {
    const status = vi.fn().mockResolvedValueOnce(statusResult([credential('video_direct')]))
      .mockResolvedValue(statusResult([credential('video_env', { envKey: 'TOKENRHYTHM_API_KEY', apiKeyEnvAuthored: true })]))
    const { el, patch, vm } = await mountSettings(directConfig, { catalog: vi.fn().mockResolvedValue({}), status })
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'TOKENRHYTHM_API_KEY')
    expect(saveButton(el).disabled).toBe(false)
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'TOKENRHYTHM_API_KEY' },
    ])
    await input(el, 'setup_video_base_url', 'https://tokenrhythm.studio/v2')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(await vm.save()).toBe(true)
    expect(patch).toHaveBeenLastCalledWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: 'https://tokenrhythm.studio/v2' },
    ])
  })

  it('allows an explicitly authored default env name for an inherited custom endpoint', async () => {
    const endpoint = 'https://provider-proxy.example.test/v1'
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential('llm_fallback', { baseUrl: endpoint })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'TOKENRHYTHM_API_KEY')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: endpoint },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'TOKENRHYTHM_API_KEY' },
    ])
  })

  it('keeps an environment source draft when switching providers and saves it explicitly', async () => {
    const { el, patch, vm } = await mountSettings(selectedConfig)
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'TOKENRHYTHM_API_KEY')
    await select(el, 'setup_video_provider', 'openrouter')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key"]')?.value).toBe('')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'TOKENRHYTHM_API_KEY' },
    ])
    expect(saveButton(el).disabled).toBe(true)
  })

  it('discards unsaved secrets across provider switches', async () => {
    const { el, patch, vm } = await mountSettings(selectedConfig)
    await input(el, 'setup_video_api_key', 'synthetic-unsaved-key')
    await select(el, 'setup_video_provider', 'openrouter')
    await select(el, 'setup_video_provider', 'tokenrhythm')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_api_key"]')?.value).toBe('')
    expect(el.textContent).not.toContain('synthetic-unsaved-key')
    await vm.save()
    expect(patch).not.toHaveBeenCalled()
  })

  it('restores shared credentials only on Save and confirms the new source afterwards', async () => {
    const status = vi.fn().mockResolvedValueOnce(statusResult([credential('video_direct', { apiKeyEnvAuthored: true })]))
      .mockResolvedValue(statusResult([credential()]))
    const { el, patch, vm } = await mountSettings(directConfig, { catalog: vi.fn().mockResolvedValue({}), status })
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).not.toBeNull())
    await restore(el)
    expect(sourceText(el)).toContain(i18n.global.t('setup.video.sharedCredentialPending'))
    expect(el.querySelector('.video-settings__credential-card')?.classList.contains('is-ready')).toBe(false)
    expect(patch).not.toHaveBeenCalled()
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: '' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: '' },
    ])
    expect(sourceText(el)).toContain(i18n.global.t('setup.video.modelServiceKeyReuse'))
    expect(saveButton(el).disabled).toBe(true)
  })

  it('restores the shared Provider address in the same atomic credential patch', async () => {
    const endpoint = 'https://provider-proxy.example.test/v1'
    const { el, patch, vm } = await mountSettings(directConfig, workflowFor(credential('video_direct', {
      sharedBaseUrl: endpoint, sharedCredentialAvailable: true,
    })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).not.toBeNull())
    await restore(el)
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: endpoint },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: '' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: '' },
    ])
  })

  it('preserves pending restoration on an unselected provider even with an empty saved env name', async () => {
    const config = { ...directConfig, providers: { ...directConfig.providers,
      tokenrhythm: { ...directConfig.providers.tokenrhythm, api_key_env: '' } } }
    const { el, patch, vm } = await mountSettings(config, workflowFor(credential('video_direct')))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).not.toBeNull())
    await restore(el)
    await select(el, 'setup_video_provider', 'openrouter')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: '' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: '' },
    ])
  })

  it('validates restoration to a custom shared endpoint for an unselected provider', async () => {
    const endpoint = 'https://provider-proxy.example.test/v1'
    const { el, patch, vm } = await mountSettings(directConfig, workflowFor(credential('video_direct', {
      sharedBaseUrl: endpoint, sharedCredentialAvailable: true,
    })))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).not.toBeNull())
    await restore(el)
    await select(el, 'setup_video_provider', 'openrouter')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    expect(await vm.save()).toBe(true)
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
      { path: 'video_generation.providers.tokenrhythm.base_url', value: endpoint },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: '' },
      { path: 'video_generation.providers.tokenrhythm.api_key', value: '' },
    ])
  })

  it('cancels restoration and discards credential edits without RPC', async () => {
    const { el, patch, vm } = await mountSettings(directConfig, workflowFor(credential('video_direct')))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).not.toBeNull())
    await restore(el)
    await restore(el)
    expect(saveButton(el).disabled).toBe(true)
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'DUMMY_EDITED_KEY')
    vm.discard()
    await nextTick()
    expect(el.querySelector('[name="setup_video_api_key_env"]')).toBeNull()
    expect(saveButton(el).disabled).toBe(true)
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps failed credential changes visible and does not lose restoration intent', async () => {
    const { el, patch, vm } = await mountSettings(directConfig, workflowFor(credential('video_direct')))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).not.toBeNull())
    patch.mockRejectedValueOnce(new Error('synthetic save failure'))
    await restore(el)
    expect(await vm.save()).toBe(false)
    expect(el.querySelector('[role="alert"]')?.textContent).toBe('synthetic save failure')
    expect(saveButton(el).disabled).toBe(false)
    await vm.save()
    expect(patch).toHaveBeenCalledTimes(2)
  })

  it('does not offer restoration for an environment injected direct key, including draft URL edits', async () => {
    const { el } = await mountSettings(directConfig, workflowFor(credential('video_env_injected_direct', {
      envKey: 'OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY', clearable: false,
    })))
    await vi.waitFor(() => expect(sourceText(el)).toContain(i18n.global.t('setup.video.directKeyManagedByEnvironment', {
      name: 'OPENSQUILLA_VIDEO_GENERATION_PROVIDERS__TOKENRHYTHM__API_KEY',
    })))
    expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).toBeNull()
    await input(el, 'setup_video_base_url', 'https://different-proxy.example.test/v1')
    expect(el.querySelector('[data-testid="video-restore-shared-credential"]')).toBeNull()
  })

  it('does not reuse confirmed credentials for an edited endpoint on another origin', async () => {
    const { el, patch, vm } = await mountSettings(selectedConfig, workflowFor(credential()))
    await vi.waitFor(() => expect(el.querySelector('[name="setup_video_api_key"]')).toBeNull())
    await input(el, 'setup_video_base_url', 'https://other-proxy.example.test/v1')
    expect(sourceText(el)).not.toContain(i18n.global.t('setup.video.modelServiceKeyReuse'))
    expect(el.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('setup.video.customEndpointNeedsEnv'))
    expect(await vm.save()).toBe(false)
    expect(patch).not.toHaveBeenCalled()
    await input(el, 'setup_video_api_key', 'synthetic-endpoint-key')
    expect(el.querySelector('[role="alert"]')).toBeNull()
  })

  it('requires a new key or dedicated env when changing a saved direct key endpoint', async () => {
    const { el, patch, vm } = await mountSettings(directConfig, workflowFor(credential('video_direct')))
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-edit-credential"]')).not.toBeNull())
    await input(el, 'setup_video_base_url', 'https://video-proxy.example.test/v1')
    expect(el.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('setup.video.existingDirectKeyEndpointChanged'))
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'DUMMY_PROXY_KEY')
    expect(el.querySelector('[role="alert"]')).toBeNull()
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.providers.tokenrhythm.base_url', value: 'https://video-proxy.example.test/v1' },
      { path: 'video_generation.providers.tokenrhythm.api_key_env', value: 'DUMMY_PROXY_KEY' },
    ])
  })

  it('reports unavailable stored keys honestly when Gateway status rejects the binding', async () => {
    const { el } = await mountSettings(directConfig, workflowFor(credential('none', { available: false, clearable: false })))
    await vi.waitFor(() => expect(sourceText(el)).toContain(i18n.global.t('setup.video.directKeyStoredUnavailable')))
    expect(el.querySelector('[name="setup_video_api_key"]')).not.toBeNull()
  })

  it('does not accept a late older credential status over a newer refresh', async () => {
    let resolveFirst!: (result: unknown) => void
    const status = vi.fn().mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve }))
      .mockResolvedValue(statusResult([credential('none', { available: false })]))
    const { el, vm } = await mountSettings(selectedConfig, { catalog: vi.fn().mockResolvedValue({}), status })
    await vi.waitFor(() => expect(status).toHaveBeenCalledOnce())
    await vm.refreshCredentialStatus()
    resolveFirst(statusResult([credential()]))
    await nextTick()
    await Promise.resolve()
    expect(sourceText(el)).not.toContain(i18n.global.t('setup.video.modelServiceKeyReuse'))
    expect(el.querySelector('[name="setup_video_api_key"]')).not.toBeNull()
  })

  it.each([
    ['gemini', 'veo-3.1-generate-preview'], ['xai', 'grok-imagine-video-1.5'],
    ['qwen', 'wan2.7-t2v'], ['qwen_token_plan', 'happyhorse-1.1-t2v'],
  ])('preserves legacy %s routes while saving unrelated settings and disabling', async (provider, primary) => {
    const { el, patch, vm } = await mountSettings({ ...videoConfig, enabled: true, provider, primary })
    const existing = el.querySelector<HTMLOptionElement>(`[name="setup_video_provider"] option[value="${provider}"]`)
    expect(existing?.disabled).toBe(true)
    expect(el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')?.value).toBe(provider)
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_legacy_model"]')?.value).toBe(primary)
    expect(el.querySelector('[data-testid="video-legacy-provider-notice"]')).not.toBeNull()
    await input(el, 'setup_video_duration', '6')
    await vm.save()
    expect(patch).toHaveBeenLastCalledWith([{ path: 'video_generation.duration_seconds', value: 6 }])
    await toggle(el, false)
    await vm.save()
    expect(patch).toHaveBeenLastCalledWith([{ path: 'video_generation.enabled', value: false }])
  })

  it('replaces a legacy provider only after explicit selection and restores it on discard', async () => {
    const { el, patch, vm } = await mountSettings({ ...videoConfig, provider: 'xai', primary: 'grok-imagine-video-1.5' })
    await select(el, 'setup_video_provider', 'tokenrhythm')
    expect(el.querySelector('[data-testid="video-legacy-provider-notice"]')).toBeNull()
    vm.discard()
    await nextTick()
    expect(el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')?.value).toBe('xai')
    await select(el, 'setup_video_provider', 'openrouter')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' },
    ])
  })

  it('preserves a known legacy route even when the older Gateway omits provider settings', async () => {
    const legacy = Object.fromEntries(Object.entries({ ...videoConfig, enabled: true,
      provider: 'xai', primary: 'grok-imagine-video-1.5' }).filter(([key]) => key !== 'providers'))
    const { el, patch, vm } = await mountSettings(legacy)
    expect(el.querySelector<HTMLSelectElement>('[name="setup_video_provider"]')?.value).toBe('xai')
    expect(el.querySelector<HTMLInputElement>('[name="setup_video_legacy_model"]')?.value).toBe('grok-imagine-video-1.5')
    await toggle(el, false)
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([{ path: 'video_generation.enabled', value: false }])
  })

  it('keeps older OpenRouter-only gateways editable without unsupported fields', async () => {
    const legacy = Object.fromEntries(Object.entries(videoConfig).filter(([key]) => key !== 'provider' && key !== 'providers'))
    const { el, patch, vm } = await mountSettings(legacy)
    expect(el.querySelector('option[value="tokenrhythm"]')).toBeNull()
    await select(el, 'setup_video_provider', 'openrouter')
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_provider_video_primary', 'google/veo-3.1-fast')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([{ path: 'video_generation.primary', value: 'google/veo-3.1-fast' }])
  })

  it('does not offer saving when Gateway has no video section', async () => {
    const { el, patch } = await mountSettings(null)
    expect(el.textContent).toContain(i18n.global.t('setup.video.unsupported'))
    expect(el.querySelector('.video-settings__actions button')).toBeNull()
    expect(patch).not.toHaveBeenCalled()
  })

  it('validates OpenRouter models, duration bounds and clearing the optional duration', async () => {
    const { el, patch, vm } = await mountSettings({ ...videoConfig, duration_seconds: 8 })
    await toggle(el, true)
    expect(saveButton(el).disabled).toBe(true)
    await select(el, 'setup_video_provider', 'openrouter')
    await input(el, 'setup_provider_video_primary', 'invalid model')
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_provider_video_primary', 'google/veo-3.1-fast')
    await input(el, 'setup_video_duration', '9')
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_video_duration', '')
    await vm.save()
    expect(patch).toHaveBeenCalledExactlyOnceWith([
      { path: 'video_generation.enabled', value: true }, { path: 'video_generation.provider', value: 'openrouter' },
      { path: 'video_generation.primary', value: 'google/veo-3.1-fast' }, { path: 'video_generation.duration_seconds', value: null },
    ])
  })

  it('validates TokenRhythm native models and duration limits', async () => {
    const { el } = await mountSettings({ ...selectedConfig, enabled: true })
    await input(el, 'setup_provider_video_primary', 'tokenrhythm/wan3.0-video')
    expect(el.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('setup.video.invalidNativeModel'))
    await input(el, 'setup_provider_video_primary', 'wan3.0-video')
    await input(el, 'setup_video_max_duration', '1')
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_video_max_duration', '30')
    await input(el, 'setup_video_duration', '31')
    expect(saveButton(el).disabled).toBe(true)
    await input(el, 'setup_video_duration', '2')
    expect(el.querySelector('[role="alert"]')).toBeNull()
  })

  it('rejects unsafe and conflicting endpoint URLs and invalid env names before RPC', async () => {
    const { el, patch, vm } = await mountSettings(selectedConfig)
    for (const endpoint of ['http://remote.example.test/v1', 'https://tokenrhythm.studio/v1?',
      'https://tokenrhythm.studio/api/../v1', 'https://tokenrhythm.studio/%76%31', 'https://openrouter.ai/api/v1']) {
      await input(el, 'setup_video_base_url', endpoint)
      expect(el.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('setup.video.invalidBaseUrl'))
    }
    await input(el, 'setup_video_base_url', providers.tokenrhythm.base_url)
    await editCredential(el, 'env')
    await input(el, 'setup_video_api_key_env', 'INVALID-NAME')
    expect(el.querySelector('[role="alert"]')?.textContent).toBe(i18n.global.t('setup.video.invalidApiKeyEnv'))
    expect(await vm.save()).toBe(false)
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps invalid connection edits isolated and visible after switching providers', async () => {
    const { el, patch, vm } = await mountSettings(selectedConfig)
    await input(el, 'setup_video_base_url', 'http://remote.example.test/v1')
    await select(el, 'setup_video_provider', 'openrouter')
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('TokenRhythm')
    expect(await vm.save()).toBe(false)
    expect(patch).not.toHaveBeenCalled()
    vm.discard()
    await nextTick()
    expect(el.querySelector('[role="alert"]')).toBeNull()
  })

  it('uses Gateway catalog and discovery while retaining documented model verification', async () => {
    const { el } = await mountSettings(selectedConfig, {
      catalog: vi.fn().mockResolvedValue({ videoGenerationProviders: [{
        providerId: 'tokenrhythm', label: 'TokenRhythm', defaultModel: 'wan3.0-video',
        suggestedModels: ['wan3.0-video'], defaultBaseUrl: providers.tokenrhythm.base_url, envKey: 'TOKENRHYTHM_API_KEY',
      }] }),
      status: vi.fn().mockResolvedValue({}),
      discoverVideoGenerationModels: vi.fn().mockResolvedValue({ ok: true, source: 'live', models: [{ id: 'wan3.0-video', verified: true }] }),
    })
    await vi.waitFor(() => expect(el.querySelector('[data-testid="video-model-status"]')?.textContent)
      .toContain(i18n.global.t('setup.video.tokenrhythmModelListed')))
  })
})
