import { describe, expect, it, vi } from 'vitest'
import { createV4SetupWorkflow } from './setupWorkflowV4'

describe('primary provider RPC compatibility', () => {
  it.each(['ROUTER_PROVIDER_CONFLICT', 'onboarding.llmProfile.router_provider_conflict'])('maps %s to the same typed conflict without interpreting its text', async code => {
    const details = { reason: 'router_provider_conflict', providerId: 'tokenrhythm', conflictProviders: ['openrouter'], allowedRouterActions: ['disable'] }
    const request = vi.fn().mockRejectedValue(Object.assign(new Error('opaque'), { code, details }))
    const workflow = createV4SetupWorkflow({ request })
    await expect(workflow.profile.activateProfile({ providerId: 'tokenrhythm' })).rejects.toMatchObject({
      code: 'conflict', reason: 'router-provider-conflict', details,
    })
  })

  it.each([false, undefined])('requires independent positive method availability (%s)', async available => {
    const request = vi.fn()
    const workflow = createV4SetupWorkflow({ request, ...(available === undefined ? {} : { supports: () => available }) })
    expect(workflow.capabilities.profileUpsertAndActivate).toBe(false)
    await expect(workflow.profile.upsertAndActivateProfile({ providerId: 'tokenrhythm' })).rejects.toMatchObject({ code: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })

  it('uses one distinct RPC and preserves keep/clear/endpoint semantics', async () => {
    const request = vi.fn().mockResolvedValue({ changed: true, restartRequired: false, configPath: '/isolated/config.toml', entry: { provider: 'tokenrhythm', active: true }, warnings: [] })
    const workflow = createV4SetupWorkflow({ request, supports: () => true })
    const command = { providerId: 'tokenrhythm', apiKey: '', apiKeyEnv: 'SYNTHETIC_ENV', keepCurrentSecret: false, baseUrl: 'https://example.invalid/v1', imageGenerationIntent: 'preserve' }
    await workflow.profile.upsertAndActivateProfile(command)
    expect(request).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledWith('onboarding.llmProfile.upsertAndActivate', command, expect.anything())
  })

  it('does not downgrade a stale advertised method and classifies already-active explicitly', async () => {
    const request = vi.fn().mockRejectedValueOnce(Object.assign(new Error('opaque'), { code: 'METHOD_NOT_FOUND' }))
      .mockRejectedValueOnce(Object.assign(new Error('opaque'), { code: 'LLM_PROFILE_INVALID', details: { reason: 'already_active' } }))
    const workflow = createV4SetupWorkflow({ request, supports: () => true })
    await expect(workflow.profile.upsertAndActivateProfile({ providerId: 'tokenrhythm' })).rejects.toMatchObject({ code: 'unsupported' })
    expect(request).toHaveBeenCalledTimes(1)
    await expect(workflow.profile.upsertAndActivateProfile({ providerId: 'tokenrhythm' })).rejects.toMatchObject({ code: 'invalid', reason: 'already-active' })
    expect(request.mock.calls.map(call => call[0])).toEqual(['onboarding.llmProfile.upsertAndActivate', 'onboarding.llmProfile.upsertAndActivate'])
  })
})
