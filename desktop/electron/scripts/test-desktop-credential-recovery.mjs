import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'
import vm from 'node:vm'
import ts from '@typescript/typescript6'
import { OnboardingFlowCoordinator } from '../dist/onboarding-flow-coordinator.js'
import { OnboardingSaveTelemetry } from '../dist/onboarding-save-telemetry.js'

const source = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const ast = ts.createSourceFile('main.ts', source, ts.ScriptTarget.Latest, true)
function functions(...names) {
  return names.map(name => {
    const node = ast.statements.find(item => ts.isFunctionDeclaration(item) && item.name?.text === name)
    assert.ok(node, name)
    return ts.transpileModule(node.getText(ast), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText
  }).join('\n')
}

for (const failure of [null, 'account', 'settings']) {
  test(`account recovery completes only after saving the account primary (${failure ?? 'success'})`, async () => {
    const handlers = new Map()
    const flow = { state: 'editing', savePayload: null, savePromise: null }
    const onboardingFlows = new OnboardingFlowCoordinator()
    onboardingFlows.activate(flow)
    let provider = 'openai'
    let selected = false
    let completed = false
    let saves = 0
    const context = vm.createContext({
      ipcMain: { handle: (name, handler) => handlers.set(name, handler) },
      onboardingFlows, trustedOnboardingIpc: () => true, AccountError: Error,
      desktopAccount: () => ({ account: { selectSource: async source => {
        assert.equal(source, 'account')
        if (failure === 'account') throw new Error('synthetic account verification failure')
        selected = true
      } } }),
      dismissOnboardingFlow: () => { completed = true },
      OnboardingSaveTelemetry, onboardingSaveTelemetryAttempt: 0, app: { isPackaged: false },
      desktopLog() {}, desktopProfileKey: () => 'primary', gatewayProcess: null,
      refreshPrimaryRecoveryAfterImportAttempt: async () => false,
      beginDesktopWriterOperation: () => () => {},
      readPendingMigrationProviderSetup: async () => null, invalidateSecretStorageBackendCache() {},
      saveDesktopCredential: async (payload, reserved, onboarding, accountPrimary) => {
        saves++
        assert.equal(selected, true)
        assert.equal(reserved, true)
        assert.equal(onboarding, true)
        assert.equal(accountPrimary, true)
        assert.equal(payload.provider, 'tokenrhythm')
        if (failure === 'settings') throw new Error('synthetic settings transaction failure')
        provider = payload.provider
        return { provider }
      },
      applyDesktopLocaleChoice() {}, clearPendingMigrationProviderSetup: async () => {},
      desktopGrowthTelemetry: { recordOnboardingCompleted() {} },
      desktopWriters: { closed: false }, isQuitting: false, appExitPhase: 'running',
      completeOnboardingFlow: current => { completed = onboardingFlows.complete(current); return completed },
      onboardingSaveFailure: (code, error) => ({ ok: false, code, error }),
    })
    const handler = ast.statements.find(node => ts.isExpressionStatement(node)
      && ts.isCallExpression(node.expression) && node.expression.arguments[0]?.text === 'desktop:onboarding:account')
    assert.ok(handler)
    vm.runInContext(functions('performOnboardingSave') + ts.transpileModule(handler.getText(ast), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText, context)
    const result = handlers.get('desktop:onboarding:account')({})
    const duplicate = handlers.get('desktop:onboarding:account')({})
    if (failure) {
      await Promise.all([assert.rejects(result, /synthetic/), assert.rejects(duplicate, /synthetic/)])
      assert.equal(provider, 'openai')
      assert.equal(completed, false)
      assert.equal(flow.state, 'editing')
    } else {
      const responses = await Promise.all([result, duplicate])
      assert.ok(responses.every(response => response.ok === true))
      assert.equal(provider, 'tokenrhythm')
      assert.equal(completed, true)
    }
    assert.equal(saves, failure === 'account' ? 0 : 1, 'duplicate clicks must share the same save')
  })
}

test('a stopped startup settles after stdio closes without publishing over its replacement', () => {
  let callback
  const visit = node => {
    if (ts.isCallExpression(node) && node.arguments[0]?.text === 'close'
      && node.arguments[1]?.getText(ast).includes('const classifiedMessage = classifyGatewayExitMessage')) {
      callback = node.arguments[1]
    }
    ts.forEachChild(node, visit)
  }
  visit(ast)
  assert.ok(callback)
  const child = { pid: 1 }
  const replacement = { pid: 2 }
  const context = vm.createContext({
    child, gatewayProcess: replacement, gatewayOutputTail: '', childExitMessage: null,
    gatewayReadyProcesses: new Map(), gatewayStoppingProcesses: new Set([child]),
    gatewayExitLooksLikePortInUse: () => false,
    classifyGatewayExitMessage: message => message,
    desktopLog() {}, writeLogLine() {}, closeLogStream() {}, isQuitting: false,
  })
  vm.runInContext(ts.transpileModule(`const onClose = ${callback.getText(ast)}; onClose(1, null)`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context)
  assert.match(context.childExitMessage, /gateway exited code=1/)
  assert.equal(context.gatewayProcess, replacement)
})

test('one unreadable credential cannot hide a usable sibling or rewrite the saved record', () => {
  for (const failed of ['encryptedApiKey', 'encryptedSearchApiKey']) {
    const record = Object.freeze({ encryptedApiKey: 'original-model', encryptedSearchApiKey: 'original-search' })
    const errors = []
    const context = vm.createContext({
      activeDesktopProfile: () => ({ home: 'synthetic-profile' }),
      unreadableDesktopCredentials: new Map(),
      decryptApiKey: () => {
        if (failed === 'encryptedApiKey') throw new Error('synthetic model decryption failure')
        return 'synthetic-model-key'
      },
      decryptSearchApiKey: () => {
        if (failed === 'encryptedSearchApiKey') throw new Error('synthetic search decryption failure')
        return 'synthetic-search-key'
      },
      desktopLog: (event, details) => errors.push({ event, details }),
    })
    vm.runInContext(functions('readDesktopStartupSecret', 'desktopCredentialNeedsRecovery'), context)
    assert.equal(context.readDesktopStartupSecret(record, 'encryptedApiKey'), failed === 'encryptedApiKey' ? '' : 'synthetic-model-key')
    assert.equal(context.readDesktopStartupSecret(record, 'encryptedSearchApiKey'), failed === 'encryptedSearchApiKey' ? '' : 'synthetic-search-key')
    assert.equal(context.desktopCredentialNeedsRecovery(), true)
    assert.deepEqual(Object.keys(context.unreadableDesktopCredentials.get('synthetic-profile')), [failed])
    assert.equal(errors.length, 1)
    assert.doesNotMatch(JSON.stringify(errors), /original-model|original-search|synthetic-model-key|synthetic-search-key/)
  }
})

for (const failure of ['backup', 'transaction', 'conflict', null]) {
  test(`credential replacement preserves the original before publishing (${failure ?? 'success'})`, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'opensquilla-credential-recovery-'))
    const profile = { home: join(directory, 'opensquilla'), credentialPath: join(directory, 'desktop-credential.json') }
    const original = '{"encryption":"safeStorage","encryptedApiKey":"synthetic-original-ciphertext"}'
    await writeFile(profile.credentialPath, original)
    let transactions = 0
    let cacheCleared = false
    const failures = new Map([[profile.home, { encryptedApiKey: 'synthetic-original-ciphertext' }]])
    const context = vm.createContext({
      Error, dirname, join, randomUUID,
      desktopProfileKey: () => 'primary', gatewayProcess: null, gatewayState: { url: '' },
      preflightDesktopConfigWrite: async () => ({ transaction_id: 'synthetic-transaction', revision: 1 }),
      readOptionalDesktopText: async path => path === profile.credentialPath
        ? failure === 'conflict' ? 'changed-by-another-writer' : original : 'config_version = 2\n',
      desktopCredentialNeedsRecovery: () => true,
      writeFile: async (...args) => {
        assert.equal(transactions, 0, 'backup must precede any transaction publication')
        assert.equal(args[2].flag, 'wx')
        assert.equal(args[2].flush, true)
        if (failure === 'backup') throw new Error('synthetic backup failure')
        await writeFile(...args)
      },
      renderDesktopConfigAfterPreflight: () => 'config_version = 2\n',
      runRecoveryCli: async () => {
        transactions++
        const backups = (await readdir(directory)).filter(name => name.startsWith('desktop-credential.unreadable-'))
        assert.equal(backups.length, 1)
        assert.equal(await readFile(join(directory, backups[0]), 'utf8'), original)
        if (failure === 'transaction') throw new Error('synthetic publication failure')
        return { outcome: 'ready' }
      },
      recoveryInspection: null, primaryRecoveryInspection: null, publishRecoveryState() {},
      decryptedSecretCache: { clear: () => { cacheCleared = true } },
      unreadableDesktopCredentials: failures,
      desktopLocale: 'en',
    })
    vm.runInContext(functions('applyDesktopSettingsPair'), context)
    try {
      const save = context.applyDesktopSettingsPair(profile, {}, 'replacement', original)
      if (failure) await assert.rejects(save, /synthetic|changed while settings/)
      else await save
      assert.equal(await readFile(profile.credentialPath, 'utf8'), original)
      assert.equal(transactions, failure === 'backup' || failure === 'conflict' ? 0 : 1)
      assert.equal(cacheCleared, failure === null)
      assert.equal(failures.has(profile.home), failure !== null)
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  })
}
