import { spawnSync } from 'node:child_process'
import { existsSync, statSync } from 'node:fs'
import { lstat, readdir, readFile, stat } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

import { verifyInstallerProgressPolicy } from './installer-progress-policy.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '..', '..')
const runtimeGatewayDir = join(packageRoot, 'runtime', 'gateway')
const sourceMainPath = join(packageRoot, 'src', 'main.ts')
const compiledMainPath = join(packageRoot, 'dist', 'main.js')
const packageJsonPath = join(packageRoot, 'package.json')
const desktopOutputDir = join(repoRoot, 'dist', 'desktop-electron')

const failures = []

function fail(message) {
  failures.push(message)
}

function pathIsFileSync(path) {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

async function pathExists(path) {
  return (await lstat(path).catch(() => null)) !== null
}

async function readJsonFile(path, label) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch (error) {
    fail(`${label} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`)
    return null
  }
}

async function verifyRuntimeMetadata(runtimeRoot, label) {
  const manifestPath = join(runtimeRoot, 'runtime-manifest.json')
  const catalogPath = join(runtimeRoot, 'runtime-pack-catalog.json')
  const manifest = await readJsonFile(manifestPath, `${label} runtime manifest`)
  const catalog = await readJsonFile(catalogPath, `${label} runtime-pack catalog`)

  if (manifest && manifest.schemaVersion !== 1) {
    fail(`${label} runtime manifest must use schemaVersion 1`)
  }
  if (catalog) {
    if (catalog.schemaVersion !== 1) {
      fail(`${label} runtime-pack catalog must use schemaVersion 1`)
    }
    if (typeof catalog.catalogVersion !== 'string' || !catalog.catalogVersion.trim()) {
      fail(`${label} runtime-pack catalog is missing catalogVersion`)
    }
    if (typeof catalog.releaseTag !== 'string' || !/^v[A-Za-z0-9._-]+$/.test(catalog.releaseTag)) {
      fail(`${label} runtime-pack catalog has an invalid releaseTag`)
    }
    if (!catalog.targets || typeof catalog.targets !== 'object' || Array.isArray(catalog.targets)) {
      fail(`${label} runtime-pack catalog is missing targets`)
    }
    if (catalog.finalized !== true && catalog.finalized !== false) {
      fail(`${label} runtime-pack catalog must declare a boolean finalized flag`)
    } else if (catalog.finalized === false && Object.keys(catalog.targets).length !== 0) {
      fail(`${label} runtime-pack catalog must not carry partial targets while finalized=false`)
    }
  }
}

async function verifyDeveloperRuntimeAbsent(runtimeRoot, label) {
  const developerRoot = join(runtimeRoot, 'developer')
  if (await pathExists(developerRoot)) {
    fail(`${label} must not contain bundled developer runtimes: ${developerRoot}`)
  }
}

function gatewayBinary(root, platform) {
  const binaryName = platform === 'win32' ? 'opensquilla-gateway.exe' : 'opensquilla-gateway'
  const candidates = [join(root, 'opensquilla-gateway', binaryName), join(root, binaryName)]
  const binary = candidates.find(pathIsFileSync)
  return { binary, candidates }
}

function verificationEnv() {
  const keys = [
    'PATH',
    'Path',
    'HOME',
    'USERPROFILE',
    'TMPDIR',
    'TEMP',
    'TMP',
    'SystemRoot',
    'ComSpec',
    'PATHEXT',
    'LANG',
    'LC_ALL',
  ]
  const env = {
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8:replace',
  }

  for (const key of keys) {
    if (process.env[key] !== undefined) env[key] = process.env[key]
  }

  return env
}

function outputTail(output) {
  const tail = [output.stdout, output.stderr]
    .filter(Boolean)
    .join('\n')
    .trim()
    .split(/\r?\n/)
    .slice(-12)
    .join('\n')
    .trim()

  return tail ? `\nOutput tail:\n${tail}` : ''
}

function requireGatewayBinary(root, label, platform) {
  const { binary, candidates } = gatewayBinary(root, platform)
  if (!binary) {
    fail(`${label} gateway binary is missing; checked ${candidates.join(', ')}`)
    return null
  }
  return binary
}

function verifyGatewayCommand(binary, label, args, options = {}) {
  const result = spawnSync(binary, args, {
    cwd: dirname(binary),
    encoding: 'utf8',
    env: verificationEnv(),
    input: options.input,
    timeout: options.timeout ?? 30000,
    windowsHide: true,
  })

  const commandLabel = `${label} gateway command ${args.join(' ')}`
  if (result.error) {
    fail(`${commandLabel} could not start: ${result.error.message}${outputTail(result)}`)
    return
  }

  if (result.status !== 0) {
    const exitReason = result.signal ? `signal ${result.signal}` : `exit ${result.status}`
    fail(`${commandLabel} failed with ${exitReason}${outputTail(result)}`)
  }
}

function verifyMacLightgbmRuntime(files, label) {
  const lightgbmLibs = files.filter((path) => path.endsWith(join('lightgbm', 'lib', 'lib_lightgbm.dylib')))
  if (lightgbmLibs.length === 0) {
    fail(`${label} runtime is missing lightgbm/lib/lib_lightgbm.dylib`)
    return
  }

  for (const lightgbmLib of lightgbmLibs) {
    const bundledLibomp = join(dirname(lightgbmLib), 'libomp.dylib')
    if (!files.includes(bundledLibomp)) {
      fail(`${label} runtime is missing bundled libomp.dylib next to ${lightgbmLib}`)
    }

    if (process.platform !== 'darwin') continue
    const result = spawnSync('otool', ['-L', lightgbmLib], {
      encoding: 'utf8',
      windowsHide: true,
    })
    if (result.error) {
      fail(`${label} could not inspect ${lightgbmLib} with otool: ${result.error.message}`)
    } else if (result.status !== 0) {
      fail(`${label} otool -L failed for ${lightgbmLib}${outputTail(result)}`)
    } else if (!result.stdout.includes('@loader_path/libomp.dylib')) {
      fail(`${label} ${lightgbmLib} does not load libomp.dylib via @loader_path`)
    }
  }
}

async function listFiles(root) {
  const files = []

  async function walk(dir) {
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch (error) {
      fail(`${root} could not be read: ${error instanceof Error ? error.message : String(error)}`)
      return
    }

    for (const entry of entries) {
      const path = join(dir, entry.name)
      if (entry.isDirectory()) {
        await walk(path)
      } else if (entry.isFile() && entry.name !== '.DS_Store') {
        files.push(path)
      }
    }
  }

  await walk(root)
  return files
}

async function verifyRuntime(root, label, { platform, executeCommands }) {
  if (!existsSync(root)) {
    fail(`${label} runtime is missing at ${root}`)
    return
  }

  const info = await stat(root).catch(() => null)
  if (!info?.isDirectory()) {
    fail(`${label} runtime is not a directory: ${root}`)
    return
  }

  const files = await listFiles(root)
  if (files.length === 0) {
    fail(`${label} runtime is empty: ${root}`)
    return
  }

  const compatFile = files.find((path) => path.endsWith(join('opensquilla', 'compat', 'aiosqlite.py')))
  if (!compatFile) {
    fail(`${label} runtime is missing opensquilla/compat/aiosqlite.py`)
  } else {
    const source = await readFile(compatFile, 'utf8')
    if (!source.includes('async def create_function(') || !source.includes('self._conn.create_function')) {
      fail(`${label} runtime aiosqlite.py does not contain _AsyncConnection.create_function`)
    }
  }

  const binary = requireGatewayBinary(root, label, platform)
  if (executeCommands) {
    if (!binary) return
    verifyGatewayCommand(binary, label, ['--help'])
    verifyGatewayCommand(binary, label, ['code-task', '--help'])
    verifyGatewayCommand(binary, label, ['code-task', 'stage-task-file'], { input: 'desktop package smoke\n' })
    verifyGatewayCommand(binary, label, ['code-task', 'smoke-imports'], { timeout: 120000 })
    verifyGatewayCommand(binary, label, ['code-task', 'smoke-router'], { timeout: 120000 })
  }

  if (platform === 'darwin') {
    verifyMacLightgbmRuntime(files, label)
  }
}

function verifyMainProcess(source, label) {
  for (const expected of [
    'gatewayStartPromise',
    'openOrResumeDesktopApp',
    'ensureGatewayStarted',
    'isCurrentWindowAtControlUi',
  ]) {
    if (!source.includes(expected)) fail(`${label} main process is missing ${expected}`)
  }

  const helperIndex = source.indexOf('async function openOrResumeDesktopApp')
  const createIndex = source.indexOf('await createMainWindow()', helperIndex)
  const ensureIndex = source.indexOf('ensureGatewayStarted()', helperIndex)
  if (helperIndex === -1 || createIndex === -1 || ensureIndex === -1 || createIndex > ensureIndex) {
    fail(`${label} main process does not create the desktop window before gateway startup`)
  }

  const activateIndex = source.indexOf('async function activateMainWindow')
  const activateSource = activateIndex === -1 ? '' : source.slice(activateIndex, activateIndex + 2_400)
  const activateRoutesToDesktopOpen = activateSource.includes('openOrResumeDesktopApp()')
  const revealRoutesToDesktopOpen =
    /function revealDesktopApp\([^)]*\)[^{]*\{[^}]{0,300}activateMainWindow\(/.test(source)
    && activateRoutesToDesktopOpen
  const handlerRoutesToDesktopOpen = (handlerPattern) => {
    const directRoute = new RegExp(
      `${handlerPattern}[\\s\\S]{0,240}openOrResumeDesktopApp`,
    ).test(source)
    const revealRoute = new RegExp(
      `${handlerPattern}[\\s\\S]{0,240}revealDesktopApp`,
    ).test(source)
    return directRoute || (revealRoute && revealRoutesToDesktopOpen)
  }

  if (!handlerRoutesToDesktopOpen(String.raw`app\.on\(['"]activate['"]`)) {
    fail(`${label} main process activate handler does not route through openOrResumeDesktopApp`)
  }
  if (!handlerRoutesToDesktopOpen('second-instance')) {
    fail(`${label} main process second-instance handler does not route through openOrResumeDesktopApp`)
  }

  const loadCurrentIndex = source.indexOf('async function loadControlUiIntoCurrentWindow')
  const controlGuardIndex = source.indexOf('isCurrentWindowAtControlUi(window, gatewayUrl)', loadCurrentIndex)
  const controlLoadIndex = source.indexOf('loadControlUi(window, gatewayUrl)', loadCurrentIndex)
  if (
    loadCurrentIndex === -1
    || controlGuardIndex === -1
    || controlLoadIndex === -1
    || controlGuardIndex > controlLoadIndex
  ) {
    fail(`${label} main process reloads the Control UI before checking whether it is already loaded`)
  }

  const onboardingIndex = source.indexOf('async function runOnboarding')
  const onboardingEndIndex = source.indexOf('async function pathExists', onboardingIndex)
  const onboardingSource =
    onboardingIndex === -1 || onboardingEndIndex === -1
      ? ''
      : source.slice(onboardingIndex, onboardingEndIndex)
  const parentIndex = onboardingSource.search(
    /const\s+parentWindow\s*=\s*currentMainWindow\(\)/,
  )
  const onboardingWindowIndex = onboardingSource.search(
    /const\s+window\s*=\s*new\s+(?:[A-Za-z_$][\w$]*\.)*BrowserWindow\s*\(\s*\{/,
  )
  const parentOptionIndex = onboardingSource.search(
    /parent\s*:\s*parentWindow\s*\?\?\s*undefined/,
  )
  const modalOptionIndex = onboardingSource.search(
    /modal\s*:\s*Boolean\(parentWindow\)/,
  )
  const onboardingWindowAssignmentIndex = onboardingSource.search(
    /onboardingWindow\s*=\s*window\b/,
  )
  if (
    onboardingIndex === -1
    || onboardingEndIndex === -1
    || onboardingWindowIndex === -1
    || parentIndex === -1
    || parentOptionIndex === -1
    || modalOptionIndex === -1
    || onboardingWindowAssignmentIndex === -1
    || parentIndex > onboardingWindowIndex
    || parentOptionIndex < onboardingWindowIndex
    || modalOptionIndex < parentOptionIndex
    || onboardingWindowAssignmentIndex < modalOptionIndex
  ) {
    fail(`${label} main process does not make first-run onboarding an owned modal child window`)
  }

  const focusIndex = source.indexOf('function focusMainWindow')
  const focusSource = focusIndex === -1 ? '' : source.slice(focusIndex, focusIndex + 800)
  const onboardingFocusMatch = /if\s*\(\s*focusOnboardingWindow\(\)\s*\)\s*(?:\{\s*)?return true\s*;?/.exec(focusSource)
  const mainFocusMatch =
    /if\s*\(\s*!mainWindow\s*\|\|\s*mainWindow\.isDestroyed\(\)\s*\)\s*(?:\{\s*)?return false\s*;?/.exec(
      focusSource,
    )
  if (
    focusIndex === -1
    || !onboardingFocusMatch
    || !mainFocusMatch
    || onboardingFocusMatch.index > mainFocusMatch.index
  ) {
    fail(`${label} main process does not prefer the onboarding window when focusing`)
  }
}

async function verifySourceMain() {
  if (!existsSync(sourceMainPath)) {
    fail(`source Electron main process is missing at ${sourceMainPath}`)
    return
  }

  verifyMainProcess(await readFile(sourceMainPath, 'utf8'), 'source')
}

async function verifyInstallerDataPolicy() {
  const packageJson = JSON.parse(await readFile(packageJsonPath, 'utf8'))
  if (packageJson.build?.nsis?.deleteAppDataOnUninstall !== false) {
    fail('NSIS uninstall must preserve Desktop profile data (deleteAppDataOnUninstall=false)')
  }
  for (const installerProgressFailure of await verifyInstallerProgressPolicy(packageRoot, packageJson)) {
    fail(installerProgressFailure)
  }
  const protocols = packageJson.build?.protocols
  if (
    !Array.isArray(protocols)
    || protocols.length !== 1
    || protocols[0]?.name !== 'OpenSquilla'
    || !Array.isArray(protocols[0]?.schemes)
    || protocols[0].schemes.length !== 1
    || protocols[0].schemes[0] !== 'opensquilla'
  ) {
    fail('electron-builder must register only the opensquilla URL protocol')
  }

  const extraResources = packageJson.build?.extraResources
  const expectedRuntimeResources = new Map([
    ['runtime/gateway', 'runtime/gateway'],
    ['runtime/runtime-manifest.json', 'runtime/runtime-manifest.json'],
    ['runtime/runtime-pack-catalog.json', 'runtime/runtime-pack-catalog.json'],
  ])
  if (!Array.isArray(extraResources)) {
    fail('electron-builder extraResources must be an explicit allowlist')
    return
  }
  for (const [from, to] of expectedRuntimeResources) {
    if (!extraResources.some((entry) => entry?.from === from && entry?.to === to)) {
      fail(`electron-builder extraResources is missing ${from}`)
    }
  }
  for (const entry of extraResources) {
    const from = typeof entry?.from === 'string' ? entry.from.replaceAll('\\', '/') : ''
    const to = typeof entry?.to === 'string' ? entry.to.replaceAll('\\', '/') : ''
    if (from === 'runtime' || to === 'runtime') {
      fail('electron-builder must not copy the entire runtime directory')
    }
    if (to.startsWith('runtime/') && expectedRuntimeResources.get(from) !== to) {
      fail(`electron-builder has an unexpected runtime resource mapping: ${from} -> ${to}`)
    }
  }
}

async function verifyCompiledMain() {
  if (!existsSync(compiledMainPath)) {
    fail(`compiled Electron main process is missing at ${compiledMainPath}; run npm run build first`)
    return
  }

  verifyMainProcess(await readFile(compiledMainPath, 'utf8'), 'compiled')
}

async function findGeneratedBundles(root) {
  const bundles = []
  const seenResourcesDirs = new Set()
  if (!existsSync(root)) return bundles

  function addBundle(bundle) {
    if (seenResourcesDirs.has(bundle.resourcesDir)) return
    seenResourcesDirs.add(bundle.resourcesDir)
    bundles.push(bundle)
  }

  async function walk(dir, depth) {
    if (depth > 5) return
    const entries = await readdir(dir, { withFileTypes: true }).catch(() => [])
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const path = join(dir, entry.name)
      if (entry.name.endsWith('.app')) {
        addBundle({
          label: `app bundle ${path}`,
          resourcesDir: join(path, 'Contents', 'Resources'),
          platform: 'darwin',
        })
      } else if (entry.name === 'win-unpacked' || entry.name === 'linux-unpacked') {
        addBundle({
          label: `generated bundle ${path}`,
          resourcesDir: join(path, 'resources'),
          platform: entry.name === 'win-unpacked' ? 'win32' : 'linux',
        })
      } else {
        await walk(path, depth + 1)
      }
    }
  }

  await walk(root, 0)
  return bundles
}

async function readAsarText(asarPath, innerPath) {
  const asar = await import('@electron/asar')
  return asar.extractFile(asarPath, innerPath).toString('utf8')
}

async function verifyAsarPackageVersion(asarPath, label) {
  let packageJson
  try {
    packageJson = JSON.parse(await readAsarText(asarPath, 'package.json'))
  } catch (error) {
    fail(`${label} app.asar package.json could not be inspected: ${error instanceof Error ? error.message : String(error)}`)
    return
  }

  const version = typeof packageJson.version === 'string' ? packageJson.version : ''
  const semverPattern = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/
  if (!semverPattern.test(version) || /\d(?:a|b|rc)\d+$/.test(version)) {
    fail(
      `${label} app.asar package.json version is not npm semver: ${JSON.stringify(version)}; ` +
        'prereleases must use 0.5.0-rc2 style, not 0.5.0rc2'
    )
  }
}

async function verifyGeneratedBundle({ label, resourcesDir, platform }) {
  const runtimeRoot = join(resourcesDir, 'runtime')
  await verifyRuntime(join(runtimeRoot, 'gateway'), label, {
    platform,
    executeCommands: platform === process.platform,
  })
  await verifyRuntimeMetadata(runtimeRoot, label)
  await verifyDeveloperRuntimeAbsent(runtimeRoot, label)

  const asarPath = join(resourcesDir, 'app.asar')
  if (!existsSync(asarPath)) {
    fail(`${label} is missing app.asar`)
    return
  }

  try {
    await verifyAsarPackageVersion(asarPath, label)
    verifyMainProcess(await readAsarText(asarPath, 'dist/main.js'), label)
  } catch (error) {
    fail(`${label} app.asar could not be inspected: ${error instanceof Error ? error.message : String(error)}`)
  }
}

await verifyRuntime(runtimeGatewayDir, 'source', { platform: process.platform, executeCommands: true })
await verifyRuntimeMetadata(join(packageRoot, 'runtime'), 'source')
await verifyInstallerDataPolicy()
await verifySourceMain()
await verifyCompiledMain()

const generatedBundles = await findGeneratedBundles(desktopOutputDir)
for (const bundle of generatedBundles) {
  await verifyGeneratedBundle(bundle)
}

if (failures.length > 0) {
  console.error('OpenSquilla desktop package verification failed:')
  for (const failure of failures) console.error(`- ${failure}`)
  process.exit(1)
}

console.log('OpenSquilla desktop package verification passed.')
