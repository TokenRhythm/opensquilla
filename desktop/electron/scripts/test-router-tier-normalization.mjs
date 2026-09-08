import { strict as assert } from 'node:assert'

import { normalizeRouterTiers } from '../dist/router-tier-normalization.js'

const currentFallback = {
  c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash-0731', supportsImage: false },
  c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro-0813' },
  c2: { provider: 'tokenrhythm', model: 'kimi-k2.7-code' },
  c3: { provider: 'tokenrhythm', model: 'glm-5.2', supportsImage: true, ensembleEnabled: true },
}

const legacyCredentialTiers = {
  c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash' },
  c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro' },
  c2: { provider: 'tokenrhythm', model: 'kimi-k2.7-code' },
  c3: { provider: 'tokenrhythm', model: 'glm-5.2' },
}

const loaded = normalizeRouterTiers(legacyCredentialTiers, currentFallback)
assert.deepEqual(
  Object.fromEntries(Object.entries(loaded).map(([name, tier]) => [name, tier.model])),
  Object.fromEntries(Object.entries(legacyCredentialTiers).map(([name, tier]) => [name, tier.model])),
)
assert.equal(Object.hasOwn(loaded.c3, 'ensembleEnabled'), false)
assert.equal(Object.hasOwn(loaded.c0, 'supportsImage'), false)
assert.equal(Object.hasOwn(loaded.c3, 'supportsImage'), false)

// saveDesktopCredential normalizes the already-loaded same-provider ladder a
// second time. The missing legacy opt-in must remain missing on that pass.
const resaved = normalizeRouterTiers(loaded, currentFallback)
assert.deepEqual(resaved, loaded)
assert.equal(Object.hasOwn(resaved.c3, 'ensembleEnabled'), false)
assert.equal(Object.hasOwn(resaved.c0, 'supportsImage'), false)

for (const key of ['supports_image', 'supportsImage']) {
  for (const value of [false, true]) {
    const legacyCapability = normalizeRouterTiers(
      { ...legacyCredentialTiers, c0: { ...legacyCredentialTiers.c0, [key]: value } },
      currentFallback,
    )
    assert.equal(Object.hasOwn(legacyCapability.c0, 'supportsImage'), false)
    assert.equal(Object.hasOwn(legacyCapability.c0, 'supports_image'), false)
    assert.equal(legacyCapability.c0.model, legacyCredentialTiers.c0.model)
  }
}

const fresh = normalizeRouterTiers(undefined, currentFallback)
assert.equal(fresh.c3.ensembleEnabled, true)
assert.equal(Object.hasOwn(fresh.c0, 'supportsImage'), false)
assert.equal(Object.hasOwn(fresh.c3, 'supportsImage'), false)

const explicitSnakeCase = normalizeRouterTiers(
  { ...legacyCredentialTiers, c3: { ...legacyCredentialTiers.c3, ensemble_enabled: true } },
  currentFallback,
)
assert.equal(explicitSnakeCase.c3.ensembleEnabled, true)

console.log(JSON.stringify({ ok: true, legacyMissingPreserved: true }))
