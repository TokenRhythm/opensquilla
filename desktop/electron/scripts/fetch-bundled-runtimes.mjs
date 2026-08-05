import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  copyFile,
  mkdir,
  readdir,
  readFile,
  rename,
  rm,
  stat,
  writeFile,
} from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
export const defaultManifestPath = join(packageRoot, 'runtime', 'runtime-manifest.json')
export const defaultRuntimeRoot = join(packageRoot, 'runtime', 'developer')
export const defaultRuntimeCacheRoot = join(packageRoot, '.runtime-cache')
const allowedArchiveTypes = new Set(['zip', 'tar.gz', 'tar.xz', '7z-sfx'])
const runtimeKeys = ['python', 'node', 'gitBash']
const portableRuntimeKeys = ['python', 'node']
const maxInstalledBytes = 2 * 1024 * 1024 * 1024

function requiredText(value, field) {
  const text = String(value ?? '').trim()
  if (!text) throw new Error(`${field} must not be empty`)
  return text
}

function safeRelativePath(value, field, { allowDot = false } = {}) {
  const text = requiredText(value, field).replaceAll('\\', '/')
  const parts = text.split('/')
  if (
    text.startsWith('/') ||
    /^[A-Za-z]:/.test(text) ||
    parts.includes('..') ||
    (!allowDot && text === '.')
  ) {
    throw new Error(`${field} must be a safe relative path`)
  }
  return text
}

function validateAsset(asset, field) {
  if (!asset || typeof asset !== 'object' || Array.isArray(asset)) {
    throw new Error(`${field} must be an object`)
  }
  requiredText(asset.id, `${field}.id`)
  requiredText(asset.version, `${field}.version`)
  const url = new URL(requiredText(asset.url, `${field}.url`))
  if (!['https:', 'file:'].includes(url.protocol)) {
    throw new Error(`${field}.url must use https or file`)
  }
  if (!/^[0-9a-f]{64}$/.test(String(asset.sha256 ?? ''))) {
    throw new Error(`${field}.sha256 must be 64 lowercase hex characters`)
  }
  if (!allowedArchiveTypes.has(asset.archiveType)) {
    throw new Error(`${field}.archiveType is unsupported`)
  }
  safeRelativePath(asset.installDir, `${field}.installDir`)
  if (!Number.isSafeInteger(asset.stripComponents) || asset.stripComponents < 0) {
    throw new Error(`${field}.stripComponents must be a non-negative integer`)
  }
  if (!Array.isArray(asset.binDirs) || asset.binDirs.length === 0) {
    throw new Error(`${field}.binDirs must be a non-empty array`)
  }
  for (const entry of asset.binDirs) {
    safeRelativePath(entry, `${field}.binDirs`, { allowDot: true })
  }
  if (!asset.executables || typeof asset.executables !== 'object' || Array.isArray(asset.executables)) {
    throw new Error(`${field}.executables must be an object`)
  }
  const executableEntries = Object.entries(asset.executables)
  if (executableEntries.length === 0) throw new Error(`${field}.executables must not be empty`)
  for (const [name, path] of executableEntries) {
    requiredText(name, `${field}.executables key`)
    safeRelativePath(path, `${field}.executables.${name}`)
  }
}

export function validateRuntimeManifest(manifest) {
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new Error('runtime manifest must be an object')
  }
  if (manifest.schemaVersion !== 1) throw new Error('schemaVersion must be 1')
  requiredText(manifest.runtimeSet, 'runtimeSet')
  if (!manifest.assets || typeof manifest.assets !== 'object' || Array.isArray(manifest.assets)) {
    throw new Error('assets must be an object')
  }
  const targets = Object.entries(manifest.assets)
  if (targets.length === 0) throw new Error('assets must not be empty')
  for (const [target, assets] of targets) {
    requiredText(target, 'asset target')
    if (!assets || typeof assets !== 'object' || Array.isArray(assets)) {
      throw new Error(`assets.${target} must be an object`)
    }
    const requiredKeys = target.startsWith('windows-') ? runtimeKeys : portableRuntimeKeys
    for (const key of requiredKeys) {
      if (!(key in assets)) throw new Error(`assets.${target} is missing ${key}`)
      validateAsset(assets[key], `assets.${target}.${key}`)
    }
  }
  return manifest
}

export async function loadRuntimeManifest(path = defaultManifestPath) {
  let manifest
  try {
    manifest = JSON.parse(await readFile(path, 'utf8'))
  } catch (error) {
    throw new Error(`could not read runtime manifest ${path}: ${error instanceof Error ? error.message : String(error)}`)
  }
  return validateRuntimeManifest(manifest)
}

export function currentRuntimeTarget(platform = process.platform, arch = process.arch) {
  const platformNames = { win32: 'windows', darwin: 'darwin', linux: 'linux' }
  const archNames = { x64: 'x64', amd64: 'x64', arm64: 'arm64', aarch64: 'arm64' }
  return `${platformNames[platform] ?? platform}-${archNames[arch] ?? arch}`
}

export function packagedRuntimeTarget(
  bundlePath,
  platform = process.platform,
  fallbackArch = process.arch,
) {
  const segments = String(bundlePath).replaceAll('\\', '/').split('/').reverse()
  for (const segment of segments) {
    const match = segment.match(/(?:^|-)(arm64|aarch64|x64|amd64)(?:-|$)/i)
    if (match) return currentRuntimeTarget(platform, match[1].toLowerCase())
  }
  return currentRuntimeTarget(platform, fallbackArch)
}

async function sha256File(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex')
}

export async function downloadVerifiedAsset(asset, destination) {
  validateAsset(asset, 'asset')
  await mkdir(dirname(destination), { recursive: true })
  const existing = await stat(destination).catch(() => null)
  if (existing?.isFile() && await sha256File(destination) === asset.sha256) return destination

  const partial = `${destination}.part-${process.pid}`
  await rm(partial, { force: true })
  try {
    const url = new URL(asset.url)
    if (url.protocol === 'file:') {
      await copyFile(fileURLToPath(url), partial)
    } else {
      const response = await fetch(url, { redirect: 'follow' })
      if (!response.ok) {
        throw new Error(`download failed for ${asset.id}: HTTP ${response.status}`)
      }
      await writeFile(partial, Buffer.from(await response.arrayBuffer()))
    }
    const actual = await sha256File(partial)
    if (actual !== asset.sha256) {
      throw new Error(`checksum mismatch: ${asset.id}; expected ${asset.sha256}, got ${actual}`)
    }
    await rm(destination, { force: true })
    await rename(partial, destination)
    return destination
  } finally {
    await rm(partial, { force: true })
  }
}

function runChecked(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: 'utf8',
    windowsHide: true,
    ...options,
  })
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(
      `${command} ${args.join(' ')} failed with exit ${result.status}\n${result.stderr || result.stdout || ''}`.trim(),
    )
  }
}

async function extractAsset(asset, archive, destination) {
  await rm(destination, { recursive: true, force: true })
  await mkdir(destination, { recursive: true })
  if (asset.archiveType === '7z-sfx') {
    // PortableGit ignores the generic `-o` argument and extracts into
    // %EXEDIR%/PortableGit.  Execute the checksum-verified SFX only after
    // copying it into the isolated staging directory, then flatten its fixed
    // wrapper folder.  The installer itself is removed before publication.
    const stagedArchive = join(destination, 'runtime-installer.exe')
    await copyFile(archive, stagedArchive)
    try {
      runChecked(stagedArchive, ['-y', '-gm2'], { cwd: destination })
    } finally {
      await rm(stagedArchive, { force: true })
    }
    const wrapper = join(destination, 'PortableGit')
    const wrapperInfo = await stat(wrapper).catch(() => null)
    if (wrapperInfo?.isDirectory()) {
      for (const entry of await readdir(wrapper)) {
        await rename(join(wrapper, entry), join(destination, entry))
      }
      await rm(wrapper, { recursive: true, force: true })
    }
    return
  }
  const args = ['-xf', archive, '-C', destination]
  if (asset.stripComponents > 0) args.push(`--strip-components=${asset.stripComponents}`)
  runChecked('tar', args)
}

function targetAssets(manifest, target) {
  const assets = manifest.assets[target]
  if (!assets) throw new Error(`runtime manifest does not contain target ${target}`)
  return assets
}

export async function fetchRuntimeSet({
  manifestPath = defaultManifestPath,
  runtimeRoot = defaultRuntimeRoot,
  cacheRoot = defaultRuntimeCacheRoot,
  target = currentRuntimeTarget(),
} = {}) {
  const manifest = await loadRuntimeManifest(manifestPath)
  const assets = targetAssets(manifest, target)
  const targetRoot = join(runtimeRoot, target)
  // Older builders cached archives below runtime/developer, which caused
  // Electron's extraResources rule to ship duplicate installers.  Remove
  // that generated cache before preparing the packageable runtime tree.
  await rm(join(runtimeRoot, '.downloads'), { recursive: true, force: true })
  await mkdir(cacheRoot, { recursive: true })
  await mkdir(targetRoot, { recursive: true })

  for (const key of runtimeKeys.filter(key => key in assets)) {
    const asset = assets[key]
    const archiveName = `${asset.id}.${asset.archiveType === '7z-sfx' ? 'exe' : asset.archiveType}`
    const archive = join(cacheRoot, archiveName)
    await downloadVerifiedAsset(asset, archive)
    const destination = join(targetRoot, asset.installDir)
    const staging = `${destination}.staging-${process.pid}`
    await rm(staging, { recursive: true, force: true })
    try {
      await extractAsset(asset, archive, staging)
      await rm(destination, { recursive: true, force: true })
      await rename(staging, destination)
    } finally {
      await rm(staging, { recursive: true, force: true })
    }
  }
  await assertRuntimeSetReady({ manifest, runtimeRoot, target, executeCommands: true })
}

async function directorySize(root) {
  const info = await stat(root)
  if (info.isFile()) return info.size
  let total = 0
  const { readdir } = await import('node:fs/promises')
  for (const entry of await readdir(root, { withFileTypes: true })) {
    total += await directorySize(join(root, entry.name))
  }
  return total
}

function smoke(executable, args) {
  runChecked(executable, args, { timeout: 30000 })
}

export async function assertRuntimeSetReady({
  manifest,
  runtimeRoot = defaultRuntimeRoot,
  target = currentRuntimeTarget(),
  executeCommands = true,
}) {
  validateRuntimeManifest(manifest)
  const assets = targetAssets(manifest, target)
  const targetRoot = join(runtimeRoot, target)
  const smokeCommands = {}
  let installedBytes = 0
  for (const key of runtimeKeys.filter(key => key in assets)) {
    const asset = assets[key]
    const installRoot = join(targetRoot, asset.installDir)
    const rootInfo = await stat(installRoot).catch(() => null)
    if (!rootInfo?.isDirectory()) throw new Error(`${asset.id} runtime is missing at ${installRoot}`)
    installedBytes += await directorySize(installRoot)
    for (const [name, relativePath] of Object.entries(asset.executables)) {
      const executable = join(installRoot, relativePath)
      const info = await stat(executable).catch(() => null)
      if (!info?.isFile()) throw new Error(`${asset.id} executable is missing: ${executable}`)
      smokeCommands[name] = executable
    }
  }
  if (installedBytes > maxInstalledBytes) {
    throw new Error(`bundled runtimes exceed the ${maxInstalledBytes} byte package size gate`)
  }
  if (executeCommands) {
    smoke(smokeCommands.python, ['--version'])
    smoke(smokeCommands.node, ['--version'])
    if (smokeCommands.git) smoke(smokeCommands.git, ['--version'])
    if (smokeCommands.bash) smoke(smokeCommands.bash, ['--version'])
  }
  return { installedBytes, executables: smokeCommands }
}

async function main() {
  const targetIndex = process.argv.indexOf('--target')
  const target = targetIndex >= 0 ? process.argv[targetIndex + 1] : currentRuntimeTarget()
  if (!target) throw new Error('--target requires a value')
  await fetchRuntimeSet({ target })
  console.log(`Bundled runtimes are ready for ${target}.`)
}

const invokedPath = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : ''
if (import.meta.url === invokedPath) {
  await main()
}
