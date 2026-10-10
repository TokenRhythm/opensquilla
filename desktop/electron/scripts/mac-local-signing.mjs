import { spawnSync } from 'node:child_process'
import { randomBytes, X509Certificate } from 'node:crypto'
import {
  closeSync, existsSync, lstatSync, mkdirSync, openSync, readFileSync,
  readdirSync, rmSync, writeFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { basename, join, resolve } from 'node:path'
import { preserveKeychainPreferences, withSigningKeychainSearch } from './mac-keychain-preferences.mjs'

const appIdentifier = 'ai.opensquilla.desktop'
const security = '/usr/bin/security'
const codesign = '/usr/bin/codesign'
const openssl = '/usr/bin/openssl'

function run(command, args) {
  const result = spawnSync(command, args, { encoding: 'utf8', timeout: 120_000 })
  // security arguments contain the dedicated build keychain's password. Never
  // propagate child_process errors, command lines, or argument arrays to logs.
  if (result.error || result.status !== 0) {
    throw new Error(`${basename(command)} failed during local signing (exit ${result.status ?? 'unavailable'}).`)
  }
  return `${result.stdout || ''}${result.stderr || ''}`.trim()
}

function assertPrivate(path, directory = false) {
  const stat = lstatSync(path)
  if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile() || stat.nlink !== 1) ||
      stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0) {
    throw new Error(`Local signing state must be owned by this user and private: ${path}`)
  }
}

export function localMacRequirement(certificateSha1, identifier = appIdentifier) {
  if (!/^[a-f0-9]{40}$/i.test(certificateSha1) || !/^[A-Za-z0-9.-]+$/.test(identifier)) {
    throw new Error('Invalid local signing certificate or application identifier')
  }
  return `identifier "${identifier}" and certificate leaf = H"${certificateSha1.toLowerCase()}"`
}

function certificateFingerprint(certificatePath) {
  const certificate = new X509Certificate(readFileSync(certificatePath))
  if (Date.parse(certificate.validTo) <= Date.now() + 30 * 24 * 60 * 60 * 1000) {
    throw new Error('The local signing certificate expires within 30 days. Renew it explicitly; do not silently replace the application identity.')
  }
  return certificate.fingerprint.replaceAll(':', '').toUpperCase()
}

function initializeIdentity(stateDirectory, paths, onKeychainCreated) {
  if (readdirSync(stateDirectory).some(name => name !== 'build.lock')) {
    throw new Error('Local signing state is incomplete. Restore the existing identity; refusing to generate a replacement.')
  }
  const password = randomBytes(32).toString('hex')
  writeFileSync(paths.password, password, { mode: 0o600, flag: 'wx' })
  const configPath = join(stateDirectory, 'certificate.conf')
  const privateKeyPath = join(stateDirectory, 'private-key.pem')
  const archivePath = join(stateDirectory, 'identity.p12')
  writeFileSync(configPath, [
    '[req]', 'distinguished_name=subject', 'x509_extensions=usage', 'prompt=no',
    '[subject]', 'CN=OpenSquilla Local Development',
    '[usage]', 'basicConstraints=critical,CA:false', 'keyUsage=critical,digitalSignature',
    'extendedKeyUsage=critical,codeSigning', 'subjectKeyIdentifier=hash', '',
  ].join('\n'), { mode: 0o600, flag: 'wx' })
  try {
    run(openssl, ['req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '3650',
      '-config', configPath, '-keyout', privateKeyPath, '-out', paths.certificate])
    run(openssl, ['pkcs12', '-export', '-inkey', privateKeyPath, '-in', paths.certificate,
      '-out', archivePath, '-passout', `file:${paths.password}`])
    preserveKeychainPreferences(
      () => run(security, ['create-keychain', '-p', password, paths.keychain]),
      args => run(security, args),
    )
    assertPrivate(paths.keychain)
    onKeychainCreated()
    run(security, ['unlock-keychain', '-p', password, paths.keychain])
    run(security, ['import', archivePath, '-k', paths.keychain, '-f', 'pkcs12',
      '-P', password, '-x', '-T', codesign])
    const certificateSha1 = certificateFingerprint(paths.certificate)
    writeFileSync(paths.manifest, JSON.stringify({ schemaVersion: 1, certificateSha1 }) + '\n',
      { mode: 0o600, flag: 'wx' })
  } finally {
    for (const path of [configPath, privateKeyPath, archivePath]) rmSync(path, { force: true })
  }
}

/** Reuse one per-user identity across checkouts without touching the login keychain. */
export async function withLocalMacSigning(callback, {
  stateDirectory = join(homedir(), 'Library', 'Application Support', 'OpenSquilla', 'local-signing'),
} = {}) {
  if (process.platform !== 'darwin') throw new Error('Local macOS signing requires macOS')
  stateDirectory = resolve(stateDirectory)
  mkdirSync(stateDirectory, { recursive: true, mode: 0o700 })
  assertPrivate(stateDirectory, true)
  const lockPath = join(stateDirectory, 'build.lock')
  let lock
  try {
    lock = openSync(lockPath, 'wx', 0o600)
  } catch {
    throw new Error(`Local signing is locked by another build. If it exited, remove ${lockPath} after checking its recorded PID.`)
  }
  writeFileSync(lock, `${process.pid}\n`)
  const paths = {
    keychain: join(stateDirectory, 'identity.keychain-db'),
    password: join(stateDirectory, 'keychain-password'),
    certificate: join(stateDirectory, 'certificate.pem'),
    manifest: join(stateDirectory, 'identity.json'),
  }
  let keychainCanLock = false
  try {
    if (!existsSync(paths.manifest)) {
      // OpenSSL and security inherit private creation permissions.
      const previousMask = process.umask(0o077)
      try {
        initializeIdentity(stateDirectory, paths, () => { keychainCanLock = true })
      } finally { process.umask(previousMask) }
    }
    for (const path of Object.values(paths)) assertPrivate(path)
    const manifest = JSON.parse(readFileSync(paths.manifest, 'utf8'))
    const certificateSha1 = certificateFingerprint(paths.certificate)
    if (manifest.schemaVersion !== 1 || manifest.certificateSha1 !== certificateSha1) {
      throw new Error('The persisted local signing certificate changed. Restore the original identity before rebuilding.')
    }
    const password = readFileSync(paths.password, 'utf8')
    if (!/^[a-f0-9]{64}$/.test(password)) throw new Error('Invalid local build keychain password file')
    // Keep identity matching explicit and add only temporary search membership
    // required by codesign. The default keychain is never changed for signing.
    run(security, ['unlock-keychain', '-p', password, paths.keychain])
    keychainCanLock = true
    run(security, ['set-keychain-settings', '-l', '-u', '-t', '21600', paths.keychain])
    return await withSigningKeychainSearch(
      paths.keychain,
      () => callback({ certificateSha1, keychainPath: paths.keychain }),
      args => run(security, args),
    )
  } finally {
    try {
      if (keychainCanLock) {
        assertPrivate(paths.keychain)
        run(security, ['lock-keychain', paths.keychain])
      }
    } finally {
      closeSync(lock)
      rmSync(lockPath)
    }
  }
}

export function localMacSignOptions(builderOptions, identity) {
  const requirement = localMacRequirement(identity.certificateSha1)
  return {
    ...builderOptions,
    identity: identity.certificateSha1,
    keychain: identity.keychainPath,
    identityValidation: false,
    preAutoEntitlements: false,
    preEmbedProvisioningProfile: false,
    strictVerify: true,
    optionsForFile(filePath) {
      const options = builderOptions.optionsForFile?.(filePath) || {}
      return {
        ...options,
        hardenedRuntime: true,
        timestamp: 'none',
        requirements: filePath === builderOptions.app ? `=designated => ${requirement}` : options.requirements,
      }
    },
  }
}

export function verifyLocalMacSignature(appPath, identity) {
  const requirement = localMacRequirement(identity.certificateSha1)
  run(codesign, ['--verify', '--deep', '--strict', '-R', `=${requirement}`, appPath])
  const details = run(codesign, ['-d', '-r-', '--verbose=2', appPath])
  const designatedRequirement = details.match(/^#?\s*designated => (.+)$/m)?.[1]
  if (details.includes('Signature=adhoc') ||
      designatedRequirement?.toLowerCase() !== requirement.toLowerCase()) {
    throw new Error('Local package does not carry the stable certificate-pinned application identity')
  }
  return { certificateSha1: identity.certificateSha1, designatedRequirement }
}
