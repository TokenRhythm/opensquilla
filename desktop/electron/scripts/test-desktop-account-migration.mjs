import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { access, mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import ts from '@typescript/typescript6'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { AccountError, TokenRhythmAccount, pauseAccountsForMaintenance } from '../dist/tokenrhythm-account.js'
import { LocalAccountSecrets, LOCAL_ACCOUNT_FILE } from '../dist/local-account-secrets.js'

const source = ts.createSourceFile('main.js', readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const functions = new Map(source.statements.filter(ts.isFunctionDeclaration).filter(node => node.name).map(node => [node.name.text, node.getText(source)]))
let handler
function collect(node) {
  if (ts.isCallExpression(node) && node.expression.getText(source) === 'ipcMain.handle'
    && ts.isStringLiteral(node.arguments[0]) && node.arguments[0].text === 'desktop:migration:run') handler = node.arguments[1].getText(source)
  ts.forEachChild(node, collect)
}
collect(source)
assert.ok(handler)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}
const origin = 'https://tokenrhythm.studio'
const exists = path => access(path).then(() => true, () => false)

function harness(account, { home = '/synthetic-profile', apply = async () => {}, receipt = () => ({}), timeoutMs = 1000, dependencies = {} } = {}) {
  const calls = []
  const window = { loadFile: async () => {}, isDestroyed: () => false, webContents: { send() {} } }
  const env = {
    tokenrhythmAccountOrigin: origin,
    AccountError, pauseAccountsForMaintenance, createHash, randomUUID, join, LocalAccountSecrets, LOCAL_ACCOUNT_FILE,
    AbortSignal: { timeout: () => AbortSignal.timeout(timeoutMs) },
    process: { env: {}, platform: 'darwin' },
    TokenRhythmAccount: class extends TokenRhythmAccount { constructor(deps) { super({ ...deps, ...dependencies }) } },
    TokenRhythmModelProxy: class { invalidate() {} async close() { calls.push('close-proxy') } },
    safeStorage: { isEncryptionAvailable: () => true, encryptString: value => Buffer.from(value), decryptString: value => value.toString() },
    app: { commandLine: { hasSwitch: () => false, getSwitchValue: () => '' } },
    shell: { openExternal: async () => {} }, currentMainWindow: () => window, onboardingWindow: null,
    trustedRecoveryIpc: () => true, trustedMainWindowControlIpc: () => true,
    trustedDesktopMigrationPreview: { id: 'preview', createdAt: Date.now(), candidate: { path: '/synthetic-source', kind: 'opensquilla' }, report: {} },
    DESKTOP_MIGRATION_PREVIEW_TTL_MS: 60_000,
    migrationPreviewAllowsApply: () => true, looksLikeOpenSquillaHome: () => true,
    desktopT: key => key, dialog: { showMessageBox: async () => ({ response: 1 }) },
    desktopWriters: new DesktopWriterAdmission(), onboardingFlows: { active: null },
    desktopMigrationBusy: false, desktopCleanupBusy: false, updateApplying: false, isQuitting: false,
    desktopAccounts: new Map([[`${home}\0${origin}`, { account, proxy: { close: async () => { calls.push('close-proxy') } } }]]),
    gatewayProcess: null, gatewayState: { owned: false, url: null, status: 'stopped' },
    healthCheck: async () => true, publishDesktopMigrationProgress() {}, cancelGatewayUnexpectedExitRestart() {},
    confirmStoppedGatewayProcessScopes: async () => true,
    beginMigrationReconciliationIntent: async () => ({}),
    runMigrateCli: async () => { calls.push('apply'); await apply(); return { ok: true, stdout: '{}' } },
    migrationRecord: value => value, migrationReportValidationError: () => null,
    migrationFailureFromReport: () => null, migrationReportErrors: () => [], migrationTransactionIdFromReport: () => 'synthetic',
    findAppliedReceiptForIntent: async () => receipt(), decryptedSecretCache: new Map(),
    bindMigrationIntentToReceipt: async value => value, prepareImportedCredentialBackup: async value => value,
    reconcileImportedDesktopCredential: async () => ({ requiresSetup: false }), clearPendingMigrationProviderSetup: async () => {},
    clearReusableGatewayState() {}, bootError: null, bootPagePath: () => '',
    primaryDesktopHome: () => home, activeDesktopProfile: () => ({ home }),
    persistDesktopMigrationResult: async () => {},
  }
  env.waitForDesktopWriterOperations = maximum => env.desktopWriters.waitForAtMost(maximum)
  const accountGetter = new Function('env', `with (env) { return ${functions.get('desktopAccount')}; }`)(env)
  env.openOrResumeDesktopApp = async () => {
    calls.push('restart')
    await accountGetter().account.initialize()
    env.gatewayState = { owned: true, url: 'http://synthetic', status: 'ready' }
  }
  const run = new Function('env', `with (env) { return ${handler}; }`)(env)
  const requireSender = new Function('env', `with (env) { return ${functions.get('requireAccountSender')}; }`)(env)
  return { env, calls, accountGetter, requireSender, run: () => run({}, { previewId: 'preview', overwrite: true }) }
}

for (const outcome of ['success', 'failure-before-publication', 'lost-result-after-publication']) {
  test(`migration drains refresh and reloads the disk account (${outcome})`, async t => {
    const home = await mkdtemp(join(tmpdir(), 'os-account-migration-'))
    const directory = join(home, 'desktop-account', createHash('sha256').update(origin).digest('hex').slice(0, 16))
    const refresh = deferred(), started = deferred()
    const dependencies = { fetch: async url => {
      if (new URL(url).pathname !== '/api/oauth/token') throw Error('synthetic offline after restart')
      started.resolve(); await refresh.promise
      return Response.json({ token_type: 'Bearer', access_token: 'new-access', refresh_token: 'new-refresh', expires_in: 3600,
        scope: 'profile.read wallet.read managed_key.read managed_key.obtain session.revoke offline_access', account_id: 'a', session_id: 's' })
    } }
    const secrets = new LocalAccountSecrets(directory)
    const account = new TokenRhythmAccount({ directory, origin, ...dependencies, storageFile: LOCAL_ACCOUNT_FILE,
      assertSecureStorage: () => secrets.assertAvailable(),
      encrypt: value => secrets.encrypt(value), decrypt: value => secrets.decrypt(value), openExternal: async () => {} })
    let published = false
    const h = harness(account, { home, dependencies, receipt: () => published ? { report: {} } : null, apply: async () => {
      const saved = JSON.parse(secrets.decrypt(await readFile(join(directory, LOCAL_ACCOUNT_FILE), 'utf8')))
      assert.equal(saved.tokens.refreshToken, 'new-refresh')
      assert.equal(h.env.desktopAccounts.size, 0)
      assert.throws(() => h.accountGetter(), /ACCOUNT_CLOSING/, 'cannot recreate an account during publication')
      await assert.rejects(() => account.initialize(), /ACCOUNT_CLOSING/)
      if (outcome === 'failure-before-publication') throw Error('synthetic pre-publication failure')
      await rm(directory, { recursive: true, force: true })
      published = true
      if (outcome === 'lost-result-after-publication') throw Error('synthetic lost CLI result')
    } })
    t.after(async () => {
      refresh.resolve(); await account.close()
      for (const entry of h.env.desktopAccounts.values()) await entry.account.close()
      await rm(home, { recursive: true, force: true })
    })
    await account.initialize()
    account.saved.tokens = { accessToken: 'old-access', refreshToken: 'old-refresh', expiresAt: Date.now(), accountId: 'a', sessionId: 's' }
    account.saved.source = 'account'; account.saved.selectedAccountId = 'a'
    await account.persist()
    const verifying = account.verify(true)
    await started.promise
    const importing = h.run()
    await new Promise(setImmediate)
    assert.deepEqual(h.calls, [])
    assert.throws(() => h.requireSender({}), /ACCOUNT_CLOSING/)
    refresh.resolve()
    const result = await importing
    await verifying
    const replacement = h.accountGetter().account
    assert.notEqual(replacement, account)
    assert.equal(h.env.desktopMigrationBusy, false)
    assert.equal(h.env.desktopWriters.closed, false)
    assert.equal(result.migrationApplied, published)
    assert.equal(result.ok, published)
    await account.verify(true)
    if (published) {
      assert.equal(replacement.snapshot().status, 'signed_out')
      assert.equal(replacement.snapshot().source, 'manual')
      assert.equal(await exists(join(directory, LOCAL_ACCOUNT_FILE)), false)
      assert.equal(await exists(join(directory, 'account.local.key')), false)
    } else {
      assert.equal(replacement.saved.tokens.refreshToken, 'new-refresh')
    }
  })
}

for (const failure of ['persistence', 'timeout']) {
  test(`migration aborts before publication when account drain fails (${failure})`, async () => {
    const drain = deferred()
    let resumed = false
    const h = harness({ pauseForMaintenance: () => ({ drained: drain.promise, resume() { resumed = true } }) }, { timeoutMs: 20 })
    const running = h.run()
    if (failure === 'persistence') drain.reject(Error('synthetic persistence failure'))
    const keepAlive = setTimeout(() => {}, 2000)
    let result
    try { result = await running } finally { clearTimeout(keepAlive); drain.resolve() }
    assert.equal(result.ok, false)
    assert.equal(result.failureStage, 'preflight')
    assert.deepEqual(h.calls, [])
    assert.equal(h.env.desktopAccounts.size, 1)
    assert.equal(h.env.desktopWriters.closed, false)
    assert.equal(h.env.desktopMigrationBusy, false)
    assert.equal(resumed, true)
  })
}
