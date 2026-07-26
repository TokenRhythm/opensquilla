import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import {
  PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS,
  useSetupProviderForm,
  buildProviderPayload,
  hasEffectiveProvider,
} from './useSetupProviderForm'

// The connection state machine talks to the gateway through the rpc store —
// stub it at the module seam (the pattern useSetupCatalog tests use).
const { callMock } = vi.hoisted(() => ({ callMock: vi.fn() }))
vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: callMock }),
}))

beforeEach(() => {
  callMock.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('buildProviderPayload', () => {
  it('camel-cases keys and drops empty values', () => {
    expect(buildProviderPayload('openrouter', { api_key: 'k', api_key_env: '', model: 'm/x' }))
      .toEqual({ providerId: 'openrouter', apiKey: 'k', model: 'm/x' })
  })
})

describe('hasEffectiveProvider', () => {
  it('accepts runtime env-backed providers even before a config file is persisted', () => {
    expect(hasEffectiveProvider(
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
      { hasConfig: false, llmConfigured: true, llmSource: 'env' },
    )).toBe(true)
  })

  it('does not accept a missing env key as a usable provider', () => {
    expect(hasEffectiveProvider(
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
      { hasConfig: false, llmConfigured: false, llmSource: 'missing_env' },
    )).toBe(false)
  })

  it('treats llmConfigured=false as authoritative even with an env source', () => {
    expect(hasEffectiveProvider(
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
      { hasConfig: false, llmConfigured: false, llmSource: 'env' },
    )).toBe(false)
  })
})

// Regression: the gateway rejects a provider save that carries BOTH a pasted
// api_key and an api_key_env reference ("configure either api_key or
// api_key_env, not both"). The env field is frequently pre-filled from a
// detected variable, so the form must keep the two mutually exclusive.
describe('useSetupProviderForm — runtime provider hydration', () => {
  it('hydrates the selected provider from env-backed runtime config', () => {
    const f = useSetupProviderForm()
    f.initFromConfig(
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro', api_key_env: 'OPENROUTER_API_KEY' },
      { hasConfig: false, llmConfigured: true, llmSource: 'env' },
      [{ providerId: 'openrouter', fields: [{ name: 'model', label: 'Model' }] }],
    )

    expect(f.selectedProvider.value).toBe('openrouter')
  })

  it('submits the registry env default shown for a fresh provider draft', () => {
    const f = useSetupProviderForm()
    const envField = {
      name: 'api_key_env',
      label: 'API key env',
      default: 'DEEPSEEK_API_KEY',
    }

    f.selectProvider('deepseek')
    f.resetForProvider({ fields: [envField] })

    expect(f.fieldValue(envField, {})).toBe('DEEPSEEK_API_KEY')
    expect(f.payload()).toEqual({
      providerId: 'deepseek',
      apiKeyEnv: 'DEEPSEEK_API_KEY',
    })
  })

  it('hydrates saved profile endpoint fields without exposing secret fields', () => {
    const f = useSetupProviderForm()

    f.initStoredProfile('custom', {
      base_url: 'https://llm.example.test/v1',
      proxy: 'http://proxy.example.test:8080',
      api_key: '[redacted]',
      api_key_env: 'CUSTOM_API_KEY',
    })

    expect(f.providerFieldValues.value).toEqual({
      base_url: 'https://llm.example.test/v1',
      proxy: 'http://proxy.example.test:8080',
    })
    expect(f.payload()).toEqual({
      providerId: 'custom',
      baseUrl: 'https://llm.example.test/v1',
      proxy: 'http://proxy.example.test:8080',
    })
    expect(f.isDirty.value).toBe(false)
  })
})

describe('useSetupProviderForm — api_key / api_key_env are mutually exclusive', () => {
  it('pasting an api_key clears a pre-filled api_key_env', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY') // pre-filled from env
    f.updateField('api_key', 'sk-pasted') // user pastes a key
    const p = f.payload()
    expect(p.apiKey).toBe('sk-pasted')
    expect(p.apiKeyEnv).toBeUndefined()
  })

  it('setting api_key_env clears a previously pasted api_key', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.updateField('api_key', 'sk-pasted')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY')
    const p = f.payload()
    expect(p.apiKeyEnv).toBe('OPENROUTER_API_KEY')
    expect(p.apiKey).toBeUndefined()
  })

  it('env-only config submits just the env reference', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY')
    const p = f.payload()
    expect(p.apiKeyEnv).toBe('OPENROUTER_API_KEY')
    expect(p.apiKey).toBeUndefined()
  })

  it('a whitespace-only api_key does not count as a credential', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY')
    f.updateField('api_key', '   ')
    const p = f.payload()
    // blank paste must not clear the env reference
    expect(p.apiKeyEnv).toBe('OPENROUTER_API_KEY')
  })
})

describe('useSetupProviderForm — provider credential state', () => {
  it('keeps saved credentials when not replacing the key', () => {
    const f = useSetupProviderForm()
    f.initFromConfig(
      { provider: 'openrouter', model: 'm', api_key_env: 'OPENROUTER_API_KEY' },
      { hasConfig: true, llmConfigured: true, llmSource: 'explicit' },
      [{ providerId: 'openrouter', fields: [{ name: 'model', label: 'Model' }] }],
    )

    expect(f.payload()).toEqual({ providerId: 'openrouter', model: 'm' })
  })

  it('rebuilds from scratch on initFromConfig and drops stale credential edits', () => {
    const f = useSetupProviderForm()
    const spec = [{ providerId: 'openrouter', fields: [{ name: 'model', label: 'Model' }] }]
    const config = { provider: 'openrouter', model: 'm' }
    const status = { hasConfig: true, llmConfigured: true, llmSource: 'explicit' }

    f.selectProvider('openrouter')
    f.updateField('api_key', 'sk-pasted')
    f.initFromConfig(config, status, spec)

    expect(f.selectedProvider.value).toBe('openrouter')
    expect(f.payload()).toEqual({ providerId: 'openrouter', model: 'm' })
    expect(f.isDirty.value).toBe(false)

    f.updateField('api_key_env', 'OPENROUTER_API_KEY')
    f.initFromConfig(config, status, spec)

    expect(f.selectedProvider.value).toBe('openrouter')
    expect(f.payload()).toEqual({ providerId: 'openrouter', model: 'm' })
    expect(f.isDirty.value).toBe(false)
  })

  it('clears stale provider selection when initFromConfig has no effective provider', () => {
    const f = useSetupProviderForm()
    const spec = [{ providerId: 'openrouter', fields: [{ name: 'model', label: 'Model' }] }]

    f.selectProvider('openrouter')
    f.updateField('api_key', 'sk-pasted')
    f.initFromConfig(
      { provider: 'openrouter', model: 'm' },
      { hasConfig: false, llmConfigured: false, llmSource: 'missing_env' },
      spec,
    )

    expect(f.selectedProvider.value).toBe('')
    expect(f.isDirty.value).toBe(false)
  })

  it('pasted replacement key clears the env reference in the save payload', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY')
    f.startCredentialReplace()
    f.updateField('api_key', 'sk-pasted')

    expect(f.payload()).toEqual({ providerId: 'openrouter', apiKey: 'sk-pasted' })
  })

  it('explicit env source clears the pasted key in the save payload', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.startCredentialReplace()
    f.updateField('api_key', 'sk-pasted')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY')

    expect(f.payload()).toEqual({ providerId: 'openrouter', apiKeyEnv: 'OPENROUTER_API_KEY' })
  })

  it('startCredentialReplace clears previous reveal state and marks replacement mode', () => {
    const f = useSetupProviderForm()
    f.setRevealedCredential('shown-key')
    f.setRevealError('failed')

    f.startCredentialReplace()

    expect(f.replacingCredential.value).toBe(true)
    expect(f.revealedCredential.value).toBe('')
    expect(f.revealError.value).toBe('')
  })

  it('cancelCredentialReplace clears api_key but leaves api_key_env intact', () => {
    const f = useSetupProviderForm()
    f.selectProvider('openrouter')
    f.updateField('api_key_env', 'OPENROUTER_API_KEY')
    f.startCredentialReplace()
    f.cancelCredentialReplace()

    expect(f.replacingCredential.value).toBe(false)
    expect(f.providerFieldValues.value.api_key).toBe('')
    expect(f.providerFieldValues.value.api_key_env).toBe('OPENROUTER_API_KEY')
  })

  it('setRevealedCredential and setRevealError clear each other', () => {
    const f = useSetupProviderForm()

    f.setRevealError('failed')
    expect(f.revealedCredential.value).toBe('')
    expect(f.revealError.value).toBe('failed')

    f.setRevealedCredential('shown-key')
    expect(f.revealedCredential.value).toBe('shown-key')
    expect(f.revealError.value).toBe('')
  })

  it('expires revealed credentials after a limited display window', () => {
    vi.useFakeTimers()
    const f = useSetupProviderForm()

    f.setRevealedCredential('shown-key')

    expect(f.revealedCredential.value).toBe('shown-key')

    vi.advanceTimersByTime(PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS - 1)
    expect(f.revealedCredential.value).toBe('shown-key')

    vi.advanceTimersByTime(1)
    expect(f.revealedCredential.value).toBe('')
  })

  it('hides revealed credentials immediately and cancels the pending expiry', () => {
    vi.useFakeTimers()
    const f = useSetupProviderForm()

    f.setRevealedCredential('shown-key')
    vi.advanceTimersByTime(PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS / 2)
    f.hideRevealedCredential()
    expect(f.revealedCredential.value).toBe('')

    f.setRevealedCredential('shown-again')
    vi.advanceTimersByTime(PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS / 2)
    expect(f.revealedCredential.value).toBe('shown-again')
  })

  it('clears revealed credentials when credential inputs change', () => {
    const f = useSetupProviderForm()
    f.setRevealedCredential('shown-key')

    f.updateField('api_key_env', 'OPENROUTER_API_KEY')

    expect(f.revealedCredential.value).toBe('')
    expect(f.revealError.value).toBe('')

    f.setRevealedCredential('shown-key')
    f.updateField('api_key', 'sk-next')

    expect(f.revealedCredential.value).toBe('')
    expect(f.revealError.value).toBe('')
  })
})

// ---------------------------------------------------------------------------
// Connection state machine — probe + model discovery
// ---------------------------------------------------------------------------

const PROBE_OK = {
  ok: true,
  providerId: 'openai',
  model: 'test-model',
  failureKind: '',
  message: '',
  code: '',
  firstResponseMs: 123,
  totalMs: 412,
}

const DISCOVER_ROW = {
  id: 'test-vendor/test-model',
  name: 'Test Model',
  contextWindow: 262144,
  maxOutputTokens: 16384,
  capabilities: ['chat', 'tools'],
  pricing: null,
  capabilitySource: 'synthesized',
}

const DISCOVER_OK = { ok: true, failureKind: '', detail: '', source: 'live', models: [DISCOVER_ROW] }

function mockRpc(responses: { probe?: unknown; discover?: unknown } = {}) {
  callMock.mockImplementation(async (method: string) => {
    if (method === 'onboarding.provider.probe') return responses.probe ?? PROBE_OK
    if (method === 'onboarding.models.discover') return responses.discover ?? DISCOVER_OK
    throw new Error(`unexpected rpc method: ${method}`)
  })
}

describe('useSetupProviderForm — connection state machine', () => {
  it('starts unconfigured and moves to unverified when a provider is selected', () => {
    const f = useSetupProviderForm()
    expect(f.connection.value.phase).toBe('unconfigured')
    f.selectProvider('openai')
    expect(f.connection.value.phase).toBe('unverified')
  })

  it('probe ok goes probing → verified and auto-discovers models', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    f.updateField('api_key', 'sk-unsaved')
    f.updateField('model', 'test-model')

    const pending = f.probeConnection()
    expect(f.connection.value.phase).toBe('probing')
    await pending

    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.firstResponseMs).toBe(123)
    expect(f.connection.value.totalMs).toBe(412)
    expect(f.connection.value.latencyMs).toBeNull()
    expect(f.connection.value.modelSource).toBe('live')
    expect(f.connection.value.models).toHaveLength(1)
    expect(f.connection.value.models[0].id).toBe('test-vendor/test-model')
    expect(f.connection.value.discoverError).toBe('')
    expect(callMock).toHaveBeenCalledTimes(2)
  })

  it('sends the CURRENT unsaved form values and falls back to the default model', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    f.updateField('api_key', 'sk-unsaved')

    await f.probeConnection({ defaultModel: 'test-default-model' })

    expect(callMock).toHaveBeenNthCalledWith(1, 'onboarding.provider.probe', {
      providerId: 'openai',
      apiKey: 'sk-unsaved',
      model: 'test-default-model',
    })
    // discover ignores the model but reuses the same candidate credentials
    expect(callMock).toHaveBeenNthCalledWith(2, 'onboarding.models.discover', {
      providerId: 'openai',
      apiKey: 'sk-unsaved',
    })
  })

  it('lets a canonical model override replace the form model for a probe', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    f.updateField('model', 'stale-provider-form-model')

    await f.probeConnection({
      defaultModel: 'fallback-model',
      modelOverride: 'canonical-fixed-model',
    })

    expect(callMock).toHaveBeenNthCalledWith(1, 'onboarding.provider.probe', {
      providerId: 'openai',
      model: 'canonical-fixed-model',
    })
    expect(callMock).toHaveBeenNthCalledWith(2, 'onboarding.models.discover', {
      providerId: 'openai',
      model: 'canonical-fixed-model',
    })
  })

  it('classifies auth_invalid as key_invalid', async () => {
    mockRpc({
      probe: {
        ok: false,
        failureKind: 'auth_invalid',
        message: 'No API key available.',
        firstResponseMs: 25,
        totalMs: 87,
      },
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection()

    expect(f.connection.value.phase).toBe('key_invalid')
    expect(f.connection.value.failureKind).toBe('auth_invalid')
    expect(f.connection.value.detail).toBe('No API key available.')
    expect(f.connection.value.firstResponseMs).toBe(25)
    expect(f.connection.value.totalMs).toBe(87) // failures keep reported probe timings too
    expect(callMock).toHaveBeenCalledTimes(1) // no discover after a failed probe
  })

  it('classifies non-auth failures as unreachable', async () => {
    mockRpc({ probe: { ok: false, failureKind: 'transport_transient', message: 'connect timeout' } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection()

    expect(f.connection.value.phase).toBe('unreachable')
    expect(f.connection.value.failureKind).toBe('transport_transient')
  })

  it('normalizes missing or bogus explicit probe timings to null', async () => {
    mockRpc({ probe: { ok: true, firstResponseMs: -1, totalMs: Number.NaN } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })

    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBeNull()
  })

  it('falls back to a legacy gateway latency as complete probe duration only', async () => {
    mockRpc({ probe: { ok: true, latencyMs: 412 } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })

    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBe(412)
    expect(f.connection.value.latencyMs).toBe(412)
  })

  it('accepts legitimate 0ms values in the new explicit timing fields', async () => {
    mockRpc({ probe: { ok: true, firstResponseMs: 0, totalMs: 0 } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })

    expect(f.connection.value.firstResponseMs).toBe(0)
    expect(f.connection.value.totalMs).toBe(0)
  })

  it('hides a failure totalMs=0 sentinel when no model response was observed', async () => {
    mockRpc({
      probe: {
        ok: false,
        failureKind: 'auth_invalid',
        message: 'No API key available.',
        firstResponseMs: null,
        totalMs: 0,
      },
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection()

    expect(f.connection.value.phase).toBe('key_invalid')
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBeNull()
  })

  it('treats legacy latencyMs=0 as the never-reached-network sentinel', async () => {
    // The gateway sends latencyMs=0 when the call never hit the network (missing
    // key / build failure); it must not render as a bogus "· 0ms" pill.
    mockRpc({ probe: { ok: false, failureKind: 'auth_invalid', message: 'No API key available.', latencyMs: 0 } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection()

    expect(f.connection.value.phase).toBe('key_invalid')
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBeNull()
    expect(f.connection.value.latencyMs).toBeNull()
  })

  it('maps a thrown RPC error to unreachable with the message as detail', async () => {
    callMock.mockRejectedValue(new Error('gateway offline'))
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection()

    expect(f.connection.value.phase).toBe('unreachable')
    expect(f.connection.value.detail).toBe('gateway offline')
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBeNull() // no model probe completed
  })

  it('a credential edit resets a verified connection to unverified and clears models', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    f.updateField('api_key', 'sk-first')
    await f.probeConnection({ defaultModel: 'm' })
    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.firstResponseMs).toBe(123)
    expect(f.connection.value.totalMs).toBe(412)

    f.updateField('api_key', 'sk-second')
    expect(f.connection.value.phase).toBe('unverified')
    expect(f.connection.value.models).toEqual([])
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBeNull() // stale verdicts drop their timings too
  })

  it('keeps the discovered catalog when a model choice invalidates the probe verdict', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })
    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.models).toEqual([expect.objectContaining({ id: 'test-vendor/test-model' })])
    expect(f.connection.value.modelSource).toBe('live')

    f.updateField('model', 'another-model')
    expect(f.connection.value.phase).toBe('unverified')
    expect(f.connection.value.firstResponseMs).toBeNull()
    expect(f.connection.value.totalMs).toBeNull()
    expect(f.connection.value.models).toEqual([expect.objectContaining({ id: 'test-vendor/test-model' })])
    expect(f.connection.value.modelSource).toBe('live')

    await f.probeConnection()
    expect(f.connection.value.phase).toBe('verified')

    f.updateField('base_url', 'http://127.0.0.1:11434')
    expect(f.connection.value.phase).toBe('unverified')
    expect(f.connection.value.models).toEqual([])
    expect(f.connection.value.modelSource).toBe('none')
  })

  it('uses saved-profile RPCs only when explicitly testing the persisted deployment', async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === 'onboarding.llmProfile.probe') return PROBE_OK
      if (method === 'onboarding.llmProfile.models.discover') return DISCOVER_OK
      throw new Error(`unexpected rpc method: ${method}`)
    })
    const f = useSetupProviderForm()
    f.initStoredProfile('deepseek')

    await f.probeConnection({ defaultModel: 'deepseek-chat', storedProfile: true })

    expect(callMock).toHaveBeenNthCalledWith(1, 'onboarding.llmProfile.probe', {
      providerId: 'deepseek',
      model: 'deepseek-chat',
    })
    expect(callMock).toHaveBeenNthCalledWith(2, 'onboarding.llmProfile.models.discover', {
      providerId: 'deepseek',
    })
  })

  it('tests a saved profile editor as a draft with its current unsaved key, endpoint, proxy, and model', async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === 'onboarding.llmProfile.draft.probe') return PROBE_OK
      if (method === 'onboarding.llmProfile.draft.models.discover') return DISCOVER_OK
      throw new Error(`unexpected rpc method: ${method}`)
    })
    const f = useSetupProviderForm()
    f.initStoredProfile('custom', {
      base_url: 'https://old.example.test/v1',
      proxy: 'http://old-proxy.example.test:8080',
    })
    f.updateField('api_key', 'draft-secret')
    f.updateField('base_url', 'https://new.example.test/v1')
    f.updateField('proxy', '')
    f.updateField('model', 'draft-model')

    await f.probeConnection({ draftProfile: true })

    const expectedDraft = {
      providerId: 'custom',
      apiKey: 'draft-secret',
      baseUrl: 'https://new.example.test/v1',
      proxy: '',
      model: 'draft-model',
      keepCurrentSecret: false,
    }
    expect(callMock).toHaveBeenNthCalledWith(
      1,
      'onboarding.llmProfile.draft.probe',
      expectedDraft,
    )
    expect(callMock).toHaveBeenNthCalledWith(
      2,
      'onboarding.llmProfile.draft.models.discover',
      expectedDraft,
    )
  })

  it('keeps the saved profile secret when current settings do not replace it', async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === 'onboarding.llmProfile.draft.probe') return PROBE_OK
      if (method === 'onboarding.llmProfile.draft.models.discover') return DISCOVER_OK
      throw new Error(`unexpected rpc method: ${method}`)
    })
    const f = useSetupProviderForm()
    f.initStoredProfile('deepseek', { base_url: 'https://api.deepseek.com', proxy: '' })

    await f.probeConnection({ defaultModel: 'deepseek-chat', draftProfile: true })

    expect(callMock).toHaveBeenNthCalledWith(1, 'onboarding.llmProfile.draft.probe', {
      providerId: 'deepseek',
      baseUrl: 'https://api.deepseek.com',
      proxy: '',
      model: 'deepseek-chat',
      keepCurrentSecret: true,
    })
  })

  it('switching provider resets the connection', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })
    expect(f.connection.value.phase).toBe('verified')

    f.selectProvider('openrouter')
    expect(f.connection.value.phase).toBe('unverified')
    expect(f.connection.value.models).toEqual([])
  })

  it('re-probing unchanged credentials sends a fresh RPC instead of reusing a stale verdict', async () => {
    mockRpc()
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    f.updateField('api_key', 'sk-first')
    await f.probeConnection({ defaultModel: 'm' })
    expect(callMock).toHaveBeenCalledTimes(2)

    f.updateField('api_key', 'sk-other') // invalidate
    f.updateField('api_key', 'sk-first') // back to the cached fingerprint
    expect(f.connection.value.phase).toBe('unverified')

    await f.probeConnection({ defaultModel: 'm' })
    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.models).toHaveLength(1)
    expect(callMock).toHaveBeenCalledTimes(4)
    expect(callMock).toHaveBeenNthCalledWith(3, 'onboarding.provider.probe', {
      providerId: 'openai',
      apiKey: 'sk-first',
      model: 'm',
    })
  })

  it('a transient unreachable outcome is NOT cached, so retry re-probes', async () => {
    mockRpc({ probe: { ok: false, failureKind: 'transport_transient', message: 'timeout' } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection()
    expect(f.connection.value.phase).toBe('unreachable')
    expect(callMock).toHaveBeenCalledTimes(1)

    mockRpc() // endpoint recovered
    await f.probeConnection({ defaultModel: 'm' })
    expect(f.connection.value.phase).toBe('verified')
  })

  it('discards a stale probe result that raced a credential edit', async () => {
    let resolveProbe!: (value: unknown) => void
    callMock.mockImplementation(() => new Promise(resolve => { resolveProbe = resolve }))
    const f = useSetupProviderForm()
    f.selectProvider('openai')

    const pending = f.probeConnection()
    f.updateField('api_key', 'sk-edited-mid-flight')
    resolveProbe(PROBE_OK)
    await pending

    expect(f.connection.value.phase).toBe('unverified')
    expect(callMock).toHaveBeenCalledTimes(1) // stale ok never triggers discover
  })

  it('deduplicates concurrent model discovery requests', async () => {
    const resolvers: Array<(value: unknown) => void> = []
    callMock.mockImplementation((method: string) => {
      if (method !== 'onboarding.models.discover') {
        throw new Error(`unexpected rpc method: ${method}`)
      }
      return new Promise(resolve => { resolvers.push(resolve) })
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')

    const first = f.discoverModels()
    const second = f.discoverModels()
    resolvers.forEach(resolve => resolve(DISCOVER_OK))
    await Promise.all([first, second])

    expect(callMock).toHaveBeenCalledTimes(1)
    expect(f.connection.value.models).toHaveLength(1)
  })

  it('keeps an in-flight catalog request alive while the user types a model id', async () => {
    let resolveDiscover!: (value: unknown) => void
    callMock.mockImplementation((method: string) => {
      if (method !== 'onboarding.models.discover') {
        throw new Error(`unexpected rpc method: ${method}`)
      }
      return new Promise(resolve => { resolveDiscover = resolve })
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')

    const pending = f.discoverModels()
    f.updateField('model', 'gpt-4.1-mini')
    resolveDiscover(DISCOVER_OK)
    await pending

    expect(f.connection.value.phase).toBe('unverified')
    expect(f.connection.value.modelSource).toBe('live')
    expect(f.connection.value.models).toHaveLength(1)
  })

  it('discards a model catalog that resolves after the selected provider changes', async () => {
    let resolveOpenAi!: (value: unknown) => void
    callMock.mockImplementation((method: string, params?: Record<string, unknown>) => {
      if (method !== 'onboarding.models.discover') {
        throw new Error(`unexpected rpc method: ${method}`)
      }
      if (params?.providerId === 'openai') {
        return new Promise(resolve => { resolveOpenAi = resolve })
      }
      return Promise.resolve({
        ...DISCOVER_OK,
        models: [{ ...DISCOVER_ROW, id: 'deepseek-chat', name: 'DeepSeek Chat' }],
      })
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    const stale = f.discoverModels()

    f.selectProvider('deepseek')
    await f.discoverModels()
    resolveOpenAi(DISCOVER_OK)
    await stale

    expect(f.selectedProvider.value).toBe('deepseek')
    expect(f.connection.value.models.map(model => model.id)).toEqual(['deepseek-chat'])
  })

  it('starts fresh discovery when a probe supersedes an in-flight listing', async () => {
    let discoverCalls = 0
    let resolveFirstDiscover!: (value: unknown) => void
    callMock.mockImplementation((method: string) => {
      if (method === 'onboarding.provider.probe') return Promise.resolve(PROBE_OK)
      if (method === 'onboarding.models.discover') {
        discoverCalls += 1
        if (discoverCalls === 1) {
          return new Promise(resolve => { resolveFirstDiscover = resolve })
        }
        return Promise.resolve(DISCOVER_OK)
      }
      throw new Error(`unexpected rpc method: ${method}`)
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')

    const staleListing = f.discoverModels()
    const probe = f.probeConnection({ defaultModel: 'test-model' })
    await Promise.resolve()
    resolveFirstDiscover(DISCOVER_OK)
    await Promise.all([staleListing, probe])

    expect(discoverCalls).toBe(2)
    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.models).toHaveLength(1)
  })

  it('a failed discover keeps the verified phase and sets discoverError', async () => {
    mockRpc({ discover: { ok: false, failureKind: 'bad_request', detail: 'listing unsupported', source: 'none', models: [] } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })

    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.models).toEqual([])
    expect(f.connection.value.modelSource).toBe('none')
    expect(f.connection.value.discoverError).toBe('listing unsupported')
  })

  it('an empty listing (ok, source none) is not an error', async () => {
    mockRpc({ discover: { ok: true, failureKind: '', detail: '', source: 'none', models: [] } })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })

    expect(f.connection.value.phase).toBe('verified')
    expect(f.connection.value.models).toEqual([])
    expect(f.connection.value.modelSource).toBe('none')
    expect(f.connection.value.discoverError).toBe('')
  })

  it('drops model rows when the gateway marks the catalog source as none', async () => {
    mockRpc({
      discover: {
        ok: true,
        failureKind: '',
        detail: '',
        source: 'none',
        models: [DISCOVER_ROW],
      },
    })
    const f = useSetupProviderForm()
    f.selectProvider('openai')
    await f.probeConnection({ defaultModel: 'm' })

    expect(f.connection.value.models).toEqual([])
    expect(f.connection.value.modelSource).toBe('none')
  })
})
