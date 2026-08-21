import { strict as assert } from 'node:assert'

import { normalizeRouterTiers } from '../dist/router-tier-normalization.js'

const currentFallback = {
  c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash-0731' },
  c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro-0813' },
  c2: { provider: 'tokenrhythm', model: 'kimi-k2.7-code' },
  c3: { provider: 'tokenrhythm', model: 'glm-5.2', ensembleEnabled: true },
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

// saveDesktopCredential normalizes the already-loaded same-provider ladder a
// second time. The missing legacy opt-in must remain missing on that pass.
const resaved = normalizeRouterTiers(loaded, currentFallback)
assert.deepEqual(resaved, loaded)
assert.equal(Object.hasOwn(resaved.c3, 'ensembleEnabled'), false)

const fresh = normalizeRouterTiers(undefined, currentFallback)
assert.equal(fresh.c3.ensembleEnabled, true)

const explicitSnakeCase = normalizeRouterTiers(
  { ...legacyCredentialTiers, c3: { ...legacyCredentialTiers.c3, ensemble_enabled: true } },
  currentFallback,
)
assert.equal(explicitSnakeCase.c3.ensembleEnabled, true)

console.log(JSON.stringify({ ok: true, legacyMissingPreserved: true }))
