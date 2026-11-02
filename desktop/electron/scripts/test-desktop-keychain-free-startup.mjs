import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import ts from '@typescript/typescript6'
import { secretStorageBackendForPolicy, shouldUseChromiumMockKeychainForPolicy } from '../dist/secret-storage-policy.js'
import { LocalAccountSecrets, LOCAL_ACCOUNT_FILE } from '../dist/local-account-secrets.js'
import { TokenRhythmAccount, AccountError } from '../dist/tokenrhythm-account.js'

const source = ts.createSourceFile('main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const functions = new Map(source.statements.filter(ts.isFunctionDeclaration).filter(node => node.name).map(node => [node.name.text, node.getText(source)]))
const bind = (name, env) => new Function('env', `with (env) { return ${functions.get(name)}; }`)(env)
const noOSStorage = new Proxy({}, { get() { assert.fail('default startup must not access OS credential storage') } })

test('macOS startup disables Chromium Keychain access before ready for all default build types', () => {
  for (const appPackaged of [false, true]) {
    const switches = []
    bind('configureChromiumKeychainPolicy', {
      process: { platform: 'darwin', env: {} }, shouldUseChromiumMockKeychainForPolicy,
      app: { isPackaged: appPackaged, commandLine: { appendSwitch: value => switches.push(value) } },
      safeStorage: noOSStorage,
    })()
    assert.deepEqual(switches, ['use-mock-keychain'])
  }
})

test('an old OS-encrypted manual credential cannot block startup or trigger a system prompt', async () => {
  const existing = { encryption: 'safeStorage', encryptedApiKey: 'legacy-secret' }
  const result = await bind('prepareDesktopStartupConnection', {
    loadPendingMigrationProviderSetup: async () => null,
    loadDesktopCredential: async () => existing,
    desktopSecretStoragePolicyBackend: () => secretStorageBackendForPolicy({ platform: 'darwin', appPackaged: true }),
    isConnectionReady: () => assert.fail('legacy keys must not be decrypted'),
    pathExists: async () => true, desktopConfigPath: () => '/synthetic/config.toml',
    forceOnboardingOnNextStartup: false, safeStorage: noOSStorage,
  })()
  assert.equal(result, null)
  assert.deepEqual(existing, { encryption: 'safeStorage', encryptedApiKey: 'legacy-secret' })
})

test('the main account factory persists and restores local credentials without touching Electron safeStorage', async t => {
  const home = await mkdtemp(join(tmpdir(), 'os-keychain-free-'))
  const accounts = new Map()
  t.after(async () => {
    for (const entry of accounts.values()) await entry.account.close()
    await rm(home, { recursive: true, force: true })
  })
  const get = bind('desktopAccount', {
    activeDesktopProfile: () => ({ home }), tokenrhythmAccountOrigin: 'https://tokenrhythm.studio',
    desktopAccounts: accounts, updateApplying: false, isQuitting: false, desktopCleanupBusy: false, desktopMigrationBusy: false,
    createHash, join, LocalAccountSecrets, LOCAL_ACCOUNT_FILE, AccountError,
    TokenRhythmAccount: class extends TokenRhythmAccount {
      constructor(deps) { super({ ...deps, fetch: async () => { throw Error('synthetic offline') } }) }
    },
    TokenRhythmModelProxy: class { invalidate() {} },
    shell: { openExternal() {} }, app: { isPackaged: true },
    currentMainWindow: () => null, onboardingWindow: null, safeStorage: noOSStorage,
  })
  const first = get().account
  await first.initialize()
  first.saved.tokens = { accessToken: 'synthetic-access', refreshToken: 'synthetic-refresh', expiresAt: Date.now() + 3600_000, accountId: 'a', sessionId: 's' }
  await first.persist()
  await first.close()
  accounts.clear()
  const restored = get().account
  await restored.initialize()
  assert.equal(restored.saved.tokens.refreshToken, 'synthetic-refresh')
  assert.equal(restored.deps.storageFile, LOCAL_ACCOUNT_FILE)
})
