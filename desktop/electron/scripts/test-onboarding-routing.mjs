import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import vm from 'node:vm'
import ts from '@typescript/typescript6'
import * as routerProfiles from '../dist/desktop-router-profiles.js'
import { normalizeRouterPresetBinding, resolveDesktopRouterUpdate, routerConfigTomlLines } from '../dist/desktop-router-config.js'
import * as primaryProviderChange from '../dist/desktop-primary-provider-change.js'
import { normalizeRouterTiers } from '../dist/router-tier-normalization.js'
import { parse, stringify } from 'smol-toml'

// Run the production catalog, selection and save functions. Only host IO,
// credential encryption and telemetry are replaced with offline boundaries.
const source = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true)
const functions = new Set([
  'providerDefaults', 'normalizeProvider', 'normalizeRouterMode',
  'modelRoutingModeAllowed', 'modelRoutingModeForRouterMode',
  'normalizeModelRoutingMode', 'routerModeForModelRoutingMode',
  'routerDefaultModel', 'normalizeDesktopCredential', 'loadDesktopCredential',
  'saveDesktopCredential',
])
const variables = new Set(['PROVIDER_CATALOG', 'PROVIDER_BY_ID', 'MIGRATION_TRANSACTION_ID_RE'])
const selected = ast.statements.filter(node => (
  ts.isFunctionDeclaration(node) && functions.has(node.name?.text)
) || (
  ts.isVariableStatement(node)
  && node.declarationList.declarations.some(declaration => variables.has(declaration.name.getText(ast)))
))
assert.equal(selected.filter(ts.isFunctionDeclaration).length, functions.size)
const executable = ts.transpileModule(selected.map(node => node.getText(ast)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
let saved = null
let credentialText = null
let configText = null
const context = vm.createContext({
  ...routerProfiles, normalizeRouterTiers, normalizeRouterPresetBinding, resolveDesktopRouterUpdate,
  parseDesktopOnboardingReceipt: () => null,
  require(specifier) {
    assert.equal(specifier, './desktop-primary-provider-change.js')
    return primaryProviderChange
  },
  join, Date, JSON, Buffer,
  activeDesktopProfile: () => ({ home: 'synthetic-profile', credentialPath: 'synthetic-credential' }),
  credentialPath: () => 'synthetic-credential',
  readOptionalDesktopText: async path => path.endsWith('config.toml') ? configText : credentialText,
  readFile: async () => {
    if (credentialText !== null) return credentialText
    throw Object.assign(new Error('missing synthetic credential'), { code: 'ENOENT' })
  },
  normalizeTextTier: value => value || 'c1',
  normalizeSearchProvider: () => 'duckduckgo',
  searchProviderDefaults: () => ({ requiresApiKey: false, envKey: '' }),
  normalizeBooleanSetting: (value, fallback) => typeof value === 'boolean' ? value : fallback,
  encryptSecret: () => ({ value: 'synthetic-encrypted-key', encryption: 'safeStorage' }),
  readDesktopConfigNetworkObservabilitySetting: () => false,
  desktopLocaleChoice: () => 'en', desktopLocale: 'en',
  parseDesktopTelemetryConsent: () => ({}),
  resolveDesktopTelemetryConsent: () => ({ growth: {} }),
  desktopGrowthTelemetry: { prepareOnboardingReceipt: () => null },
  beginDesktopWriterOperation: () => () => {},
  applyDesktopSettingsPair: async (_profile, credential) => { saved = credential },
  runDesktopTelemetryConsentSideEffect: async (_phase, effect) => effect(),
  syncDesktopConsentMirror: async () => {},
  rememberDecryptedCredentialSecrets: () => {},
})
vm.runInContext(executable, context)
const catalog = vm.runInContext('PROVIDER_CATALOG', context)

for (const provider of catalog) {
  const supportsRouter = Object.hasOwn(routerProfiles.ROUTER_PROFILES, provider.id)
  assert.equal(provider.routerSupported, supportsRouter, `${provider.id}: UI capability must match the routing catalog`)
  const requestedMode = supportsRouter ? 'squilla_router' : 'direct'
  const routerMode = supportsRouter ? 'recommended' : 'disabled'
  for (const explicitMode of [false, true]) {
    saved = null
    const credential = await context.saveDesktopCredential({
      provider: provider.id, apiKey: 'synthetic-key', model: 'synthetic-direct-model',
      ...(explicitMode ? { modelRoutingMode: requestedMode, routerMode } : {}),
    }, true, true)
    assert.equal(credential, saved, `${provider.id}: must use the settings transaction`)
    assert.equal(credential.modelRoutingMode, requestedMode, provider.id)
    assert.equal(credential.routerMode, routerMode, provider.id)
    assert.equal(context.normalizeDesktopCredential(credential).modelRoutingMode, requestedMode)
    const config = parse(routerConfigTomlLines(credential).join('\n')).squilla_router
    assert.equal(config.enabled, supportsRouter, provider.id)
    if (supportsRouter) {
      for (const tier of ['c0', 'c1', 'c2', 'c3']) {
        assert.equal(config.tiers[tier].provider, provider.id)
        assert.equal(config.tiers[tier].model, routerProfiles.ROUTER_PROFILES[provider.id][tier].model)
      }
    }
  }
}

// First-run providers without an onboarding model preset submit an explicit
// direct choice, even when the backend supports optional Router presets.
// Persist the model the user entered instead of replacing it with a tier.
for (const provider of catalog.filter(provider => !['tokenrhythm', 'openrouter'].includes(provider.id))) {
  const credential = await context.saveDesktopCredential({
    provider: provider.id, apiKey: 'synthetic-key', model: 'operator-selected-model',
    modelRoutingMode: 'direct', routerMode: 'disabled',
  }, true, true)
  assert.equal(credential.model, 'operator-selected-model', provider.id)
  assert.equal(credential.modelRoutingMode, 'direct', provider.id)
  assert.equal(credential.routerMode, 'disabled', provider.id)
  assert.equal(parse(routerConfigTomlLines(credential).join('\n')).squilla_router.enabled, false, provider.id)
}

for (const payload of [
  { provider: 'minimax_openai', modelRoutingMode: 'squilla_router' },
  { provider: 'ollama', routerMode: 'recommended' },
  { provider: 'mimo_openai', modelRoutingMode: 'llm_ensemble' },
  { provider: 'mimo_openai', modelRoutingMode: 'unknown-mode' },
  { provider: 'mimo_openai', routerMode: 'unknown-mode' },
  { provider: 'mimo_openai', modelRoutingMode: 'direct', routerMode: 'recommended' },
  { provider: 'mimo_openai', modelRoutingMode: 'squilla_router', routerMode: 'disabled' },
  { provider: 'mimo_openai', routerMode: false },
]) {
  saved = null
  await assert.rejects(context.saveDesktopCredential({
    apiKey: 'synthetic-key', model: 'synthetic-model', ...payload,
  }, true, true), /routing|Router/i)
  assert.equal(saved, null, 'invalid routing must not reach the settings transaction')
}
// A rejected request must leave the next corrected request usable.
await context.saveDesktopCredential({
  provider: 'minimax_openai', modelRoutingMode: 'direct',
  model: 'synthetic-model', apiKey: 'synthetic-key',
}, true, true)
assert.equal(saved.modelRoutingMode, 'direct')

// A new explicit mode must not be rejected because the previous provider used
// another one; custom routes must also survive a save/load round trip.
for (const custom of [false, true]) {
  const existing = await context.saveDesktopCredential({
    provider: 'tokenrhythm', apiKey: 'synthetic-key',
  }, true, true)
  credentialText = JSON.stringify(existing)
  const config = parse(routerConfigTomlLines(existing).join('\n'))
  config.llm = { provider: 'tokenrhythm', model: existing.model }
  if (custom) {
    config.squilla_router.preset_binding = 'custom'
    config.squilla_router.cross_provider_tiers = true
  }
  configText = stringify(config)
  const credential = await context.saveDesktopCredential({
    provider: custom ? 'mimo_openai' : 'ollama', apiKey: 'synthetic-key',
    model: 'synthetic-direct-model',
    modelRoutingMode: custom ? 'squilla_router' : 'direct',
  }, true)
  const reloaded = context.normalizeDesktopCredential(credential)
  assert.equal(reloaded.modelRoutingMode, custom ? 'squilla_router' : 'direct')
  assert.equal(reloaded.routerMode, custom ? 'recommended' : 'disabled')
  assert.equal(reloaded.routerPresetBinding, custom ? 'custom' : 'follow_primary')
  if (custom) assert.equal(reloaded.routerTiers.c1.provider, 'tokenrhythm')
  credentialText = null
  configText = null
}

credentialText = JSON.stringify({
  provider: 'minimax_openai', modelRoutingMode: 'squilla_router', routerMode: 'recommended',
})
await assert.rejects(context.loadDesktopCredential(), /Reset setup/)
credentialText = '{invalid'
await assert.rejects(context.loadDesktopCredential(), /invalid or unreadable/)

// Exercise the real startup catch, including a profile change during its await.
// It must reveal the existing reset UI, and must still clean up a superseded run.
const resume = ast.statements.find(node => ts.isFunctionDeclaration(node)
  && node.name?.text === 'openOrResumeDesktopApp')
assert.ok(resume)
const resumeCode = ts.transpileModule(resume.getText(ast), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText
for (const superseded of [false, true]) {
  const events = []
  let profile = 'original'
  let attempts = 0
  const startup = vm.createContext({
    Error, DesktopRoutingConfigurationError: routerProfiles.DesktopRoutingConfigurationError,
    isQuitting: false, desktopOpenFlowPromise: null, desktopOpenFlowRevision: 0,
    forceOnboardingOnNextStartup: false, onboardingPromptProfileKey: null,
    gatewayProfileKey: 'original', gatewayProcess: null,
    gatewayState: { status: 'starting', owned: false, url: null },
    invalidateDesktopOpenFlow: () => { startup.desktopOpenFlowRevision += 1 },
    desktopProfileKey: () => profile,
    desktopOpenAuthorityIsCurrent: (revision, key) => (
      revision === startup.desktopOpenFlowRevision && key === profile
    ),
    createMainWindow: async () => {}, focusMainWindow() {},
    inspectActiveProfileBeforeStartup: async () => true, syncDesktopConsentMirror: async () => {},
    loadDesktopRendererIntoCurrentWindow: async () => { events.push('renderer') },
    reuseHealthyGatewayState: async () => null,
    ensureGatewayStarted: async () => {
      if (++attempts === 1) throw new routerProfiles.DesktopRoutingConfigurationError()
      return startup.gatewayState
    },
    publishGatewayConnection() {}, desktopLog() {}, currentMainWindow: () => ({}),
    restoreMainWindowToBootPage: async () => {
      events.push('boot')
      if (superseded) profile = 'replacement'
    },
    sendBootError: error => { assert.match(error.message, /Reset setup/); events.push('error') },
    finishAppStartFailure: () => { events.push('failure') },
    clearReusableGatewayState: () => { events.push('cleanup') },
    sendBootStatus() {}, finishAppStartSuccess: () => { events.push('ready') },
  })
  vm.runInContext(resumeCode, startup)
  await startup.openOrResumeDesktopApp()
  assert.deepEqual(events, superseded
    ? ['renderer', 'boot', 'cleanup', 'renderer', 'ready']
    : ['renderer', 'boot', 'error', 'failure'])
  assert.equal(startup.desktopOpenFlowPromise, null)
}

console.log(JSON.stringify({ ok: true, providers: catalog.length, freshSave: true, invalidRoutingRejected: true }))
