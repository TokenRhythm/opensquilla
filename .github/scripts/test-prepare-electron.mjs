import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { prepareElectron, verifyElectron } from './prepare-electron.mjs'

function fixture(t, platform = process.platform) {
  const root = mkdtempSync(join(tmpdir(), 'ci-electron-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const electronRoot = join(root, 'node_modules', 'electron')
  const path = { darwin: 'Electron.app/Contents/MacOS/Electron', win32: 'electron.exe', linux: 'electron' }[platform]
  mkdirSync(join(electronRoot, 'dist', platform === 'darwin' ? 'Electron.app/Contents/MacOS' : '.'), { recursive: true })
  writeFileSync(join(root, 'package-lock.json'), JSON.stringify({ packages: { 'node_modules/electron': { version: '42.4.0' } } }))
  writeFileSync(join(electronRoot, 'package.json'), JSON.stringify({ version: '42.4.0' }))
  writeFileSync(join(electronRoot, 'path.txt'), path)
  writeFileSync(join(electronRoot, 'dist', 'version'), '42.4.0\n')
  writeFileSync(join(electronRoot, 'dist', path), '')
  return { root, electronRoot, identity: { version: '42.4.0', platform, arch: process.arch } }
}

for (const platform of ['win32', 'darwin', 'linux']) {
  test(`verify ${platform} binary without invoking a package loader`, (t) => {
    const { root, identity } = fixture(t, platform)
    const actual = verifyElectron(root, { platform, run(binary, args, options) {
      assert.ok(binary.includes('dist'))
      assert.equal(options.env.ELECTRON_RUN_AS_NODE, '1')
      assert.equal(options.timeout, 15000)
      assert.equal(args[0], '-p')
      return { status: 0, stdout: JSON.stringify(identity) }
    } })
    assert.deepEqual(actual, identity)
  })
}

test('successful preparation invokes the pinned installer once without waiting', async (t) => {
  const { root, identity } = fixture(t)
  const calls = []
  await prepareElectron(root, { run(binary, args) {
    calls.push({ binary, args })
    return { status: 0, stdout: args[0] === '-p' ? JSON.stringify(identity) : '' }
  }, wait() { assert.fail('successful preparation must not wait') }, write() {} })
  assert.equal(calls.length, 2)
  assert.equal(calls[0].binary, process.execPath)
  assert.equal(calls[0].args[0], join(root, 'node_modules', 'electron', 'install.js'))
})

for (const status of [500, 502, 503, 504]) {
  test(`download HTTP ${status} retries once and then verifies`, async (t) => {
    const { root, identity } = fixture(t)
    let installs = 0
    let waits = 0
    await prepareElectron(root, { run(binary, args) {
      if (args[0] === '-p') return { status: 0, stdout: JSON.stringify(identity) }
      installs += 1
      return installs === 1 ? { status: 1, stderr: `HTTPError: Response code ${status}` } : { status: 0 }
    }, wait(ms) { waits += 1; assert.equal(ms, 2000) }, write() {} })
    assert.equal(installs, 2)
    assert.equal(waits, 1)
  })
}

for (const stderr of ['HTTPError: Response code 403', 'checksum mismatch', 'HTTPError: Response code 500; integrity mismatch', 'extraction failed']) {
  test(`does not retry ${stderr}`, async (t) => {
    const { root } = fixture(t)
    let installs = 0
    await assert.rejects(prepareElectron(root, { run() { installs += 1; return { status: 1, stderr } }, wait() { assert.fail('must not wait') }, write() {} }))
    assert.equal(installs, 1)
  })
}

test('two download failures remain a hard failure without a binary probe', async (t) => {
  const { root } = fixture(t)
  let installs = 0
  await assert.rejects(prepareElectron(root, { run(binary, args) {
    assert.notEqual(args[0], '-p')
    installs += 1
    return { status: 1, stderr: 'HTTPError: Response code 500' }
  }, wait() {}, write() {} }), /attempt 2/)
  assert.equal(installs, 2)
})

test('identity mismatches are hard failures, never download retries', async (t) => {
  const { root, identity, electronRoot } = fixture(t)
  for (const field of ['version', 'platform', 'arch']) {
    assert.throws(() => verifyElectron(root, { run: () => ({ status: 0, stdout: JSON.stringify({ ...identity, [field]: 'wrong' }) }) }), /identity mismatch/)
  }
  writeFileSync(join(electronRoot, 'dist', 'version'), '42.3.0')
  assert.throws(() => verifyElectron(root), /version differs/)
  writeFileSync(join(electronRoot, 'path.txt'), '../outside')
  assert.throws(() => verifyElectron(root), /Unexpected Electron binary path/)
  writeFileSync(join(electronRoot, 'package.json'), JSON.stringify({ version: '42.3.0' }))
  await assert.rejects(prepareElectron(root, { run() { assert.fail('must validate package before install') } }), /package differs/)
})
