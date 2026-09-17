// Electron 42 downloads lazily on first require(). Prepare it before E2E timers
// start; install.js uses electron_config_cache (not ELECTRON_CACHE).
import { readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { setTimeout as sleep } from 'node:timers/promises'

function readJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'))
}

export function verifyElectron(root, { run = spawnSync, platform = process.platform, arch = process.arch } = {}) {
  const electronRoot = join(root, 'node_modules', 'electron')
  const version = readJson(join(root, 'package-lock.json')).packages['node_modules/electron'].version
  if (readJson(join(electronRoot, 'package.json')).version !== version) {
    throw new Error('Installed Electron package differs from the lockfile')
  }
  const expectedPath = {
    darwin: 'Electron.app/Contents/MacOS/Electron',
    win32: 'electron.exe',
    linux: 'electron',
  }[platform]
  const binaryPath = readFileSync(join(electronRoot, 'path.txt'), 'utf8').trim()
  if (!expectedPath || binaryPath !== expectedPath) {
    throw new Error(`Unexpected Electron binary path for ${platform}: ${binaryPath}`)
  }
  const binary = join(electronRoot, 'dist', expectedPath)
  if (!statSync(binary).isFile()) throw new Error('Electron executable is missing')
  if (readFileSync(join(electronRoot, 'dist', 'version'), 'utf8').trim().replace(/^v/, '') !== version) {
    throw new Error('Installed Electron binary version differs from the lockfile')
  }
  // Execute the already resolved file: requiring electron here could download
  // another binary and accidentally turn this verification into a repair.
  const probe = run(binary, ['-p', 'JSON.stringify({version:process.versions.electron,platform:process.platform,arch:process.arch})'], {
    encoding: 'utf8',
    timeout: 15000,
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
  })
  if (probe.error || probe.status !== 0) {
    throw new Error(`Electron binary probe failed: ${probe.error?.message || probe.stderr || probe.status}`)
  }
  const actual = JSON.parse(probe.stdout.trim())
  if (actual.version !== version || actual.platform !== platform || actual.arch !== arch) {
    throw new Error(`Electron binary identity mismatch: ${JSON.stringify(actual)}`)
  }
  return actual
}

export async function prepareElectron(root, { run = spawnSync, wait = sleep, write = (text) => process.stdout.write(text) } = {}) {
  const electronRoot = join(root, 'node_modules', 'electron')
  const version = readJson(join(root, 'package-lock.json')).packages['node_modules/electron'].version
  if (readJson(join(electronRoot, 'package.json')).version !== version) {
    throw new Error('Installed Electron package differs from the lockfile')
  }
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    const result = run(process.execPath, [join(electronRoot, 'install.js')], {
      cwd: root,
      encoding: 'utf8',
      timeout: 180000,
      env: process.env,
    })
    const output = `${result.stdout || ''}${result.stderr || ''}`
    write(output)
    if (!result.error && result.status === 0) {
      const identity = verifyElectron(root, { run })
      write(`Prepared Electron ${identity.version} (${identity.platform}/${identity.arch})\n`)
      return identity
    }
    // Only the observed download-server failure is eligible. Permission,
    // checksum, extraction and timeout failures keep their original hard error.
    const transientDownload = /HTTPError: Response code (?:500|502|503|504)\b/.test(output)
      && !/checksum|integrity|EACCES|EPERM/i.test(output)
    if (attempt === 2 || result.error || !transientDownload) {
      throw new Error(`Electron preparation failed on attempt ${attempt}: ${result.error?.message || result.status}`)
    }
    write('::warning::Electron binary download returned a transient server error; retrying once.\n')
    await wait(2000)
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await prepareElectron(resolve(process.argv[2] || 'desktop/electron'))
  } catch (error) {
    console.error(error)
    process.exitCode = 1
  }
}
