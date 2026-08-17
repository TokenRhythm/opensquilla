import { describe, expect, it } from 'vitest'
import {
  DORMANT_SHARED_SELECTION_MODES,
  ENSEMBLE_SELECTION_MODES,
  PROVIDER_RECOMMENDED_ENSEMBLE_SELECTION_MODES,
  STATIC_B5_PROFILES,
  staticB5ModeForProvider,
} from './router_tier_contract'

describe('generated router-tier SelectionMode contract', () => {
  it('keeps one generated static profile per supported provider', () => {
    expect(Object.keys(STATIC_B5_PROFILES)).toEqual([
      'static_openrouter_b5',
      'static_tokenrhythm_b5',
    ])
    expect(staticB5ModeForProvider('OpenRouter')).toBe('static_openrouter_b5')
    expect(staticB5ModeForProvider('tokenrhythm')).toBe('static_tokenrhythm_b5')
  })

  it('keeps provider recommendations and ownership sets in generated data', () => {
    expect(PROVIDER_RECOMMENDED_ENSEMBLE_SELECTION_MODES.tokenrhythm).toBe(
      'static_tokenrhythm_b5',
    )
    expect(DORMANT_SHARED_SELECTION_MODES).toEqual([
      'static_openrouter_b5',
      'static_tokenrhythm_b5',
      'custom_b5',
    ])
    expect(ENSEMBLE_SELECTION_MODES).toContain('router_dynamic')
  })
})
