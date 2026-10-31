import test from 'node:test'
import assert from 'node:assert/strict'
import { resolveAccountOrigin } from '../dist/tokenrhythm-environment.js'

test('ordinary packages retain production and test packages work without shell variables', () => {
  assert.equal(resolveAccountOrigin(undefined), 'https://tokenrhythm.studio')
  assert.equal(resolveAccountOrigin('https://gpt.tokenrhythm.studio'), 'https://gpt.tokenrhythm.studio')
  assert.equal(resolveAccountOrigin(undefined, 'https://gpt.tokenrhythm.studio'), 'https://gpt.tokenrhythm.studio')
})

test('explicit runtime origin overrides remain validated', () => {
  assert.equal(resolveAccountOrigin('https://gpt.tokenrhythm.studio', 'https://uat.example'), 'https://uat.example')
  for (const origin of ['http://gpt.tokenrhythm.studio', 'https://user:pass@gpt.tokenrhythm.studio', 'https://gpt.tokenrhythm.studio/api', 'https://gpt.tokenrhythm.studio?x=1']) {
    assert.throws(() => resolveAccountOrigin(origin), /INVALID_ACCOUNT_ORIGIN/)
  }
  assert.throws(() => resolveAccountOrigin({}), /Invalid packaged/)
})
