// Patch the locked include, not installer.nsi: electron-builder must retain its
// normal build/sign/embed sequence for the uninstaller and final installer.
const { createHash } = require('node:crypto')
const { readFile, writeFile } = require('node:fs/promises')
const { dirname, join } = require('node:path')

const VERSION = '26.15.3'
const ORIGINAL_SHA256 = '97bd546b5cd2aaf16b77bc9e2be8a18962dd74ab5c4d23b35b163ca89bf4dd2a'
const sha256 = (value) => createHash('sha256').update(value).digest('hex')
const HEADER = '; OpenSquilla #1441: verified legacy-uninstaller compatibility patch\n'

function replaceOnce(source, before, after) {
  if (source.split(before).length !== 2) {
    throw new Error(`The locked NSIS template no longer has exactly one patch point: ${before}`)
  }
  return source.replace(before, after)
}

function patchedTemplate(original, boundary) {
  if (sha256(original) !== ORIGINAL_SHA256) {
    throw new Error('Unexpected upstream NSIS installUtil.nsh; review the compatibility patch before upgrading electron-builder.')
  }
  let source = original.toString('utf8').replace(/\r\n/g, '\n')
  source = replaceOnce(source,
    `    ExecWait '"$uninstallerFileNameTemp" /S /KEEP_APP_DATA $0 _?=$installationDir' $R0`,
    `    !insertmacro OpenSquillaLegacyRetry\n    !insertmacro OpenSquillaExecLegacyUninstaller "$uninstallerFileNameTemp"`)
  source = replaceOnce(source,
    `      ExecWait '"$uninstallerFileName" /S /KEEP_APP_DATA $0 _?=$installationDir' $R0`,
    `      !insertmacro OpenSquillaExecLegacyUninstaller "$uninstallerFileName"`)
  source = replaceOnce(source,
    '    MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0"',
    '    MessageBox MB_OK|MB_ICONEXCLAMATION "$(uninstallFailed): $R0" /SD IDOK')
  return Buffer.from(`${HEADER}${boundary.replace(/\r\n/g, '\n')}\n${source}`)
}

async function prepareTemplate(libraryRoot) {
  const manifest = JSON.parse(await readFile(join(libraryRoot, 'package.json'), 'utf8'))
  if (manifest.version !== VERSION) {
    throw new Error(`NSIS compatibility patch requires app-builder-lib ${VERSION}, got ${manifest.version}.`)
  }
  const target = join(libraryRoot, 'templates', 'nsis', 'include', 'installUtil.nsh')
  const originalPath = `${target}.opensquilla-1441-original`
  const current = await readFile(target)
  const boundary = await readFile(join(__dirname, 'legacy-uninstaller-temp.nsh'), 'utf8')
  if (sha256(current) === ORIGINAL_SHA256) {
    const patched = patchedTemplate(current, boundary)
    // Keep a verified original only for exact idempotence checks, never to
    // silently overwrite a changed dependency or a stale implementation.
    await writeFile(originalPath, current, { flag: 'wx' }).catch(async (error) => {
      if (error.code !== 'EEXIST' || sha256(await readFile(originalPath)) !== ORIGINAL_SHA256) throw error
    })
    await writeFile(target, patched)
    return { changed: true, sha256: sha256(patched) }
  }
  const original = await readFile(originalPath).catch(() => null)
  if (!original || !current.equals(patchedTemplate(original, boundary))) {
    throw new Error('NSIS template differs from both the locked upstream and this exact patch. Reinstall locked dependencies; do not package an unreviewed template.')
  }
  return { changed: false, sha256: sha256(current) }
}

let preparation
async function beforePack(context) {
  if (context.electronPlatformName !== 'win32') return
  // Parallel architecture builds share this dependency tree and this promise.
  preparation ??= prepareTemplate(dirname(require.resolve('app-builder-lib/package.json')))
  const result = await preparation
  console.log(`Verified NSIS legacy upgrade boundary (${result.sha256}).`)
}

module.exports = beforePack
module.exports.prepareTemplate = prepareTemplate
module.exports.patchedTemplate = patchedTemplate
module.exports.ORIGINAL_SHA256 = ORIGINAL_SHA256
