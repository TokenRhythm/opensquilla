// Repair native HTTPS option forwarding in the locked BSD-3-Clause global-agent.
// Review and retire this patch when upgrading its published implementation.
const { createHash } = require('node:crypto')
const { readFile, writeFile } = require('node:fs/promises')
const { createRequire } = require('node:module')
const { dirname, join } = require('node:path')

const VERSION = '3.0.0'
const ORIGINAL_SHA256 = '661807c170d5a796bd81e8632a6a6c85f2ab2a73d80211a882e4ba6a6bc5a69a'
const PATCHED_SHA256 = '0c00d7a99dbb636ff2158f919c8e8960c618022f122204c735d698f09fcd8c8f'
const sha256 = (value) => createHash('sha256').update(value).digest('hex')
const TLS_OPTIONS = [
  'checkServerIdentity', 'minVersion', 'maxVersion', 'secureContext', 'session',
  'ALPNProtocols', 'sigalgs', 'minDHSize', 'allowPartialTrustChain', 'requestOCSP',
  'enableTrace', 'pskCallback', 'certificateCompression', 'privateKeyEngine',
  'privateKeyIdentifier',
]
const TLS_FORWARDING = `...Object.fromEntries(${JSON.stringify(TLS_OPTIONS)}
          .filter((key) => configuration[key] !== undefined)
          .map((key) => [key, configuration[key]])),
        host: connectionConfiguration.host,
        ca: configuration.ca`
const TLS_SERVERNAME = `servername: configuration.servername === undefined
          ? (require('node:net').isIP(connectionConfiguration.host) ? undefined : connectionConfiguration.host)
          : configuration.servername`

function patchedSource(original) {
  if (sha256(original) !== ORIGINAL_SHA256) {
    throw new Error('Unexpected upstream Agent.js; review the HTTPS patch before upgrading global-agent.')
  }
  let source = original.toString('utf8')
  for (const [before, after] of [
    ['ca: configuration.ca', TLS_FORWARDING],
    ['servername: configuration.servername || connectionConfiguration.host', TLS_SERVERNAME],
  ]) {
    if (source.split(before).length !== 2) throw new Error('Unexpected HTTPS patch point.')
    source = source.replace(before, after)
  }
  const patched = Buffer.from(source)
  if (sha256(patched) !== PATCHED_SHA256) throw new Error('Unexpected HTTPS patch output.')
  return patched
}

async function prepareProxyTls(libraryRoot) {
  const manifest = JSON.parse(await readFile(join(libraryRoot, 'package.json'), 'utf8'))
  if (manifest.version !== VERSION) {
    throw new Error(`HTTPS patch requires global-agent ${VERSION}, got ${manifest.version}.`)
  }
  const target = join(libraryRoot, 'dist', 'classes', 'Agent.js')
  const original = await readFile(target)
  if (sha256(original) === PATCHED_SHA256) return { changed: false, sha256: PATCHED_SHA256 }
  const patched = patchedSource(original)
  await writeFile(target, patched)
  return { changed: true, sha256: PATCHED_SHA256 }
}

async function prepareInstalledProxyTls() {
  const builderRequire = createRequire(require.resolve('app-builder-lib/package.json'))
  const getRequire = createRequire(builderRequire.resolve('@electron/get'))
  return prepareProxyTls(dirname(getRequire.resolve('global-agent/package.json')))
}

if (require.main === module) {
  prepareInstalledProxyTls().then((result) => {
    console.log(`Verified installer HTTPS option forwarding (${result.sha256}).`)
  }).catch((error) => {
    console.error(error.message)
    process.exitCode = 1
  })
}

module.exports = {
  prepareProxyTls, prepareInstalledProxyTls, patchedSource,
  ORIGINAL_SHA256, PATCHED_SHA256, TLS_FORWARDING, TLS_SERVERNAME,
}
