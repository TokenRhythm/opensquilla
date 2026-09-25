import assert from 'node:assert/strict'

import { parse } from 'smol-toml'

import { freshDesktopSandboxConfigLines } from '../dist/desktop-sandbox-default.js'
import { renderUnconfiguredDesktopConfig } from '../dist/desktop-unconfigured-profile.js'

for (const platform of ['darwin', 'win32', 'linux']) {
  const config = parse(renderUnconfiguredDesktopConfig('zh-CN', platform))
  assert.deepEqual(config.llm, {
    provider: '', model: '', api_key: '', api_key_env: '', base_url: '',
  }, 'deferred setup must explicitly leave the model service unselected')
  assert.equal(config.squilla_router.enabled, false)
  assert.equal(config.llm_ensemble.enabled, false)
  assert.deepEqual(config.control_ui, {
    enabled: true, base_path: '/control', default_locale: 'zh-CN',
  }, 'the local client must remain available before model setup')
  assert.deepEqual(
    config.sandbox,
    parse(freshDesktopSandboxConfigLines(null, platform).join('\n')).sandbox,
    'deferred setup must use the same fresh-profile sandbox policy',
  )
  assert.deepEqual(Object.keys(config).sort(), [
    'control_ui', 'llm', 'llm_ensemble', 'sandbox', 'squilla_router',
  ], 'the seed must not contain credentials, profiles, or inferred provider defaults')
}

const unusualLocale = 'zh-CN"\n[llm]\nprovider = "openai'
const escaped = parse(renderUnconfiguredDesktopConfig(unusualLocale, 'darwin'))
assert.equal(escaped.control_ui.default_locale, unusualLocale)
assert.equal(escaped.llm.provider, '', 'locale content must not alter the TOML structure')

const environmentKeys = [
  'TOKENRHYTHM_API_KEY', 'OPENROUTER_API_KEY', 'OPENSQUILLA_LLM_API_KEY',
  'OPENSQUILLA_LLM_PROVIDER', 'OPENSQUILLA_LLM_MODEL',
]
const previous = new Map(environmentKeys.map((key) => [key, process.env[key]]))
const baseline = renderUnconfiguredDesktopConfig('en', 'linux')
try {
  for (const key of environmentKeys) process.env[key] = 'synthetic-inherited-value'
  assert.equal(
    renderUnconfiguredDesktopConfig('en', 'linux'), baseline,
    'inherited credentials must not change or enter the unconfigured seed',
  )
} finally {
  for (const [key, value] of previous) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
}

console.log('Desktop unconfigured profile tests passed.')
