import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { access, mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import ts from '@typescript/typescript6'
import { DesktopWriterAdmission } from '../dist/desktop-writer-admission.js'
import { AccountError, TokenRhythmAccount, pauseAccountsForMaintenance } from '../dist/tokenrhythm-account.js'

const source = ts.createSourceFile('main.js', readFileSync(
  new URL('../dist/main.js', import.meta.url), 'utf8',
), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS)
const functions = new Map(source.statements.filter(ts.isFunctionDeclaration)
  .filter(node => node.name).map(node => [node.name.text, node.getText(source)]))
const deferred = () => {
  let resolve, reject
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}
const exists = path => access(path).then(() => true, () => false)

function harness(account, { home = '/synthetic-profile', mode = 'delete-current-profile', apply = async () => ({ outcome: 'complete' }), timeoutMs = 1000 } = {}) {
  const calls = []
  const report = { mode }
  const env = {
    AccountError, pauseAccountsForMaintenance,
    AbortSignal: { timeout: () => AbortSignal.timeout(timeoutMs) },
    activeDesktopProfile: () => ({ home }), confirmDesktopCleanup: async () => true,
    desktopWriters: new DesktopWriterAdmission(), desktopCleanupBusy: false, desktopMigrationBusy: false,
    onboardingFlows: { active: null }, desktopProfileKey: () => 'same-profile',
    gatewayProcess: null, gatewayState: { owned: false, url: null },
    isQuitting: false, updateApplying: false, allowGracefulShutdownWhileQuitting: false,
    stopOwnedGatewayAndWait: async () => { calls.push('stop-gateway') },
    cleanupSelectorArgs: () => [], sameDesktopCleanupScope: () => true,
    desktopAccounts: new Map([['profile', { account, proxy: { close: async () => { calls.push('close-proxy') } } }]]),
    runDesktopCleanupCli: async (_active, command) => {
      calls.push(command)
      if (command === 'cleanup-inspect') return { outcome: 'ready', transaction_id: 'synthetic', revision: 1 }
      return await apply()
    },
    decryptedSecretCache: new Map(), appExitPhase: '', desktopReliabilityTelemetry: { abandonSession() {} },
    destroyWindowsTray() {}, app: { getPath: () => home, exit: () => calls.push('exit') },
    restoreAfterIncompleteCleanup: async () => { env.isQuitting = false; calls.push('restore') },
    transientPendingMigrationProviderSetup: null, forceOnboardingOnNextStartup: false,
    clearReusableGatewayState() {}, bootError: null, setImmediate,
    currentMainWindow: () => ({ loadFile: async () => {} }), bootPagePath: () => '', openOrResumeDesktopApp() {},
    trustedMainWindowControlIpc: () => true,
  }
  env.waitForDesktopWriterOperations = maximum => env.desktopWriters.waitForAtMost(maximum)
  const cleanup = new Function('env', `with (env) { return ${functions.get('applyApprovedDesktopCleanup')}; }`)(env)
  const requireSender = new Function('env', `with (env) { return ${functions.get('requireAccountSender')}; }`)(env)
  return { env, calls, requireSender, apply: () => cleanup({ report, selection: {} }, { acknowledged: true, confirmation: '' }) }
}

for (const outcome of ['complete', 'partial', 'error']) {
  test(`profile cleanup drains a token refresh and cannot recreate deleted account files (${outcome})`, async t => {
    const home = await mkdtemp(join(tmpdir(), 'os-account-cleanup-'))
    const directory = join(home, 'desktop-account', 'synthetic-origin')
    const refresh = deferred(), started = deferred()
    const account = new TokenRhythmAccount({
      directory, origin: 'https://tokenrhythm.studio', assertSecureStorage() {},
      encrypt: value => Buffer.from(value).toString('base64'), decrypt: value => Buffer.from(value, 'base64').toString(),
      openExternal: async () => {}, fetch: async url => {
        assert.equal(new URL(url).pathname, '/api/oauth/token')
        started.resolve()
        await refresh.promise
        return Response.json({ token_type: 'Bearer', access_token: 'synthetic-new-access', refresh_token: 'synthetic-new-refresh', expires_in: 3600,
          scope: 'profile.read wallet.read managed_key.read managed_key.obtain session.revoke offline_access', account_id: 'a', session_id: 's' })
      },
    })
    t.after(async () => { refresh.resolve(); await account.close(); await rm(home, { recursive: true, force: true }) })
    await account.initialize()
    account.saved.tokens = { accessToken: 'synthetic-old-access', refreshToken: 'synthetic-old-refresh', expiresAt: Date.now(), accountId: 'a', sessionId: 's' }
    await account.persist()
    const verifying = account.verify(true)
    await started.promise
    const h = harness(account, { home, apply: async () => {
      const saved = JSON.parse(Buffer.from(await readFile(join(directory, 'account.enc'), 'utf8'), 'base64').toString())
      assert.equal(saved.tokens.refreshToken, 'synthetic-new-refresh')
      assert.equal(h.env.desktopAccounts.size, 0, 'retire memory state before destructive cleanup')
      await assert.rejects(() => account.login(), /ACCOUNT_CLOSING/)
      await rm(home, { recursive: true, force: true })
      if (outcome === 'error') throw Error('synthetic cleanup failure after deletion')
      return { outcome }
    } })
    const cleanup = h.apply()
    await new Promise(setImmediate)
    assert.deepEqual(h.calls, [])
    assert.throws(() => h.requireSender({}), /ACCOUNT_CLOSING/)
    assert.equal(await exists(home), true)
    refresh.resolve()
    const result = await cleanup
    await verifying
    await account.verify(true)
    await new Promise(setImmediate)
    assert.equal(result.ok, outcome === 'complete')
    assert.equal(await exists(home), false)
    assert.equal(h.calls.includes('exit'), outcome === 'complete')
    assert.equal(h.calls.includes('restore'), outcome !== 'complete')
    assert.equal(h.env.desktopAccounts.size, 0)
  })
}

test('resetting settings pauses accounts through cleanup and resumes them after reopening admission', async () => {
  const drain = deferred(), calls = []
  let h
  const account = {
    pauseForMaintenance() {
      calls.push('pause')
      return { drained: drain.promise, resume() {
        assert.equal(h.env.desktopCleanupBusy, false)
        assert.equal(h.env.desktopWriters.closed, false)
        calls.push('resume')
      } }
    },
    close: async () => { calls.push('close') },
  }
  h = harness(account, { mode: 'reset-current-settings' })
  const cleanup = h.apply()
  await new Promise(setImmediate)
  assert.deepEqual(calls, ['pause'])
  assert.deepEqual(h.calls, [])
  drain.resolve()
  assert.equal((await cleanup).ok, true)
  assert.deepEqual(calls, ['pause', 'resume'])
  assert.equal(h.env.desktopAccounts.size, 1)
  assert.equal(h.calls.includes('exit'), false)
})

test('account drain failure prevents deletion and releases the cleanup hold', async () => {
  let resumed = false
  const h = harness({ pauseForMaintenance: () => ({ drained: Promise.reject(Error('synthetic write failure')), resume() { resumed = true } }) })
  const result = await h.apply()
  assert.equal(result.ok, false)
  assert.match(result.detail, /synthetic write failure/)
  assert.deepEqual(h.calls, ['restore'])
  assert.equal(resumed, true)
  assert.equal(h.env.desktopWriters.closed, false)
})
