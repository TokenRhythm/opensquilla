import { describe, expect, it, vi } from 'vitest'
import { SetupWorkflowError } from '@/modules/setupWorkflow'
import { createV4SetupWorkflow, mapSetupError } from './setupWorkflowV4'

describe('setup workflow error mapping', () => {
  it.each(['code', 'data'] as const)('recognizes the exact ensemble validation code in %s', location => {
    const error = Object.assign(new Error('1 validation error for EnsembleConfig'),
      location === 'code'
        ? { code: 'onboarding.ensemble.invalid' }
        : { data: { code: 'onboarding.ensemble.invalid' } },
    )
    const mapped = mapSetupError(error)
    expect(mapped).toBeInstanceOf(SetupWorkflowError)
    expect(mapped.code).toBe('invalid')
    expect(mapped.reason).toBe('ensemble-invalid')
    expect(mapped.message).toBe(error.message)
    expect(mapped.cause).toBe(error)
  })

  it('preserves the original rejection when saving ensemble settings', async () => {
    const original = Object.assign(new Error('Value error, candidates must contain at least 2 items'), {
      code: 'onboarding.ensemble.invalid',
      details: { input_value: { candidates: ['synthetic-model'] } },
    })
    const request = vi.fn().mockRejectedValue(original)
    const setup = createV4SetupWorkflow({ request })
    await expect(setup.capability.configureEnsemble({ enabled: true })).rejects.toMatchObject({
      code: 'invalid', reason: 'ensemble-invalid', cause: original,
    })
    expect(request).toHaveBeenCalledWith(
      'onboarding.ensemble.configure', { enabled: true }, expect.any(Object),
    )
  })

  it.each([
    ['UNAUTHORIZED', 'forbidden', undefined],
    ['FORBIDDEN', 'forbidden', undefined],
    ['INVALID_PARAMS', 'invalid', undefined],
    ['onboarding.other.invalid', 'invalid', undefined],
    ['onboarding.ensemble.invalid.extra', 'unavailable', undefined],
    ['onboarding.provider.invalid', 'invalid', 'provider-invalid'],
    ['onboarding.router.invalid', 'invalid', 'router-invalid'],
    ['NETWORK_ERROR', 'unavailable', undefined],
  ])('preserves handling of %s', (code, expectedCode, reason) => {
    const original = Object.assign(new Error('Synthetic failure detail'), { code })
    const mapped = mapSetupError(original)
    expect(mapped.code).toBe(expectedCode)
    expect(mapped.reason).toBe(reason)
    expect(mapped.message).toBe(original.message)
    expect(mapped.cause).toBe(original)
  })

  it('does not infer an ensemble validation failure from message text', () => {
    const original = new Error('onboarding.ensemble.invalid: connection closed')
    const mapped = mapSetupError(original)
    expect(mapped.code).toBe('unavailable')
    expect(mapped.reason).toBeUndefined()
  })

  it('keeps an already mapped domain error unchanged', () => {
    const error = new SetupWorkflowError('invalid', 'Synthetic detail', 'ensemble-invalid')
    expect(mapSetupError(error)).toBe(error)
  })
})
