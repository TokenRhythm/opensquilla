import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { constants, createReadStream } from 'node:fs'
import { copyFile, lstat, mkdir, open, readFile, readdir, realpath, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { promisify } from 'node:util'
import ts from 'typescript'
import {
  WINDOWS_INSTALLER_SIGNATURE_SCRIPT,
  WINDOWS_UPDATE_SIGNING_POLICY,
  runWindowsPowerShellJson,
  verifyWindowsInstaller,
  windowsPowerShellPath,
} from '../dist/windows-update-security.js'
import {
  WINDOWS_UPDATE_CACHE_DESCRIPTOR,
  createWindowsUpdateCacheDescriptor,
  loadWindowsUpdateCache,
  saveWindowsUpdateCache,
  verifyCachedInstaller,
} from '../dist/windows-update-cache.js'

// Opt-in native verification only. This script never executes an installer,
// changes a registry key/profile, signs a file, or substitutes a signature hook.
// Usage: node scripts/test-windows-update-authenticode-local.mjs
//   --installer <canonical-signed-installer.exe> --evidence-dir <new-temp-directory>
const options = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index]
  assert.ok(['--installer', '--evidence-dir'].includes(key) && !options.has(key), `Unknown/duplicate option: ${key}`)
  assert.ok(process.argv[index + 1], `Missing value for ${key}`)
  options.set(key, process.argv[index + 1])
}
assert.equal(process.platform, 'win32', 'This opt-in audit requires native Windows.')
for (const key of ['--installer', '--evidence-dir']) {
  assert.ok(options.has(key) && isAbsolute(options.get(key)), `${key} must be an explicit absolute path.`)
}
const installer = await realpath(options.get('--installer'))
const evidenceDir = resolve(options.get('--evidence-dir'))
const temporaryRoot = await realpath(tmpdir())
const evidenceParent = await realpath(dirname(evidenceDir))
const parentRelative = relative(temporaryRoot, evidenceParent)
assert.ok(parentRelative === '' || (!parentRelative.startsWith(`..${sep}`) && parentRelative !== '..' && !isAbsolute(parentRelative)),
  'Evidence must be in an existing directory inside the OS temporary directory.')
assert.notEqual(evidenceDir.toLowerCase(), temporaryRoot.toLowerCase())
const versionMatch = /^OpenSquilla-((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))-win-x64\.exe$/.exec(basename(installer))
assert.ok(versionMatch && versionMatch[1] !== '0.0.0', 'Use a canonical stable signed installer newer than 0.0.0.')
const fixtureVersion = versionMatch[1]
const root = resolve(fileURLToPath(new URL('../../..', import.meta.url)))
const electronRoot = resolve(fileURLToPath(new URL('..', import.meta.url)))
const run = promisify(execFile)
const sha256 = async (path) => {
  const digest = createHash('sha256')
  for await (const chunk of createReadStream(path)) digest.update(chunk)
  return digest.digest('hex')
}
const sameText = (value) => value.replace(/\r\n/g, '\n').trimEnd()
const modules = []
for (const name of ['windows-update-security', 'windows-update-cache', 'update-channel', 'update-feed-resolver']) {
  const sourcePath = join(electronRoot, 'src', `${name}.ts`)
  const outputPath = join(electronRoot, 'dist', `${name}.js`)
  const source = await readFile(sourcePath, 'utf8')
  const expected = ts.transpileModule(source, {
    fileName: sourcePath,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, esModuleInterop: true },
  }).outputText
  assert.equal(sameText(await readFile(outputPath, 'utf8')), sameText(expected), `${name}: run npm run build before this audit.`)
  modules.push({ name, sourceSha256: await sha256(sourcePath), compiledSha256: await sha256(outputPath) })
}
const sourceSha = (await run('git', ['rev-parse', 'HEAD'], { cwd: root, windowsHide: true })).stdout.trim()
assert.match(sourceSha, /^[0-9a-f]{40}$/)
const moduleChanges = (await run('git', ['status', '--porcelain', '--', ...modules.map(({ name }) => `desktop/electron/src/${name}.ts`)],
  { cwd: root, windowsHide: true })).stdout.trim()
assert.equal(moduleChanges, '', 'Commit or otherwise pin production module edits before this native audit.')
await mkdir(evidenceDir) // Deliberately refuse an existing evidence directory.
const sourceInfo = await stat(installer)
const sourceDigest = await sha256(installer)
const evidence = {
  schemaVersion: 1, ok: false, sourceSha, modules,
  scriptSha256: await sha256(fileURLToPath(import.meta.url)),
  startedAt: new Date().toISOString(), platform: process.platform, nodeVersion: process.version,
  installer: { path: installer, sha256: sourceDigest, bytes: sourceInfo.size, fixtureVersion },
  policy: WINDOWS_UPDATE_SIGNING_POLICY,
  simulatedCurrentVersion: '0.0.0',
  cases: [],
  boundaries: [
    'Real Windows PowerShell Authenticode and production cache modules; no signature injection or policy override.',
    'Fixture is an older signed installer; no installer, uninstaller, Electron, Gateway, signing service or registry write is run.',
    'Cache versions are explicit synthetic comparison inputs; no A-to-B install or GUI/health/profile claim is made.',
    'One disposable full installer copy is reused and reduced to a truncated specimen at completion.',
    'Fresh-process cache reuse proves this API returns the same existing file; it does not exercise a downloader.',
  ],
}
const resultPath = join(evidenceDir, 'result.json')
let disposableInstaller
const persist = async () => writeFile(resultPath, `${JSON.stringify(evidence, null, 2)}\n`, { mode: 0o600 })
async function check(name, fn) {
  const started = Date.now()
  try {
    const details = await fn()
    evidence.cases.push({ name, passed: true, elapsedMs: Date.now() - started, ...details })
    console.log(`PASS ${name}`)
  } catch (error) {
    evidence.cases.push({ name, passed: false, elapsedMs: Date.now() - started, error: error.message })
    throw error
  } finally { await persist() }
}
async function rejectNative(path, expectedStatus) {
  const native = await runWindowsPowerShellJson(WINDOWS_INSTALLER_SIGNATURE_SCRIPT, { path })
  if (expectedStatus) assert.equal(native.status, expectedStatus)
  await assert.rejects(verifyWindowsInstaller(path), (error) =>
    error.code === (native.status === 'UnknownError' ? 'signature_unavailable' : 'signature_invalid'))
  return { native, sha256: await sha256(path) }
}
async function peLayout(path) {
  const file = await open(path, 'r')
  try {
    const dos = Buffer.alloc(64)
    await file.read(dos, 0, dos.length, 0)
    assert.equal(dos.toString('ascii', 0, 2), 'MZ')
    const headerOffset = dos.readUInt32LE(60)
    assert.ok(headerOffset < 1024 * 1024)
    const header = Buffer.alloc(24)
    await file.read(header, 0, header.length, headerOffset)
    assert.equal(header.readUInt32LE(0), 0x00004550)
    const count = header.readUInt16LE(6)
    const optionalSize = header.readUInt16LE(20)
    assert.ok(count > 0 && count < 128 && optionalSize >= 160 && optionalSize < 4096)
    const optional = Buffer.alloc(optionalSize)
    await file.read(optional, 0, optional.length, headerOffset + 24)
    const magic = optional.readUInt16LE(0)
    assert.ok(magic === 0x10b || magic === 0x20b)
    const directoryOffset = magic === 0x20b ? 112 : 96
    const certificateDirectory = headerOffset + 24 + directoryOffset + 4 * 8
    const certificateOffset = optional.readUInt32LE(directoryOffset + 4 * 8)
    const certificateSize = optional.readUInt32LE(directoryOffset + 4 * 8 + 4)
    const table = Buffer.alloc(count * 40)
    const tableOffset = headerOffset + 24 + optionalSize
    await file.read(table, 0, table.length, tableOffset)
    const mutationOffset = Array.from({ length: count }, (_, index) => {
      const size = table.readUInt32LE(index * 40 + 16)
      const offset = table.readUInt32LE(index * 40 + 20)
      return size > 32 && offset >= tableOffset + table.length ? offset + 16 : null
    }).find((offset) => offset !== null)
    assert.ok(mutationOffset && certificateSize > 0 && certificateOffset > mutationOffset)
    assert.ok(certificateOffset + certificateSize <= (await file.stat()).size)
    return { certificateDirectory, certificateOffset, certificateSize, mutationOffset }
  } finally { await file.close() }
}
async function mutateSpecimen(operation) {
  const file = await open(disposableInstaller, 'r+')
  try {
    await operation(file)
    await file.sync()
  } finally { await file.close() }
}

try {
  await check('signed_installer_accept', async () => {
    await verifyWindowsInstaller(installer)
    return { native: await runWindowsPowerShellJson(WINDOWS_INSTALLER_SIGNATURE_SCRIPT, { path: installer }) }
  })
  await check('different_real_publisher_reject', async () => {
    const path = windowsPowerShellPath()
    const native = await runWindowsPowerShellJson(WINDOWS_INSTALLER_SIGNATURE_SCRIPT, { path })
    assert.equal(native.status, 'Valid', 'Wrong-publisher control must itself have a valid real signature.')
    assert.notEqual(native.thumbprint.toUpperCase(), WINDOWS_UPDATE_SIGNING_POLICY.certificateSha1)
    await assert.rejects(verifyWindowsInstaller(path), (error) => error.code === 'signature_invalid')
    return { path, native }
  })
  const cacheDir = join(evidenceDir, "缓存 空格 $() ' ` & [literal]")
  await mkdir(cacheDir)
  const cached = join(cacheDir, basename(installer))
  await copyFile(installer, cached, constants.COPYFILE_EXCL)
  disposableInstaller = cached
  const descriptor = createWindowsUpdateCacheDescriptor(
    { tag: `v${fixtureVersion}`, version: fixtureVersion, installer: basename(installer) }, sourceDigest, sourceInfo.size)
  await check('chinese_spaces_special_literal_path_accept', async () => {
    assert.equal(await sha256(cached), sourceDigest)
    await verifyWindowsInstaller(cached)
    return { path: cached, sha256: sourceDigest }
  })
  await check('cache_persist_and_verify', async () => {
    await saveWindowsUpdateCache(cacheDir, descriptor)
    assert.deepEqual(await loadWindowsUpdateCache(cacheDir), descriptor)
    const verified = await verifyCachedInstaller(cacheDir, descriptor, '0.0.0')
    assert.equal(verified?.path, cached)
    return { path: verified.path, descriptor: verified.descriptor }
  })
  await check('fresh_node_process_reuses_same_cached_file', async () => {
    const before = await lstat(cached)
    const entries = await readdir(cacheDir)
    const worker = `
      let input = ''; for await (const chunk of process.stdin) input += chunk;
      const request = JSON.parse(input);
      const cache = await import(request.module);
      const descriptor = await cache.loadWindowsUpdateCache(request.directory);
      const result = await cache.verifyCachedInstaller(request.directory, descriptor, request.currentVersion);
      if (!result) throw new Error('The persisted native cache did not restore.');
      process.stdout.write(JSON.stringify(result));
    `
    const restored = await new Promise((resolveWorker, rejectWorker) => {
      const child = execFile(process.execPath, ['--input-type=module', '--eval', worker],
        { windowsHide: true, timeout: 40_000, maxBuffer: 64 * 1024 }, (error, stdout) => {
          if (error) return rejectWorker(error)
          try { resolveWorker(JSON.parse(stdout)) } catch (parseError) { rejectWorker(parseError) }
        })
      child.stdin.on('error', () => {})
      child.stdin.end(JSON.stringify({ module: pathToFileURL(join(electronRoot, 'dist/windows-update-cache.js')).href,
        directory: cacheDir, currentVersion: '0.0.0' }))
    })
    const after = await lstat(cached)
    assert.equal(restored.path, cached)
    assert.deepEqual(restored.descriptor, descriptor)
    for (const field of ['ino', 'size', 'mtimeMs', 'ctimeMs']) assert.equal(after[field], before[field])
    assert.deepEqual(await readdir(cacheDir), entries)
    return { path: restored.path, sameFileIdentity: true, noAdditionalCacheFiles: true }
  })
  await check('cache_same_version_reject', async () => {
    assert.equal(await verifyCachedInstaller(cacheDir, descriptor, fixtureVersion), null)
  })
  await check('cache_older_candidate_reject', async () => {
    const newer = `${Number(fixtureVersion.split('.')[0]) + 1}.0.0`
    assert.equal(await verifyCachedInstaller(cacheDir, descriptor, newer), null)
    return { currentVersion: newer }
  })
  await check('corrupt_persisted_descriptor_reject', async () => {
    await writeFile(join(cacheDir, WINDOWS_UPDATE_CACHE_DESCRIPTOR), '{invalid-json')
    assert.equal(await loadWindowsUpdateCache(cacheDir), null)
    await saveWindowsUpdateCache(cacheDir, descriptor)
  })
  const layout = await peLayout(cached)
  const original = Buffer.alloc(1)
  await mutateSpecimen(async (writable) => {
    await writable.read(original, 0, 1, layout.mutationOffset)
    await writable.write(Buffer.from([original[0] ^ 1]), 0, 1, layout.mutationOffset)
  })
  await check('signed_pe_section_tamper_reject', async () => ({
    mutationOffset: layout.mutationOffset, ...await rejectNative(cached, 'HashMismatch'),
  }))
  await check('corrupt_cached_installer_reject', async () => {
    assert.equal(await verifyCachedInstaller(cacheDir, await loadWindowsUpdateCache(cacheDir), '0.0.0'), null)
  })
  await mutateSpecimen((writable) => writable.write(original, 0, 1, layout.mutationOffset))
  assert.equal(await sha256(cached), sourceDigest, 'Restore the single mutated byte before creating the unsigned specimen.')
  await mutateSpecimen(async (writable) => {
    await writable.write(Buffer.alloc(8), 0, 8, layout.certificateDirectory)
    await writable.truncate(layout.certificateOffset)
  })
  await check('unsigned_pe_without_certificate_table_reject', async () => ({
    removedCertificateTable: layout, ...await rejectNative(cached, 'NotSigned'),
  }))
  await mutateSpecimen((writable) => writable.truncate(128))
  await check('truncated_pe_reject', async () => rejectNative(cached))
  evidence.ok = true
} catch (error) {
  evidence.error = { name: error.name, message: error.message }
  process.exitCode = 1
} finally {
  if (disposableInstaller) {
    const specimenPath = await realpath(disposableInstaller)
    assert.ok(specimenPath.startsWith(`${evidenceDir}${sep}`), 'Cleanup must remain in this newly created evidence directory.')
    assert.equal((await lstat(disposableInstaller)).isSymbolicLink(), false)
    evidence.disposableSpecimen = { path: specimenPath, sha256BeforeReduction: await sha256(specimenPath) }
    await mutateSpecimen((writable) => writable.truncate(128))
    evidence.disposableSpecimen.retainedBytes = (await stat(specimenPath)).size
  }
  evidence.originalFixtureUnchanged = await sha256(installer) === sourceDigest
  evidence.ok &&= evidence.originalFixtureUnchanged
  if (!evidence.ok) process.exitCode = 1
  evidence.finishedAt = new Date().toISOString()
  evidence.passed = evidence.cases.filter((item) => item.passed).length
  evidence.failed = evidence.cases.filter((item) => !item.passed).length
  await persist()
  console.log(JSON.stringify({ ok: evidence.ok, passed: evidence.passed, failed: evidence.failed, resultPath }))
}
