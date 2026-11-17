// Native Windows acceptance: real main.js, recovery CLI, Gateway and safeStorage.
// Prerequisites: locked Desktop/WebUI builds, Python development environment,
// and verified LFS router assets. No account or provider credentials are used.
// --baseline runs the same cases against --repo=<v0.5.8 source checkout> and
// asserts the known failures. Evidence is retained under --output=<new folder>.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdir, mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import electronBinary from 'electron'
import { _electron } from 'playwright'
import { closeElectronWithDeadline, requestElectronQuitWithConfirmation } from './e2e-shutdown-helpers.mjs'

assert.equal(process.platform, 'win32', 'This acceptance test requires native Windows')
const option = name => process.argv.find(value => value.startsWith(`--${name}=`))?.slice(name.length + 3)
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repo = resolve(option('repo') || join(packageRoot, '../..'))
const baseline = process.argv.includes('--baseline')
const cases = (option('cases') || (baseline
  ? 'fresh,early,account,model,search,interrupted'
  : 'fresh,early,account,model,search,both,interrupted,repair')).split(',')
assert.ok(cases.every(name => ['fresh', 'early', 'account', 'model', 'search', 'both', 'interrupted', 'repair'].includes(name)))
const root = option('output') ? resolve(option('output')) : await mkdtemp(join(tmpdir(), 'opensquilla-startup-recovery-'))
await mkdir(root, { recursive: true })
const environment = process.env.UV_PROJECT_ENVIRONMENT
const reports = []

async function readEvents(userData) {
  const raw = await readFile(join(userData, 'logs', 'desktop.log'), 'utf8').catch(error => {
    if (error.code !== 'ENOENT') throw error
    return ''
  })
  return raw.trim().split('\n').flatMap(line => {
    try { return [JSON.parse(line)] } catch { return [] } // the live final line may be incomplete
  })
}

async function waitFor(check, label) {
  const deadline = Date.now() + 90_000
  while (Date.now() < deadline) {
    const value = await check()
    if (value) return value
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${label}`)
}

async function close(app, phase) {
  const result = await closeElectronWithDeadline({
    app, phase,
    requestClose: options => requestElectronQuitWithConfirmation(app, { ...options, requireConfirmation: false }),
  })
  assert.equal(result.closed, true, JSON.stringify(result))
  assert.equal(result.forcedExitSucceeded, false)
  return result
}

async function launchEnvironment(directory) {
  const osHome = join(directory, 'os-home')
  for (const path of ['AppData/Roaming', 'AppData/Local/Temp']) await mkdir(join(osHome, path), { recursive: true })
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !(
    /^(OPENSQUILLA_|ELECTRON_|UV_|PYTHON)/i.test(name)
    || /API.?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|PROXY/i.test(name)
    || /^(AWS|AZURE|GOOGLE|ANTHROPIC|OPENAI|OPENROUTER|HF|HUGGINGFACE)_/i.test(name)
  )))
  const probe = createServer()
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve))
  const port = probe.address().port
  await new Promise(resolve => probe.close(resolve))
  return {
    ...env, HOME: osHome, USERPROFILE: osHome,
    APPDATA: join(osHome, 'AppData/Roaming'), LOCALAPPDATA: join(osHome, 'AppData/Local'),
    TEMP: join(osHome, 'AppData/Local/Temp'), TMP: join(osHome, 'AppData/Local/Temp'),
    OPENSQUILLA_DESKTOP_REPO_ROOT: repo, OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(port), OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'safeStorage',
    OPENSQUILLA_USER_STATE_DIR: join(directory, 'locks'), OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    ...(environment ? { UV_PROJECT_ENVIRONMENT: environment, UV_NO_SYNC: '1' } : { UV_FROZEN: '1' }),
    UV_OFFLINE: '1', PYTHONPATH: join(repo, 'src'), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8',
    HTTP_PROXY: 'http://127.0.0.1:1', HTTPS_PROXY: 'http://127.0.0.1:1',
    ALL_PROXY: 'http://127.0.0.1:1', NO_PROXY: '127.0.0.1,localhost',
  }
}

async function runCase(name) {
  const directory = join(root, name)
  await mkdir(directory) // Never reuse a profile or overwrite earlier evidence.
  const userData = join(directory, 'user-data')
  const home = join(userData, 'opensquilla')
  await mkdir(userData)
  await writeFile(join(userData, 'desktop-locale'), 'en')
  const env = await launchEnvironment(directory)
  const report = { name, baseline, repo, attempts: [], evidence: directory }
  const originals = new Map()
  const preserve = async (path, data) => { await writeFile(path, data); originals.set(path, Buffer.from(data)) }
  const python = (code, ...args) => {
    const child = spawnSync('uv', ['run', 'python', '-c', code, ...args], {
      cwd: repo, env: { ...env, OPENSQUILLA_PROFILE_KIND: 'desktop-primary' },
      encoding: 'utf8', windowsHide: true, timeout: 30_000,
    })
    assert.equal(child.status, 0, child.stderr || String(child.error))
    return child.stdout
  }
  const history = action => python(`import asyncio, sys
from opensquilla.session.storage import SessionStorage
from opensquilla.session.manager import SessionManager
async def main():
    async with SessionStorage(sys.argv[1]) as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        key = 'agent:main:startup-preservation'
        if sys.argv[2] == 'seed':
            await manager.create(key)
            await manager.append_message(key, 'user', 'synthetic retained question')
            await manager.append_message(key, 'assistant', 'synthetic retained answer')
        entries = await manager.get_transcript(key)
        assert [(item.role, item.content) for item in entries] == [
            ('user', 'synthetic retained question'), ('assistant', 'synthetic retained answer')]
asyncio.run(main())
`, join(home, 'state', 'sessions.db'), action)
  const visibleHistory = async () => {
    const page = await waitFor(() => app.windows().find(item => item.url().startsWith('opensquilla-app://desktop/')), 'desktop history renderer')
    const url = new URL('/chat', page.url())
    url.searchParams.set('session', 'agent:main:startup-preservation')
    await page.goto(url.toString(), { waitUntil: 'domcontentloaded' })
    await page.getByText('synthetic retained question', { exact: true }).first().waitFor({ timeout: 30_000 })
    await page.getByText('synthetic retained answer', { exact: true }).first().waitFor({ timeout: 30_000 })
  }
  let app
  try {
    const bootstrap = join(directory, 'safe-storage.cjs')
    await writeFile(bootstrap, "const {app}=require('electron'); app.whenReady();\n")
    app = await _electron.launch({ executablePath: electronBinary, args: [`--user-data-dir=${userData}`, bootstrap], env })
    const native = await app.evaluate(async ({ app, safeStorage }) => {
      await app.whenReady()
      const valid = safeStorage.encryptString('synthetic-original-key')
      const corrupt = Buffer.from(valid)
      corrupt[corrupt.length - 1] ^= 255
      let error = ''
      try { safeStorage.decryptString(corrupt) } catch (failure) { error = failure.message }
      return { platform: process.platform, version: process.versions.electron,
        available: safeStorage.isEncryptionAvailable(), roundtrip: safeStorage.decryptString(valid),
        ciphertext: corrupt.toString('base64'), validCiphertext: valid.toString('base64'), error }
    })
    assert.equal(native.roundtrip, 'synthetic-original-key')
    assert.equal(native.available, true)
    assert.equal(native.error, 'Error while decrypting the ciphertext provided to safeStorage.decryptString.')
    report.native = { platform: native.platform, version: native.version, error: native.error }
    await app.close()
    app = null

    if (['model', 'search', 'both', 'repair'].includes(name)) {
      await mkdir(join(home, 'workspace'), { recursive: true })
      await mkdir(join(home, 'state'))
      const provider = 'openai'
      const config = `config_version = 2\n[llm]\nprovider = "${provider}"\nmodel = "synthetic-model"\napi_key_env = "OPENAI_API_KEY"\nbase_url = "http://127.0.0.1:1"\n[squilla_router]\nenabled = false\n[control_ui]\nenabled = true\n`
      await preserve(join(home, 'config.toml'), config)
      await preserve(join(home, 'workspace', 'preserve.txt'), 'synthetic workspace')
      await preserve(join(home, 'state', 'preserve.json'), 'synthetic history')
      await preserve(join(userData, 'desktop-credential.json'), JSON.stringify({
        provider, model: 'synthetic-model', baseUrl: 'http://127.0.0.1:1', apiKeyEnv: 'OPENAI_API_KEY',
        encryption: 'safeStorage', encryptedApiKey: name === 'search' ? native.validCiphertext : native.ciphertext,
        encryptedSearchApiKey: name === 'model' ? native.validCiphertext : native.ciphertext,
        searchProvider: 'brave', searchApiKeyEnv: 'BRAVE_API_KEY',
        routerMode: 'disabled', configAuthority: 'generated',
      }))
    }
    if (name === 'account') {
      const account = join(home, 'desktop-account', '0123456789abcdef')
      await mkdir(account, { recursive: true })
      await mkdir(join(home, 'workspace'))
      await mkdir(join(home, 'state'))
      await preserve(join(account, 'installation-id'), '11234567-89ab-4cde-8fab-0123456789ab')
    }
    if (name === 'interrupted') {
      const code = `import sys
from pathlib import Path
from opensquilla.recovery import config_recovery, inspect_profile
home = Path(sys.argv[1])
(home / 'workspace').mkdir(parents=True)
(home / 'state').mkdir()
(home / 'config.toml').write_bytes(b'broken = [\\n')
park = config_recovery._park_corrupt_config
def interrupt(path):
    park(path)
    raise RuntimeError('synthetic interruption after park')
config_recovery._park_corrupt_config = interrupt
try:
    config_recovery.recover_config(home)
except RuntimeError as error:
    print(str(error))
assert not (home / 'config.toml').exists()
print(inspect_profile(home).stable_code)
`
      const child = spawnSync('uv', ['run', 'python', '-c', code, home], {
        cwd: repo, env: { ...env, OPENSQUILLA_PROFILE_KIND: 'desktop-primary' },
        encoding: 'utf8', windowsHide: true, timeout: 30_000,
      })
      report.interruption = { status: child.status, stdout: child.stdout, stderr: child.stderr }
      assert.equal(child.status, 0, child.stderr)
      const parked = (await readdir(home)).find(file => file.startsWith('config.toml.corrupt.'))
      assert.ok(parked)
      originals.set(join(home, parked), Buffer.from('broken = [\n'))
      await preserve(join(home, 'workspace', 'preserve.txt'), 'synthetic workspace')
      await preserve(join(home, 'state', 'preserve.json'), 'synthetic history')
    }
    const hasHistory = ['model', 'search', 'both', 'repair', 'interrupted'].includes(name)
    if (hasHistory) history('seed')

    const start = async () => {
      const instance = await _electron.launch({
        executablePath: electronBinary, args: [`--user-data-dir=${userData}`, join(repo, 'desktop/electron')], env,
      })
      await instance.evaluate(({ shell, dialog }, repair) => {
        // Authentication remains local: verify the actual login IPC generated
        // the authorization URL, without opening a real browser/account.
        shell.openExternal = async url => { globalThis.syntheticAuthorizationUrl = url }
        globalThis.credentialRecoveryNotices = []
        const original = dialog.showMessageBox.bind(dialog)
        dialog.showMessageBox = async (...args) => {
          const options = args.at(-1)
          if (options.title !== 'Saved credentials need attention') return original(...args)
          globalThis.credentialRecoveryNotices.push(options)
          return { response: repair ? 0 : 1, checkboxChecked: false }
        }
      }, name === 'repair')
      return instance
    }
    app = await start()
    if (name === 'early') {
      const page = await app.firstWindow()
      await page.waitForFunction(() => Boolean(window.opensquillaDesktop?.accountAction))
      const state = await page.evaluate(() => window.opensquillaDesktop.accountAction('login'))
      assert.equal(state.status, 'authorizing')
      report.earlyLogin = state.status
    }
    const failedBaseline = baseline && name !== 'fresh'
    const terminal = async index => waitFor(async () => {
      for (const page of app.windows()) {
        if (!page.isClosed() && page.url().startsWith('data:text/html') && name !== 'repair') {
          try {
            await page.evaluate(() => window.opensquillaDesktop?.skipOnboarding())
          } catch (error) {
            // Skipping destroys this optional wizard before its IPC reply can
            // reach Playwright. Other errors still fail, and startup must still
            // produce the real successful terminal event below.
            if (!page.isClosed()) throw error
          }
        }
      }
      return (await readEvents(userData)).filter(event => event.event === 'desktop_open_failed'
        || (event.event === 'startup_stage_attempt_finished' && event.outcome === 'success'))[index]
    }, `${name} startup ${index + 1}`)

    let restoredAfterFailedSave = 0
    for (let index = 0; index < 3; index++) {
      const event = await terminal(index + restoredAfterFailedSave)
      report.attempts.push(event)
      if (failedBaseline) {
        assert.equal(event.event, 'desktop_open_failed')
        assert.match(event.error, ['model', 'search', 'both', 'repair'].includes(name)
          ? /safeStorage.decryptString/ : /profile_not_fresh/)
      } else {
        assert.equal(event.outcome, 'success', JSON.stringify(event))
      }
      if (index === 0 && name === 'repair') {
        const wizard = await waitFor(() => app.windows().find(page => page.url().startsWith('data:text/html')), 'credential recovery setup')
        // A partial replacement must fail without discarding either original.
        await assert.rejects(wizard.evaluate(() => window.opensquillaDesktop.saveOnboarding({
          provider: 'openai', model: 'synthetic-model', apiKey: 'synthetic-replacement-model-key',
          searchProvider: 'brave',
        })), /safeStorage.decryptString/)
        for (const [path, bytes] of originals) assert.deepEqual(await readFile(path), bytes, path)
        report.partialReplacementPreserved = true
        report.partialReplacementRestart = await terminal(1)
        assert.equal(report.partialReplacementRestart.outcome, 'success')
        restoredAfterFailedSave = 1
        const result = await wizard.evaluate(() => window.opensquillaDesktop.saveOnboarding({
          provider: 'openai', model: 'synthetic-model', baseUrl: 'http://127.0.0.1:1',
          apiKey: 'synthetic-replacement-model-key', modelRoutingMode: 'direct', routerMode: 'disabled',
          searchProvider: 'brave', searchApiKey: 'synthetic-replacement-search-key',
        }))
        assert.equal(result.ok, true, JSON.stringify(result))
        report.reentered = true
        const saved = JSON.parse(await readFile(join(userData, 'desktop-credential.json'), 'utf8'))
        assert.equal(saved.encryption, 'safeStorage')
        const roundtrip = await app.evaluate(({ safeStorage }, record) => ({
          model: safeStorage.decryptString(Buffer.from(record.encryptedApiKey, 'base64')),
          search: safeStorage.decryptString(Buffer.from(record.encryptedSearchApiKey, 'base64')),
        }), saved)
        assert.deepEqual(roundtrip, { model: 'synthetic-replacement-model-key', search: 'synthetic-replacement-search-key' })
        const backups = (await readdir(userData)).filter(file => file.startsWith('desktop-credential.unreadable-'))
        assert.ok(backups.length > 0)
        assert.ok((await Promise.all(backups.map(file => readFile(join(userData, file)))))
          .some(bytes => bytes.equals(originals.get(join(userData, 'desktop-credential.json')))))
        originals.delete(join(userData, 'desktop-credential.json'))
        originals.delete(join(home, 'config.toml'))
        // Saving settings initiates the second startup itself.
      } else if (index < 2) {
        const page = await app.firstWindow()
        const restarted = await page.evaluate(() => window.opensquillaDesktop.retryStartup())
        assert.equal(restarted.ok, true)
      }
    }
    report.notices = await app.evaluate(() => globalThis.credentialRecoveryNotices)
    if (!baseline && ['model', 'search', 'both', 'repair'].includes(name)) assert.equal(report.notices.length, 1)
    for (const [path, bytes] of originals) assert.deepEqual(await readFile(path), bytes, path)
    if (name === 'early') {
      assert.equal(await app.evaluate(() => Boolean(globalThis.syntheticAuthorizationUrl)), true)
      if (!baseline) assert.ok((await readFile(join(home, 'config.toml'), 'utf8')).includes('[llm]'))
    }
    if (hasHistory && !baseline) { await visibleHistory(); report.historyVisible = true }
    report.shutdown = await close(app, name)
    app = null
    if (hasHistory) { history('verify'); report.historyPreserved = true }
    // A full process restart must also preserve the repaired state.
    if (!baseline) {
      const previous = report.attempts.length + restoredAfterFailedSave
      app = await start()
      const event = await terminal(previous)
      assert.equal(event.outcome, 'success', JSON.stringify(event))
      report.coldRestart = event
      for (const [path, bytes] of originals) assert.deepEqual(await readFile(path), bytes, path)
      if (hasHistory) { await visibleHistory(); report.coldHistoryVisible = true }
    }
  } catch (error) {
    report.error = String(error.stack || error)
    throw error
  } finally {
    if (app) report.finalShutdown = await close(app, name)
    if (!report.error && report.historyPreserved) { history('verify'); report.coldHistoryPreserved = true }
    await writeFile(join(directory, 'result.json'), JSON.stringify(report, null, 2))
    reports.push(report)
  }
  console.log(`${name}: ${baseline ? 'baseline verified' : 'passed'}, evidence: ${directory}`)
}

try {
  for (const name of cases) await runCase(name)
} finally {
  await writeFile(join(root, 'results.json'), JSON.stringify(reports, null, 2))
}
