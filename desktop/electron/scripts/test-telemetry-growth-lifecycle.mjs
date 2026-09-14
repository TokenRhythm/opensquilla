import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import vm from 'node:vm'
import ts from 'typescript'

import { OnboardingSaveTelemetry } from '../dist/onboarding-save-telemetry.js'
import { DesktopTelemetryRuntimeGate, clearEarlyTelemetryScope } from '../dist/telemetry/early-spool.js'
import { CONSENT_MIRROR_SCHEMA_VERSION, writeConsentMirror } from '../dist/telemetry/consent-mirror.js'
import { runTelemetrySideEffectFailOpen } from '../dist/telemetry/fail-open.js'
import {
  applyDesktopTelemetryConsentPayload, desktopPrivacyTomlLines, parseDesktopTelemetryConsent,
  parseLegacyNetworkObservabilityDisabled, requireExplicitOnboardingConsent,
} from '../dist/telemetry/onboarding-consent.js'
import {
  clearDesktopGrowthTelemetryState, DesktopGrowthTelemetry, parseDesktopOnboardingReceipt,
} from '../dist/telemetry/growth.js'

// Execute the production lifecycle functions with only their Electron, provider,
// and atomic settings-transaction boundaries substituted by offline fixtures.
const source = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true)
const names = new Set([
  'finishAppStartSuccess', 'finishAppStartFailure', 'normalizeDesktopCredential',
  'loadDesktopCredential', 'saveDesktopCredential', 'performOnboardingSave',
  'mirroredScopeConsent', 'writeDesktopConsentMirror', 'syncDesktopConsentMirror',
  'runDesktopTelemetryConsentSideEffect',
])
const extracted = ast.statements.filter((node) => ts.isFunctionDeclaration(node)
  && names.has(node.name?.text))
assert.equal(extracted.length, names.size)
const executable = ts.transpileModule(
  'let appStartResultRecorded = false;\n'
    + extracted.map((node) => node.getText(ast)).join('\n'),
  { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } },
).outputText

const NOW = '2026-09-11T12:00:00.000Z'
const READY_AT = '2026-09-11T12:01:00.000Z'
const root = mkdtempSync(join(tmpdir(), 'opensquilla-growth-lifecycle-'))
const payload = {
  provider: 'ollama', model: 'synthetic-model', reliabilityDiagnosticsEnabled: false,
  productAnalyticsEnabled: true,
}

function harness(directory, {
  stableCode = 'fresh_profile', platform = 'macos', failure = null, deferred = false,
  importedOrMigrated = false, env = {}, nowUtc = NOW,
} = {}) {
  class TestDate extends Date {
    constructor(...args) { super(...(args.length ? args : [nowUtc])) }
    static now() { return Date.parse(nowUtc) }
  }
  const profile = { home: join(directory, 'profile'), credentialPath: join(directory, 'credential.json') }
  const telemetryDirectory = join(profile.home, 'state', 'telemetry')
  const paths = {
    profileKey: profile.home, telemetryDirectory,
    consentMirrorPath: join(telemetryDirectory, 'desktop-consent-mirror.json'),
    spoolRoot: join(telemetryDirectory, 'desktop-early-spool'),
  }
  const gate = new DesktopTelemetryRuntimeGate()
  const growth = new DesktopGrowthTelemetry({
    runtimeGate: gate, appVersion: () => '0.5.4', platform, env, nowDate: () => new TestDate(),
  })
  growth.observeProfileInspection({ profileKey: profile.home, stableCode, importedOrMigrated })
  const startupResults = []
  let settingsPersisted = false
  let writerFinished = false
  const defaults = { requiresApiKey: false, model: 'synthetic-model', baseUrl: '', apiKeyEnv: '' }
  const context = vm.createContext({
    Date: TestDate, join, Math, JSON, Buffer, OnboardingSaveTelemetry,
    desktopTelemetryRuntimeGate: gate, desktopGrowthTelemetry: growth,
    desktopReliabilityTelemetry: { synchronize() {}, recordAppStartResult: (event) => startupResults.push(event) },
    desktopProcessStartedAt: TestDate.now(), desktopLocale: 'en', onboardingSaveTelemetryAttempt: 0,
    app: { isPackaged: false }, desktopLog() {}, refreshDesktopReliabilityForegroundState() {},
    activeDesktopProfile: () => profile, credentialPath: () => profile.credentialPath,
    readFile: async (path) => readFileSync(path, 'utf8'),
    readOptionalDesktopText: async (path) => existsSync(path) ? readFileSync(path, 'utf8') : null,
    desktopTelemetryDirectory: () => telemetryDirectory,
    desktopConsentMirrorPath: () => paths.consentMirrorPath,
    desktopEarlyTelemetrySpoolPath: () => paths.spoolRoot,
    writeConsentMirror, CONSENT_MIRROR_SCHEMA_VERSION, clearEarlyTelemetryScope,
    clearDesktopGrowthTelemetryState, parseDesktopOnboardingReceipt, runTelemetrySideEffectFailOpen,
    applyDesktopTelemetryConsentPayload, parseDesktopTelemetryConsent,
    parseLegacyNetworkObservabilityDisabled, requireExplicitOnboardingConsent,
    PROVIDER_BY_ID: new Map(), normalizeProvider: (value) => value,
    providerDefaults: () => defaults, normalizeRouterMode: () => 'disabled',
    normalizeModelRoutingMode: () => 'direct', routerModeForModelRoutingMode: () => 'disabled',
    normalizeTextTier: () => 'c1', defaultRouterTiers: () => ({}),
    normalizeRouterTiers: () => ({}), routerDefaultModel: () => '',
    normalizeSearchProvider: () => 'none', searchProviderDefaults: () => ({ requiresApiKey: false, envKey: '' }),
    normalizeBooleanSetting: (value, fallback) => typeof value === 'boolean' ? value : fallback,
    readDesktopConfigNetworkObservabilitySetting: () => false,
    desktopLocaleChoice: () => 'en', MIGRATION_TRANSACTION_ID_RE: /^[0-9a-f-]{36}$/,
    decryptApiKey: () => '', decryptSearchApiKey: () => '', rememberDecryptedCredentialSecrets() {},
    refreshPrimaryRecoveryAfterImportAttempt: async () => false,
    onboardingFlows: { canComplete: () => true },
    beginDesktopWriterOperation: () => () => { writerFinished = true },
    readPendingMigrationProviderSetup: async () => null, invalidateSecretStorageBackendCache() {},
    applyDesktopLocaleChoice() {},
    clearPendingMigrationProviderSetup: async () => {
      if (failure === 'local_finalize') throw new Error('synthetic local finalize failure')
    },
    desktopWriters: { closed: deferred }, isQuitting: false,
    appExitPhase: deferred ? 'deferred' : 'running', abandonOnboardingFlow() {},
    completeOnboardingFlow: () => true,
    onboardingSaveFailure: (code, error) => ({ ok: false, code, error }),
    applyDesktopSettingsPair: async (_profile, _credential, candidate, expected, _reserved, _locale, consent) => {
      assert.equal(existsSync(profile.credentialPath) ? readFileSync(profile.credentialPath, 'utf8') : null, expected)
      // Stand in for the existing recoverable credential/config pair commit.
      mkdirSync(profile.home, { recursive: true })
      writeFileSync(profile.credentialPath, candidate)
      const configPath = join(profile.home, 'config.toml')
      const effectiveConsent = consent ?? parseDesktopTelemetryConsent(readFileSync(configPath, 'utf8'))
      writeFileSync(configPath, desktopPrivacyTomlLines(false, effectiveConsent, true).join('\n'))
      settingsPersisted = true
      if (failure === 'after_commit') throw new Error('synthetic process stop after settings commit')
    },
  })
  vm.runInContext(executable, context)
  return {
    context, growth, paths, profile, startupResults,
    setNow(value) { nowUtc = value },
    get settingsPersisted() { return settingsPersisted },
    get writerFinished() { return writerFinished },
    events() {
      const scope = join(paths.spoolRoot, 'growth')
      return existsSync(scope) ? readdirSync(scope).filter((name) => name.endsWith('.ready'))
        .map((name) => JSON.parse(readFileSync(join(scope, name), 'utf8'))) : []
    },
  }
}

try {
  for (const platform of ['macos', 'windows', 'linux']) {
    const directory = join(root, platform)
    const first = harness(directory, { platform })
    await first.context.syncDesktopConsentMirror()
    await first.context.performOnboardingSave({ state: 'saving' }, payload)
    first.context.finishAppStartFailure(new Error('synthetic startup failure'), {
      stage: 'gateway_start', errorCode: 'spawn_failed',
    })
    first.setNow(READY_AT)
    first.context.finishAppStartSuccess()
    first.context.finishAppStartSuccess()
    assert.deepEqual(first.startupResults.map((event) => event.outcome), ['fail'])
    assert.equal(first.events().filter((event) => event.event_name === 'first_app_ready').length, 1)
    assert.equal(first.events().find((event) => event.event_name === 'onboarding_result').occurred_at_utc, NOW)
    assert.equal(first.events().find((event) => event.event_name === 'first_app_ready').occurred_at_utc, READY_AT)
  }

  for (const failure of ['local_finalize', 'after_commit']) {
    const directory = join(root, failure)
    const first = harness(directory, { failure })
    await first.context.syncDesktopConsentMirror()
    await assert.rejects(first.context.performOnboardingSave({ state: 'saving' }, payload), /synthetic/)
    assert.equal(first.settingsPersisted, true)
    assert.equal(first.writerFinished, true)
    const receipt = JSON.parse(readFileSync(first.profile.credentialPath, 'utf8')).growthOnboardingReceipt
    assert.ok(receipt)
    if (failure === 'after_commit') assert.deepEqual(first.events(), [])
    const recovered = harness(directory, { stableCode: 'ready', nowUtc: READY_AT })
    await recovered.context.syncDesktopConsentMirror()
    await recovered.context.syncDesktopConsentMirror()
    recovered.context.finishAppStartSuccess()
    const events = recovered.events()
    assert.deepEqual(events.map((event) => event.event_name).sort(), ['first_app_ready', 'onboarding_result'])
    assert.equal(new Set(events.map((event) => event.analytics_user_id)).size, 1)
    assert.equal(events.find((event) => event.event_name === 'onboarding_result').occurred_at_utc, NOW)
    assert.equal(events.find((event) => event.event_name === 'first_app_ready').occurred_at_utc, READY_AT)
  }

  const deferred = harness(join(root, 'lifecycle-deferral'), { deferred: true })
  await deferred.context.syncDesktopConsentMirror()
  const result = await deferred.context.performOnboardingSave({ state: 'saving' }, payload)
  assert.equal(result.code, 'lifecycle_deferred')
  assert.equal(deferred.writerFinished, true)
  assert.equal(deferred.events().filter((event) => event.event_name === 'onboarding_result').length, 1)

  // A failed transaction has no committed receipt to authorize recovery.
  const failed = harness(join(root, 'failed-save'))
  await failed.context.syncDesktopConsentMirror()
  failed.context.applyDesktopSettingsPair = async () => { throw new Error('synthetic commit failure') }
  await assert.rejects(failed.context.performOnboardingSave({ state: 'saving' }, payload), /commit failure/)
  assert.equal(existsSync(failed.profile.credentialPath), false)
  assert.deepEqual(failed.events(), [])

  for (const choice of [false, true]) {
    const old = harness(join(root, `old-profile-${choice}`), { stableCode: 'ready' })
    await old.context.syncDesktopConsentMirror()
    await old.context.performOnboardingSave({ state: 'saving' }, { ...payload, productAnalyticsEnabled: choice })
    assert.equal(JSON.parse(readFileSync(old.profile.credentialPath, 'utf8')).growthOnboardingReceipt, undefined)
    assert.deepEqual(old.events(), [])
  }

  const declined = harness(join(root, 'declined'))
  await declined.context.syncDesktopConsentMirror()
  await declined.context.performOnboardingSave({ state: 'saving' }, { ...payload, productAnalyticsEnabled: false })
  assert.equal(JSON.parse(readFileSync(declined.profile.credentialPath, 'utf8')).growthOnboardingReceipt, undefined)
  assert.deepEqual(declined.events(), [])

  const renewedDirectory = join(root, 'withdraw-and-renew')
  const consented = harness(renewedDirectory)
  await consented.context.syncDesktopConsentMirror()
  await consented.context.performOnboardingSave({ state: 'saving' }, payload)
  clearDesktopGrowthTelemetryState(consented.paths.telemetryDirectory)
  clearEarlyTelemetryScope(consented.paths.spoolRoot, 'growth')
  const renewedConsent = applyDesktopTelemetryConsentPayload(parseDesktopTelemetryConsent(null), payload, '2026-09-12T00:00:00.000Z')
  writeFileSync(join(consented.profile.home, 'config.toml'), desktopPrivacyTomlLines(false, renewedConsent, true).join('\n'))
  const renewed = harness(renewedDirectory, { stableCode: 'ready' })
  await renewed.context.syncDesktopConsentMirror()
  renewed.context.finishAppStartSuccess()
  assert.deepEqual(renewed.events(), [])
  assert.equal(existsSync(join(renewed.paths.telemetryDirectory, 'growth_cohort.json')), false)

  // A later ordinary settings save retains the receipt without creating a new
  // milestone, and normalization still accepts pre-receipt credentials.
  const settings = harness(join(root, 'settings-preserve'))
  await settings.context.syncDesktopConsentMirror()
  await settings.context.performOnboardingSave({ state: 'saving' }, payload)
  const saved = JSON.parse(readFileSync(settings.profile.credentialPath, 'utf8'))
  await settings.context.saveDesktopCredential({ provider: 'ollama', model: 'another-model' })
  const updated = JSON.parse(readFileSync(settings.profile.credentialPath, 'utf8'))
  assert.deepEqual(updated.growthOnboardingReceipt, saved.growthOnboardingReceipt)
  assert.equal(settings.events().length, 1)
  delete saved.growthOnboardingReceipt
  assert.equal(settings.context.normalizeDesktopCredential(saved).growthOnboardingReceipt, undefined)

  const importedDirectory = join(root, 'imported-receipt')
  const interrupted = harness(importedDirectory, { failure: 'after_commit' })
  await interrupted.context.syncDesktopConsentMirror()
  await assert.rejects(interrupted.context.performOnboardingSave({ state: 'saving' }, payload), /synthetic/)
  const imported = harness(importedDirectory, { stableCode: 'ready', importedOrMigrated: true })
  await imported.context.syncDesktopConsentMirror()
  imported.context.finishAppStartSuccess()
  assert.deepEqual(imported.events(), [])

  for (const disabled of ['legacy', 'environment']) {
    const blocked = harness(join(root, disabled), {
      env: disabled === 'environment' ? { DO_NOT_TRACK: '1' } : {},
    })
    await blocked.context.syncDesktopConsentMirror()
    await blocked.context.performOnboardingSave({ state: 'saving' }, {
      ...payload, disableNetworkObservability: disabled === 'legacy',
    })
    assert.equal(JSON.parse(readFileSync(blocked.profile.credentialPath, 'utf8')).growthOnboardingReceipt, undefined)
  }
} finally {
  rmSync(root, { recursive: true, force: true })
}

console.log('telemetry growth lifecycle and restart tests passed')
