const assert = require('node:assert/strict')
const { createHash } = require('node:crypto')
const { mkdir, mkdtemp, readFile, realpath, rm, writeFile } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const { basename, dirname, join, resolve } = require('node:path')
const test = require('node:test')

const hook = require('./nsis/prepare-upgrade-template.cjs')
const libraryRoot = dirname(require.resolve('app-builder-lib/package.json'))
const templateRelative = ['templates', 'nsis', 'include', 'installUtil.nsh']
const hash = (value) => createHash('sha256').update(value).digest('hex')

async function upstream() {
  const manifest = JSON.parse(await readFile(join(libraryRoot, 'package.json'), 'utf8'))
  assert.equal(manifest.version, '26.15.3', 'Review the compatibility patch when upgrading the locked builder')
  const target = join(libraryRoot, ...templateRelative)
  let original = await readFile(target)
  if (hash(original) !== hook.ORIGINAL_SHA256) {
    // The real build hook may already have run. Its verified backup is the
    // canonical upstream input; tests never reset or modify real dependencies.
    original = await readFile(`${target}.opensquilla-1441-original`)
  }
  assert.equal(hash(original), hook.ORIGINAL_SHA256)
  return { manifest, original }
}

async function fixture(t) {
  const parent = await realpath(tmpdir())
  const root = await mkdtemp(join(parent, 'opensquilla-nsis-template-test-'))
  const createdRoot = await realpath(root)
  t.after(async () => {
    const actual = await realpath(root)
    assert.equal(actual, createdRoot, 'Refuse cleanup if the fixture root changed')
    assert.equal(dirname(actual), parent, 'Fixture cleanup must remain in its temporary parent')
    assert.ok(basename(actual).startsWith('opensquilla-nsis-template-test-'))
    await rm(actual, { recursive: true, force: true })
  })
  const { manifest, original } = await upstream()
  const target = join(root, ...templateRelative)
  await mkdir(dirname(target), { recursive: true })
  await writeFile(join(root, 'package.json'), JSON.stringify(manifest))
  await writeFile(target, original)
  return { root, target, backup: `${target}.opensquilla-1441-original`, original }
}

test('the locked upstream patches successfully and a second preparation is byte-identical', async (t) => {
  const f = await fixture(t)
  const first = await hook.prepareTemplate(f.root)
  const patched = await readFile(f.target)
  assert.equal(first.changed, true)
  assert.equal(first.sha256, hash(patched))
  assert.deepEqual(await readFile(f.backup), f.original)

  const second = await hook.prepareTemplate(f.root)
  assert.equal(second.changed, false)
  assert.equal(second.sha256, first.sha256)
  assert.deepEqual(await readFile(f.target), patched)
  assert.deepEqual(await readFile(f.backup), f.original)
})

test('the upstream body changes only the two legacy execution sites and the silent error default', async () => {
  const { original } = await upstream()
  const boundary = await readFile(join(__dirname, 'nsis', 'legacy-uninstaller-temp.nsh'), 'utf8')
  const patched = hook.patchedTemplate(original, boundary).toString('utf8')
  const marker = '!macro moveFile FROM TO\n'
  assert.equal(patched.split(marker).length, 2)
  let body = patched.slice(patched.indexOf(marker))
  const replacements = [
    [
      '    !insertmacro OpenSquillaLegacyRetry\n    !insertmacro OpenSquillaExecLegacyUninstaller "$uninstallerFileNameTemp"',
      `    ExecWait '"$uninstallerFileNameTemp" /S /KEEP_APP_DATA $0 _?=$installationDir' $R0`,
    ],
    [
      '      !insertmacro OpenSquillaExecLegacyUninstaller "$uninstallerFileName"',
      `      ExecWait '"$uninstallerFileName" /S /KEEP_APP_DATA $0 _?=$installationDir' $R0`,
    ],
    [
      '    MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0" /SD IDOK',
      '    MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0"',
    ],
  ]
  for (const [after, before] of replacements) {
    assert.equal(body.split(after).length, 2, `Expected one reviewed patch site: ${after}`)
    body = body.replace(after, before)
  }
  assert.equal(body, original.toString('utf8').replace(/\r\n/g, '\n'))
  assert.ok(patched.slice(0, patched.indexOf(marker)).includes(boundary.replace(/\r\n/g, '\n')))
})

test('an altered upstream template is rejected without changing its bytes', async (t) => {
  const f = await fixture(t)
  const changed = Buffer.concat([f.original, Buffer.from('\n; unexpected upstream change\n')])
  await writeFile(f.target, changed)
  await assert.rejects(hook.prepareTemplate(f.root), /differs from both|Unexpected upstream/)
  assert.deepEqual(await readFile(f.target), changed)
  assert.throws(() => hook.patchedTemplate(changed, ''), /Unexpected upstream/)
})

test('a builder version change is rejected before writing a template', async (t) => {
  const f = await fixture(t)
  await writeFile(join(f.root, 'package.json'), JSON.stringify({ version: '26.15.4' }))
  await assert.rejects(hook.prepareTemplate(f.root), /requires app-builder-lib 26\.15\.3, got 26\.15\.4/)
  assert.deepEqual(await readFile(f.target), f.original)
})

test('a stale compatibility boundary is rejected instead of silently replaced', async (t) => {
  const f = await fixture(t)
  await hook.prepareTemplate(f.root)
  const current = await readFile(f.target, 'utf8')
  assert.ok(current.includes('Function OpenSquillaLegacyTempPrepare'))
  const stale = current.replace('Function OpenSquillaLegacyTempPrepare', '; obsolete boundary\nFunction OpenSquillaLegacyTempPrepare')
  await writeFile(f.target, stale)
  await assert.rejects(hook.prepareTemplate(f.root), /differs from both/)
  assert.equal(await readFile(f.target, 'utf8'), stale)
  assert.deepEqual(await readFile(f.backup), f.original)
})

test('an invalid saved original cannot authorize a modified dependency', async (t) => {
  const f = await fixture(t)
  await hook.prepareTemplate(f.root)
  const patched = await readFile(f.target)
  await writeFile(f.backup, Buffer.concat([f.original, Buffer.from('\n; invalid backup\n')]))
  await assert.rejects(hook.prepareTemplate(f.root), /Unexpected upstream/)
  assert.deepEqual(await readFile(f.target), patched)
})

test('non-Windows packaging does not invoke preparation', async () => {
  const target = join(libraryRoot, ...templateRelative)
  const before = await readFile(target)
  assert.equal(await hook({ electronPlatformName: 'darwin' }), undefined)
  assert.equal(await hook({ electronPlatformName: 'linux' }), undefined)
  assert.deepEqual(await readFile(target), before)
})

test('project configuration keeps the default NSIS script and its signing pipeline', async () => {
  const projectRoot = resolve(__dirname, '..')
  const manifest = JSON.parse(await readFile(join(projectRoot, 'package.json'), 'utf8'))
  assert.equal(resolve(projectRoot, manifest.build.beforePack), resolve(__dirname, 'nsis', 'prepare-upgrade-template.cjs'))
  assert.equal(manifest.build.nsis.script, undefined, 'A custom NSIS script bypasses the builder uninstaller signing path')
  assert.equal(manifest.build.nsis.include, 'scripts/nsis/installer-progress.nsh')
  assert.equal(manifest.build.win.signExecutable, undefined, 'Unsigned settings must remain confined to the regression workflow')
  assert.equal(manifest.build.forceCodeSigning, undefined)
})
