const assert = require('node:assert/strict')
const { createHash } = require('node:crypto')
const { mkdir, mkdtemp, readFile, realpath, rm, writeFile } = require('node:fs/promises')
const { createRequire } = require('node:module')
const { tmpdir } = require('node:os')
const { basename, dirname, join } = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const patch = require('./prepare-macos-keychain.cjs')
const libraryRoot = dirname(require.resolve('app-builder-lib/package.json'))
const targetRelative = ['out', 'codeSign', 'macCodeSign.js']
const hash = (value) => createHash('sha256').update(value).digest('hex')

async function upstream() {
  const source = await readFile(join(libraryRoot, ...targetRelative))
  assert.equal(hash(source), patch.ORIGINAL_SHA256, 'Tests require clean locked dependencies')
  return source
}

async function fixture(t) {
  const parent = await realpath(tmpdir())
  const root = await mkdtemp(join(parent, 'opensquilla-keychain-test-'))
  const createdRoot = await realpath(root)
  t.after(async () => {
    const actual = await realpath(root)
    assert.equal(actual, createdRoot)
    assert.equal(dirname(actual), parent)
    assert.ok(basename(actual).startsWith('opensquilla-keychain-test-'))
    await rm(actual, { recursive: true, force: true })
  })
  const original = await upstream()
  const target = join(root, ...targetRelative)
  await mkdir(dirname(target), { recursive: true })
  await writeFile(join(root, 'package.json'), JSON.stringify({ version: '26.15.3' }))
  await writeFile(target, original)
  return { root, target, original }
}

// Execute the actual dependency code with every security invocation intercepted.
// No real certificate, keychain, credential, or external process is used.
function signingHarness(source) {
  const calls = []
  let keychainPassword
  const dependencyRequire = createRequire(join(libraryRoot, ...targetRelative))
  const exports = {}
  const mockRequire = (name) => {
    if (name === 'builder-util') return {
      ...dependencyRequire(name),
      exec: async (file, args) => {
        assert.equal(file, '/usr/bin/security')
        const values = Array.from(args)
        calls.push(values)
        if (values[0] === 'create-keychain') keychainPassword = values[values.indexOf('-p') + 1]
        if (values[0] === 'set-key-partition-list'
            && values[values.indexOf('-k') + 1] !== keychainPassword) {
          throw new Error('Synthetic keychain rejects certificate password')
        }
        return ''
      },
    }
    if (name === './codesign') return { importCertificate: async (link) => link }
    return dependencyRequire(name)
  }
  const load = vm.runInNewContext(`(function(require, exports, __dirname, process) {${source}\n})`)
  load(mockRequire, exports, dirname(join(libraryRoot, ...targetRelative)), {
    platform: 'darwin', env: { TRAVIS: 'true' },
  })
  return { createKeychain: exports.createKeychain, calls, password: () => keychainPassword }
}

const options = {
  tmpDir: {}, currentDir: '/synthetic/project', cscLink: '/synthetic/app.p12',
  cscKeyPassword: 'synthetic-certificate-password',
}

test('the unpatched dependency reproduces the macOS keychain password failure', async () => {
  const harness = signingHarness(await upstream())
  await assert.rejects(harness.createKeychain(options), /keychain rejects certificate password/)
})

for (const installer of [false, true]) {
  test(`the upstream backport uses the keychain password for every ACL (installer=${installer})`, async () => {
    const harness = signingHarness(patch.patchedSource(await upstream()))
    await harness.createKeychain({ ...options, ...(installer ? {
      cscILink: '/synthetic/installer.p12', cscIKeyPassword: 'synthetic-installer-password',
    } : {}) })
    const imports = harness.calls.filter((args) => args[0] === 'import')
    const partitions = harness.calls.filter((args) => args[0] === 'set-key-partition-list')
    assert.deepEqual(imports.map((args) => args[args.indexOf('-P') + 1]), installer
      ? [options.cscKeyPassword, 'synthetic-installer-password'] : [options.cscKeyPassword])
    assert.equal(partitions.length, installer ? 2 : 1)
    assert.notEqual(harness.password(), options.cscKeyPassword)
    for (const args of partitions) assert.equal(args[args.indexOf('-k') + 1], harness.password())
    const unlock = harness.calls.find((args) => args[0] === 'unlock-keychain')
    assert.equal(unlock[unlock.indexOf('-p') + 1], harness.password())
  })
}

test('preparation is exact, idempotent, and only changes the reviewed three sites', async (t) => {
  const f = await fixture(t)
  assert.equal((await patch.prepareKeychain(f.root)).changed, true)
  const patched = await readFile(f.target)
  assert.equal(hash(patched), patch.PATCHED_SHA256)
  assert.deepEqual(patched, patch.patchedSource(f.original))
  assert.equal((await patch.prepareKeychain(f.root)).changed, false)
  assert.deepEqual(await readFile(f.target), patched)
  assert.equal(f.original.toString().split('\n').length, patched.toString().split('\n').length)
  const before = f.original.toString().split('\n')
  assert.equal(patched.toString().split('\n').filter((line, index) => line !== before[index]).length, 3)
})

test('a builder upgrade is rejected without modifying its source', async (t) => {
  const f = await fixture(t)
  await writeFile(join(f.root, 'package.json'), JSON.stringify({ version: '26.15.4' }))
  await assert.rejects(patch.prepareKeychain(f.root), /requires app-builder-lib 26.15.3/)
  assert.deepEqual(await readFile(f.target), f.original)
})

for (const alreadyPatched of [false, true]) {
  test(`modified dependency bytes fail closed (alreadyPatched=${alreadyPatched})`, async (t) => {
    const f = await fixture(t)
    if (alreadyPatched) await patch.prepareKeychain(f.root)
    const changed = Buffer.concat([await readFile(f.target), Buffer.from('\n// unexpected\n')])
    await writeFile(f.target, changed)
    await assert.rejects(patch.prepareKeychain(f.root), /Unexpected upstream/)
    assert.deepEqual(await readFile(f.target), changed)
  })
}
