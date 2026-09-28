// Backport electron-builder #10172 to the locked MIT-licensed app-builder-lib.
// Remove after reviewing a released builder containing the upstream fix:
// https://github.com/electron-userland/electron-builder/pull/10172
const { createHash } = require('node:crypto')
const { readFile, writeFile } = require('node:fs/promises')
const { dirname, join } = require('node:path')

const VERSION = '26.15.3'
const ORIGINAL_SHA256 = '9f7d789b326147b6da29e1218d6e3f76b0314c68cf5d0f1efa59338a3c86e71a'
const PATCHED_SHA256 = '5b6551607c61c75cd1eadb2165b05513ff6d6b2965d129b5db9192b7e99e6fd1'
const sha256 = (value) => createHash('sha256').update(value).digest('hex')

function patchedSource(original) {
  if (sha256(original) !== ORIGINAL_SHA256) {
    throw new Error('Unexpected upstream macCodeSign.js; review the keychain backport before upgrading electron-builder.')
  }
  let source = original.toString('utf8')
  for (const [before, after] of [
    ['importCerts(keychainFile, certPaths, cscPasswords)',
      'importCerts(keychainFile, certPaths, cscPasswords, keychainPassword)'],
    ['async function importCerts(keychainFile, paths, keyPasswords)',
      'async function importCerts(keychainFile, paths, keyPasswords, keychainPassword)'],
    ['"apple-tool:,apple:", "-s", "-k", password, keychainFile',
      '"apple-tool:,apple:", "-s", "-k", keychainPassword, keychainFile'],
  ]) {
    if (source.split(before).length !== 2) throw new Error('Unexpected macOS keychain patch point.')
    source = source.replace(before, after)
  }
  const patched = Buffer.from(source)
  if (sha256(patched) !== PATCHED_SHA256) throw new Error('Unexpected macOS keychain backport output.')
  return patched
}

async function prepareKeychain(libraryRoot) {
  const manifest = JSON.parse(await readFile(join(libraryRoot, 'package.json'), 'utf8'))
  if (manifest.version !== VERSION) {
    throw new Error(`Keychain backport requires app-builder-lib ${VERSION}, got ${manifest.version}.`)
  }
  const target = join(libraryRoot, 'out', 'codeSign', 'macCodeSign.js')
  const original = await readFile(target)
  if (sha256(original) === PATCHED_SHA256) return { changed: false, sha256: PATCHED_SHA256 }
  const patched = patchedSource(original)
  await writeFile(target, patched)
  return { changed: true, sha256: PATCHED_SHA256 }
}

if (require.main === module) {
  Promise.resolve().then(async () => {
    if (process.platform !== 'darwin') throw new Error('Keychain preparation is only for macOS packaging.')
    const result = await prepareKeychain(dirname(require.resolve('app-builder-lib/package.json')))
    console.log(`Verified macOS keychain password backport (${result.sha256}).`)
  }).catch((error) => {
    console.error(error.message)
    process.exitCode = 1
  })
}

module.exports = { prepareKeychain, patchedSource, ORIGINAL_SHA256, PATCHED_SHA256 }
