import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { EventEmitter } from 'node:events'
import { access, readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { win32 } from 'node:path'
import {
  WINDOWS_INSTALL_REGISTRY_KEY,
  WINDOWS_INSTALL_LOCATIONS_SCRIPT,
  assertUnambiguousWindowsInstallation,
  launchWindowsInstaller,
} from '../dist/windows-update-handoff.js'
import { runWindowsPowerShellJson } from '../dist/windows-update-security.js'

const require = createRequire(import.meta.url)
const { UUID } = require('builder-util-runtime')
const packageJson = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
const guid = packageJson.build.nsis.guid || UUID.v5(packageJson.build.appId, UUID.parse('50e065bc-3134-11e6-9bab-38c9862bdaf3'))
assert.equal(WINDOWS_INSTALL_REGISTRY_KEY, `Software\\${guid}`)
const builderSource = await readFile(require.resolve('app-builder-lib/out/targets/nsis/NsisTarget.js'), 'utf8')
assert.match(builderSource, /50e065bc-3134-11e6-9bab-38c9862bdaf3/, 'recheck registry identity if the locked builder changes its UUID namespace')
if (process.platform === 'win32') {
  assert.deepEqual(await runWindowsPowerShellJson(WINDOWS_INSTALL_LOCATIONS_SCRIPT,
    { key: 'Software\\OpenSquilla-Update-Test-No-Such-Registration-87f68b15' }), [])
}

const executable = 'C:\\Users\\Example\\自定义 app\\OpenSquilla.exe'
for (const scope of ['CurrentUser', 'LocalMachine']) {
  await assertUnambiguousWindowsInstallation(executable, { runPowerShell: async (script, input) => {
    assert.equal(script, WINDOWS_INSTALL_LOCATIONS_SCRIPT)
    assert.deepEqual(input, { key: WINDOWS_INSTALL_REGISTRY_KEY })
    return [{ scope, path: 'C:\\Users\\Example\\自定义 app\\' }]
  } })
}
for (const entries of [[],
  [{ scope: 'CurrentUser', path: 'C:\\Other' }],
  [{ scope: 'CurrentUser', path: 'C:\\Users\\Example\\自定义 app' }, { scope: 'LocalMachine', path: 'C:\\Machine' }],
  [{ scope: 'CurrentUser', path: 'C:\\Users\\Example\\自定义 app' }, { scope: 'LocalMachine', path: 'C:\\Users\\Example\\自定义 app' }],
]) await assert.rejects(assertUnambiguousWindowsInstallation(executable, { runPowerShell: async () => entries }),
  (error) => error.code === 'installation_ambiguous')
await assert.rejects(assertUnambiguousWindowsInstallation(executable, { runPowerShell: async () => ({}) }),
  (error) => error.code === 'installation_unavailable')
await assert.rejects(assertUnambiguousWindowsInstallation(executable, { runPowerShell: async () => { throw new Error('denied') } }),
  (error) => error.code === 'installation_unavailable')

const installer = "C:\\Users\\Example\\update cache\\OpenSquilla ' & 0.5.5.exe"
const child = new EventEmitter()
let unrefs = 0
child.unref = () => { unrefs += 1 }
let completed = false
const pending = launchWindowsInstaller(installer, { spawn: (path, args, options) => {
  assert.equal(path, installer)
  assert.deepEqual(args, ['--updated'], 'keep the assisted wizard and registered installation scope')
  assert.equal(options.shell, false)
  assert.equal(options.detached, true)
  assert.equal(options.stdio, 'ignore')
  assert.equal(options.windowsHide, false)
  assert.equal(options.cwd, 'C:\\Users\\Example\\update cache')
  return child
} }).then(() => { completed = true })
await Promise.resolve()
assert.equal(completed, false, 'returning ChildProcess is not a successful spawn')
assert.equal(unrefs, 0)
child.emit('spawn')
await pending
assert.equal(completed, true)
assert.equal(unrefs, 1)
child.emit('error', new Error('post-handoff event'))
await assert.rejects(launchWindowsInstaller(installer, { spawn: () => { throw new Error('sync failure') } }),
  (error) => error.code === 'install_failed')
const failed = new EventEmitter()
failed.unref = () => assert.fail('failed process must not unref as success')
const failure = launchWindowsInstaller(installer, { spawn: () => failed })
failed.emit('error', Object.assign(new Error('missing'), { code: 'ENOENT' }))
await assert.rejects(failure, (error) => error.code === 'install_failed')

if (process.platform === 'win32') {
  // Exercise the OS process-creation boundary with the existing test runner.
  // Node rejects --updated before running a script. Never substitute an
  // installer or the current Electron client for this harmless child.
  assert.equal(win32.basename(process.execPath).toLowerCase(), 'node.exe')
  assert.equal(process.versions.electron, undefined)
  const childEnv = { ...process.env }
  delete childEnv.NODE_OPTIONS
  const assertLaunchArguments = (path, args, options, expectedPath) => {
    assert.equal(path, expectedPath)
    assert.deepEqual(args, ['--updated'])
    assert.equal(options.shell, false)
    assert.equal(options.detached, true)
    assert.equal(options.stdio, 'ignore')
    assert.equal(options.windowsHide, false)
    assert.equal(options.cwd, win32.dirname(expectedPath))
  }
  let runnerExit
  const runnerEvents = []
  await launchWindowsInstaller(process.execPath, { spawn: (path, args, options) => {
    assertLaunchArguments(path, args, options, process.execPath)
    // Hide only the test console; the production NSIS wizard remains visible.
    // Drop inherited preloads so this child can execute no user script.
    const runner = spawn(path, args, { ...options, windowsHide: true, env: childEnv })
    runner.once('spawn', () => runnerEvents.push('spawn'))
    runnerExit = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        runner.kill()
        reject(new Error('The Node --updated test child did not exit.'))
      }, 10_000)
      runner.once('error', (error) => {
        clearTimeout(timer)
        reject(error)
      })
      runner.once('exit', (code, signal) => {
        clearTimeout(timer)
        runnerEvents.push('exit')
        resolve({ code, signal })
      })
    })
    return runner
  } })
  assert.equal(runnerEvents[0], 'spawn', 'handoff requires actual Windows process creation')
  const outcome = await runnerExit
  assert.equal(typeof outcome.code, 'number')
  assert.notEqual(outcome.code, 0, 'Node must reject the unsupported --updated CLI argument')
  assert.equal(outcome.signal, null)
  assert.deepEqual(runnerEvents, ['spawn', 'exit'])

  // A missing executable fails asynchronously after spawn() returns its
  // ChildProcess wrapper. It must never be accepted as an installer handoff.
  const missingPath = win32.join(win32.dirname(process.execPath),
    `OpenSquilla-Update-Test-Missing-${randomUUID()}.exe`)
  await assert.rejects(access(missingPath), (error) => error.code === 'ENOENT')
  const missingEvents = []
  await assert.rejects(launchWindowsInstaller(missingPath, { spawn: (path, args, options) => {
    assertLaunchArguments(path, args, options, missingPath)
    const missing = spawn(path, args, { ...options, windowsHide: true, env: childEnv })
    missingEvents.push('returned')
    missing.once('spawn', () => missingEvents.push('spawn'))
    missing.once('error', (error) => missingEvents.push(error.code))
    return missing
  } }), (error) => error.code === 'install_failed')
  assert.deepEqual(missingEvents, ['returned', 'ENOENT'], 'an OS spawn error must not cross the commitment boundary')
  console.log(`Windows real process boundary passed (Node --updated exited ${outcome.code}; missing executable rejected before spawn).`)
}
console.log('Windows installer handoff checks passed (process creation only; no installer executed or installation success asserted).')
