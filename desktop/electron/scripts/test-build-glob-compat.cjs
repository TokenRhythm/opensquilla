const assert = require('node:assert/strict')
const { spawnSync } = require('node:child_process')
const { createHash } = require('node:crypto')
const fs = require('node:fs')
const { statSync } = fs
const { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } = require('node:fs/promises')
const { createRequire } = require('node:module')
const { tmpdir } = require('node:os')
const { basename, dirname, join, resolve, win32 } = require('node:path')
const { before, test } = require('node:test')
const { createContext, runInContext } = require('node:vm')

const { PATCHES, prepareInstalledGlobs } = require('./prepare-build-glob-compat.cjs')
const desktopRoot = resolve(__dirname, '..')
const hash = (value) => createHash('sha256').update(value).digest('hex')

before(async () => {
  await prepareInstalledGlobs(desktopRoot)
})

async function fixture(t) {
  const parent = await realpath(tmpdir())
  const root = await mkdtemp(join(parent, 'opensquilla-build-glob-test-'))
  const originalRoot = await realpath(root)
  t.after(async () => {
    const actual = await realpath(root)
    assert.equal(actual, originalRoot)
    assert.equal(dirname(actual), parent)
    assert.ok(basename(actual).startsWith('opensquilla-build-glob-test-'))
    await rm(actual, { recursive: true, force: true })
  })
  return root
}

async function put(root, name, content = name) {
  const target = join(root, name)
  await mkdir(dirname(target), { recursive: true })
  await writeFile(target, content)
  return target
}

async function dependencyFixture(t) {
  const root = await fixture(t)
  for (const patch of PATCHES) {
    const installed = join(desktopRoot, 'node_modules', patch.packageName)
    const target = join(root, 'node_modules', patch.packageName)
    await put(target, 'package.json', await readFile(join(installed, 'package.json')))
    let source = await readFile(join(installed, patch.file))
    if (hash(source) === patch.patchedSha256) {
      let original = source.toString('utf8')
      for (const [before, after] of [...patch.replacements].reverse()) {
        assert.equal(original.split(after).length, 2)
        original = original.replace(after, before)
      }
      source = Buffer.from(original)
    }
    assert.equal(hash(source), patch.originalSha256)
    await put(target, patch.file, source)
  }
  return root
}

async function patchedFiles(root) {
  return Promise.all(PATCHES.map((patch) => readFile(join(root, 'node_modules', patch.packageName, patch.file))))
}

test('reviewed dependency files patch once and remain byte-identical on subsequent preparation', async (t) => {
  const root = await dependencyFixture(t)
  assert.equal((await prepareInstalledGlobs(root)).changed, PATCHES.length)
  const first = await patchedFiles(root)
  assert.deepEqual(first.map(hash), PATCHES.map((patch) => patch.patchedSha256))
  assert.equal((await prepareInstalledGlobs(root)).changed, 0)
  assert.deepEqual(await patchedFiles(root), first)
})

test('an unknown final-file hash rejects the entire preparation before any write', async (t) => {
  const root = await dependencyFixture(t)
  const patch = PATCHES.at(-1)
  const target = join(root, 'node_modules', patch.packageName, patch.file)
  await writeFile(target, Buffer.concat([await readFile(target), Buffer.from('\n// synthetic local edit\n')]))
  const before = await patchedFiles(root)
  await assert.rejects(prepareInstalledGlobs(root), /Unexpected upstream/)
  assert.deepEqual(await patchedFiles(root), before)
})

test('an unreviewed package version rejects the entire preparation before any write', async (t) => {
  const root = await dependencyFixture(t)
  const patch = PATCHES.at(-1)
  const target = join(root, 'node_modules', patch.packageName, 'package.json')
  const manifest = JSON.parse(await readFile(target, 'utf8'))
  manifest.version = '99.0.0'
  await writeFile(target, JSON.stringify(manifest))
  const before = await patchedFiles(root)
  await assert.rejects(prepareInstalledGlobs(root), /requires .* got 99\.0\.0/)
  assert.deepEqual(await patchedFiles(root), before)
})

function asyncGlob(glob, pattern, options) {
  return new Promise((resolve, reject) => {
    glob(pattern, options, (error, matches) => error ? reject(error) : resolve(matches))
  })
}

// Execute the installed callers with Windows path semantics on every host.
// Only filesystem paths are translated to a synthetic fixture; matching and
// traversal still run the real dependency code and the real minimatch engine.
function windowsDependencies(root) {
  const cwd = 'C:\\fixture'
  const toLocalPath = (path) => {
    const relative = win32.relative(cwd, win32.resolve(cwd, path))
    assert.equal(win32.isAbsolute(relative), false)
    assert.ok(relative !== '..' && !relative.startsWith('..\\'))
    return join(root, ...relative.split('\\'))
  }
  const filesystem = {}
  for (const method of ['readdirSync', 'statSync', 'lstatSync', 'readdir', 'stat', 'lstat']) {
    filesystem[method] = (path, ...args) => fs[method](toLocalPath(path), ...args)
  }
  const minimatch = require('minimatch')
  const windowsMatcher = minimatch.defaults({ platform: 'win32' })
  const matcherExports = { ...minimatch, ...windowsMatcher, minimatch: windowsMatcher }
  const globRoot = dirname(require.resolve('glob/package.json'))
  const sources = new Set([
    ...['glob.js', 'sync.js', 'common.js'].map((file) => join(globRoot, file)),
    require.resolve('filelist'),
    require.resolve('dir-compare/build/src/FilterHandler/defaultFilterHandler'),
  ])
  const cache = new Map()
  const context = createContext({ Buffer, console })
  const windowsProcess = { platform: 'win32', cwd: () => cwd, nextTick: process.nextTick.bind(process) }
  const load = (filename) => {
    if (cache.has(filename)) return cache.get(filename).exports
    const module = { exports: {} }
    cache.set(filename, module)
    const nativeRequire = createRequire(filename)
    const moduleRequire = (request) => {
      if (request === 'path') return win32
      if (request === 'fs') return filesystem
      if (request === 'path-is-absolute') return win32.isAbsolute
      if (request === 'minimatch') return matcherExports
      const resolved = nativeRequire.resolve(request)
      return sources.has(resolved) ? load(resolved) : nativeRequire(request)
    }
    const evaluate = runInContext(
      `(function(require, module, exports, process) {\n${fs.readFileSync(filename, 'utf8')}\n})`,
      context, { filename },
    )
    evaluate(moduleRequire, module, module.exports, windowsProcess)
    return module.exports
  }
  return (request) => load(require.resolve(request))
}

test('legacy glob sync and callback APIs preserve file selection, escapes, raw parents, and brace identity', async (t) => {
  const root = await fixture(t)
  for (const name of ['alpha.txt', 'beta.js', 'literal[x].txt', '.hidden.txt', 'nested/inside.txt', 'nested/skip.txt']) {
    await put(root, name)
  }
  await mkdir(join(root, 'empty'))
  const glob = require('glob')
  const cases = [
    ['*.{txt,js}', {}, ['alpha.txt', 'beta.js', 'literal[x].txt']],
    ['@(alpha|beta).*', {}, ['alpha.txt', 'beta.js']],
    ['*.txt', { dot: true }, ['.hidden.txt', 'alpha.txt', 'literal[x].txt']],
    ['**/*.txt', { ignore: ['nested/skip.txt'] }, ['alpha.txt', 'literal[x].txt', 'nested/inside.txt']],
    ['**/*.txt', { ignore: ['nested/**'] }, ['alpha.txt', 'literal[x].txt']],
    ['literal[[]x].txt', {}, ['literal[x].txt']],
    ['nested/../*.txt', {}, ['nested/../alpha.txt', 'nested/../literal[x].txt']],
    ['empty/*.{js,ts}', { nonull: true }, ['empty/*.js', 'empty/*.ts']],
    ['{alpha.txt,*.js}', { nounique: true }, ['alpha.txt', 'beta.js']],
    ['{alpha.txt,alpha.txt}', { nounique: true }, ['alpha.txt', 'alpha.txt']],
    ['{alpha.txt,alpha.txt}', {}, ['alpha.txt']],
  ]
  // Legacy minimatch3 treated Windows backslashes as separators even when
  // callers passed allowWindowsEscape, so escaping is a POSIX-only contract.
  if (process.platform !== 'win32') {
    cases.push(['literal\\[x\\].txt', {}, ['literal[x].txt']])
  }
  for (const [pattern, options, expected] of cases) {
    const settings = { cwd: root, ...options }
    assert.deepEqual(glob.sync(pattern, settings).sort(), expected, `sync ${pattern}`)
    assert.deepEqual((await asyncGlob(glob, pattern, settings)).sort(), expected, `async ${pattern}`)
  }
  if (process.platform !== 'win32') assert.equal(glob.hasMagic('literal\\[x\\].txt'), false)
  assert.equal(glob.hasMagic('*.txt'), true)
})

test('Windows glob callers traverse native and mixed separator patterns', async (t) => {
  const root = await fixture(t)
  for (const name of ['source/main.js', 'source/.hidden.txt', 'source/nested/keep.txt']) await put(root, name)
  const glob = windowsDependencies(root)('glob')
  const cases = [
    ['C:\\fixture\\source\\*.js', ['C:/fixture/source/main.js']],
    ['C:\\fixture\\source/**/*', [
      'C:/fixture/source/.hidden.txt',
      'C:/fixture/source/main.js',
      'C:/fixture/source/nested/keep.txt',
    ]],
  ]
  for (const [pattern, expected] of cases) {
    const options = { cwd: 'C:\\fixture', dot: true, nodir: true }
    assert.deepEqual(Array.from(glob.sync(pattern, options)).sort(), expected, `sync ${pattern}`)
    assert.deepEqual(Array.from(await asyncGlob(glob, pattern, options)).sort(), expected, `async ${pattern}`)
  }
})

test('Windows FileList preserves native include and exclude patterns after path normalization', async (t) => {
  const root = await fixture(t)
  for (const name of ['alpha.txt', 'ignored.txt', 'beta.js']) await put(root, `source/${name}`)
  const { FileList } = windowsDependencies(root)('filelist')
  const files = new FileList()
  files.include('C:\\fixture\\source\\*.txt')
  files.exclude('C:\\fixture\\source\\ignore*.txt')
  assert.deepEqual(Array.from(files.toArray()), ['C:/fixture/source/alpha.txt'])
})

test('Windows directory comparison filters match native relative paths', async (t) => {
  const root = await fixture(t)
  const { defaultFilterHandler } = windowsDependencies(root)('dir-compare/build/src/FilterHandler/defaultFilterHandler')
  const options = { includeFilter: 'nested\\*.txt', excludeFilter: 'nested\\ignore*.txt' }
  const entry = (name) => ({ name, stat: { isFile: () => true } })
  assert.equal(defaultFilterHandler(entry('keep.txt'), 'nested', options), true)
  assert.equal(defaultFilterHandler(entry('ignore.txt'), 'nested', options), false)
  assert.equal(defaultFilterHandler(entry('other.js'), 'nested', options), false)
})

test('legacy glob follows an explicitly selected directory link inside the fixture', async (t) => {
  const root = await fixture(t)
  await put(root, 'nested/inside.txt')
  await symlink(join(root, 'nested'), join(root, 'linked'), process.platform === 'win32' ? 'junction' : 'dir')
  const glob = require('glob')
  assert.deepEqual(glob.sync('linked/**/*.txt', { cwd: root }), ['linked/inside.txt'])
  assert.deepEqual(await asyncGlob(glob, 'linked/**/*.txt', { cwd: root }), ['linked/inside.txt'])
})

test('asar creates readable archives and honors file and directory unpack patterns', async (t) => {
  const root = await fixture(t)
  const source = join(root, 'source')
  await put(source, 'main.js', 'module.exports = "synthetic"\n')
  await put(source, '.hidden.txt', 'hidden fixture')
  await put(source, 'native/addon.node', 'synthetic native placeholder')
  await put(source, 'assets/logo.txt', 'synthetic asset')
  const asar = require('@electron/asar')
  const archive = join(root, 'app.asar')
  await asar.createPackageWithOptions(source, archive, { unpack: '*.node', unpackDir: 'asset?' })
  assert.equal(asar.extractFile(archive, 'main.js').toString(), 'module.exports = "synthetic"\n')
  assert.equal(asar.extractFile(archive, '.hidden.txt').toString(), 'hidden fixture')
  assert.equal(asar.statFile(archive, 'native/addon.node').unpacked, true)
  assert.equal(asar.statFile(archive, 'assets/logo.txt').unpacked, true)
  assert.equal((await readFile(`${archive}.unpacked/native/addon.node`, 'utf8')), 'synthetic native placeholder')
  assert.equal((await readFile(`${archive}.unpacked/assets/logo.txt`, 'utf8')), 'synthetic asset')
  asar.uncache(archive)
})

test('directory comparison preserves both include and exclude filters', async (t) => {
  const root = await fixture(t)
  for (const side of ['left', 'right']) {
    await put(root, `${side}/keep.txt`, 'identical')
    await put(root, `${side}/ignore.txt`, side)
    await put(root, `${side}/other.bin`, side)
  }
  const comparison = require('dir-compare').compareSync(join(root, 'left'), join(root, 'right'), {
    compareContent: true, includeFilter: '*.txt', excludeFilter: 'ignore*',
  })
  assert.equal(comparison.same, true)
  assert.ok(comparison.diffSet.some((entry) => entry.name1 === 'keep.txt' && entry.state === 'equal'))
})

test('filelist include and exclude patterns still use the named list matching API', async (t) => {
  const root = await fixture(t)
  for (const name of ['alpha.txt', 'ignored.txt', 'beta.js']) await put(root, name)
  const { FileList } = require('filelist')
  const files = new FileList()
  files.include(join(root, '*.txt').replaceAll('\\', '/'))
  files.exclude(join(root, 'ignored.txt').replaceAll('\\', '/'))
  assert.deepEqual(files.toArray().map((name) => basename(name)), ['alpha.txt'])
})

test('electron-builder file matchers preserve traversal, inclusion, and exclusion', async (t) => {
  const root = await fixture(t)
  for (const name of ['nested/keep.txt', 'nested/ignore.txt', 'nested/other.bin']) await put(root, name)
  const { FileMatcher } = require('app-builder-lib/out/fileMatcher')
  const matcher = new FileMatcher(root, root, (pattern) => pattern, ['**/*.txt', '!**/ignore.txt'])
  const filter = matcher.createFilter()
  const selected = (name) => {
    const path = join(root, name)
    return filter(path, statSync(path))
  }
  assert.equal(selected('nested'), true)
  assert.equal(selected('nested/keep.txt'), true)
  assert.equal(selected('nested/ignore.txt'), false)
  assert.equal(selected('nested/other.bin'), false)
})

test('modern named callers retain drive, UNC, case, and matchBase behavior', () => {
  const { Minimatch, minimatch } = require('minimatch')
  assert.equal(new Minimatch('C:/project/**/*.js', { platform: 'win32', nocase: true }).match('c:/project/lib/entry.js'), true)
  assert.equal(new Minimatch('//server/share/**/*.txt', { platform: 'win32', nocase: true }).match('//SERVER/SHARE/nested/file.txt'), true)
  assert.equal(new Minimatch('C:/project/**/*.js', { platform: 'win32' }).match('C:/project/lib/entry.txt'), false)
  assert.equal(minimatch('Contents/Resources/native/addon.node', '*.node', { matchBase: true }), true)
  assert.equal(minimatch('Contents/Resources/native/addon.txt', '*.node', { matchBase: true }), false)
})

test('glob preprocessing amplification and brace expansion stay bounded in a child process', () => {
  const source = `
    const assert = require('node:assert/strict')
    const { Minimatch } = require('minimatch')
    const amplified = new Minimatch('a/' + '**/../b/'.repeat(20) + '**', {
      optimizationLevel: 2, braceExpandMax: 256,
    })
    assert.ok(amplified.globParts.length <= 256)
    const expanded = new Minimatch('{a,b}/'.repeat(20) + '*.js', {
      optimizationLevel: 2, braceExpandMax: 256,
    })
    assert.ok(expanded.globParts.length <= 256)
    const ordinary = new Minimatch('src/**/entry.{js,ts}', { optimizationLevel: 2 })
    assert.equal(ordinary.match('src/lib/entry.js'), true)
    assert.equal(ordinary.match('src/lib/entry.py'), false)
    process.stdout.write('bounded and matched')
  `
  const result = spawnSync(process.execPath, ['-e', source], {
    cwd: desktopRoot, encoding: 'utf8', timeout: 10000, maxBuffer: 1024 * 1024, windowsHide: true,
  })
  assert.ifError(result.error)
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout, 'bounded and matched')
})
