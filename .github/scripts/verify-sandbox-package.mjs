import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const runtimeRoot = join(repoRoot, 'desktop', 'electron', 'runtime')
const manifestPath = join(runtimeRoot, 'runtime-manifest.json')
const catalogPath = join(runtimeRoot, 'runtime-pack-catalog.json')
const packageJsonPath = join(repoRoot, 'desktop', 'electron', 'package.json')
const failures = []

function fail(message) {
  failures.push(message)
}

function isFile(path) {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

function readJson(path, label) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch (error) {
    fail(`${label} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`)
    return null
  }
}

const requiredTargets = [
  'windows-x64',
  'windows-arm64',
  'linux-x64',
  'linux-arm64',
  'darwin-x64',
  'darwin-arm64',
]
const safeAssetName = /^[A-Za-z0-9][A-Za-z0-9._+-]*\.tar\.xz$/
const sha256Pattern = /^[0-9a-f]{64}$/i

function verifyCatalog(catalog, label, { requireFinalized }) {
  if (!catalog) return
  if (catalog.schemaVersion !== 1) fail(`${label} must use schemaVersion 1`)
  if (catalog.catalogVersion !== '2026-08-21.2') {
    fail(`${label} must pin catalogVersion 2026-08-21.2`)
  }
  if (catalog.releaseTag !== 'v2026.08.21.2') {
    fail(`${label} must pin releaseTag v2026.08.21.2`)
  }
  if (catalog.finalized !== true && catalog.finalized !== false) {
    fail(`${label} must declare a boolean finalized flag`)
  }

  const targets = catalog.targets
  if (!targets || typeof targets !== 'object' || Array.isArray(targets)) {
    fail(`${label} is missing targets`)
    return
  }
  if (catalog.finalized !== true) {
    if (requireFinalized) fail(`${label} is not finalized; refusing to publish a desktop package`)
    if (Object.keys(targets).length !== 0) {
      fail(`${label} must not carry partial targets while finalized=false`)
    }
    return
  }

  const actualTargets = Object.keys(targets).sort()
  const expectedTargets = [...requiredTargets].sort()
  if (JSON.stringify(actualTargets) !== JSON.stringify(expectedTargets)) {
    fail(`${label} target set differs: ${actualTargets.join(', ')}`)
  }

  for (const target of requiredTargets) {
    const components = targets[target]
    if (!components || typeof components !== 'object' || Array.isArray(components)) {
      fail(`${label} is missing ${target}`)
      continue
    }
    const componentIds = target.startsWith('windows-')
      ? ['python', 'node', 'gitBash']
      : ['python', 'node']
    for (const componentId of componentIds) {
      const asset = components[componentId]
      if (!asset || typeof asset !== 'object' || Array.isArray(asset)) {
        fail(`${label} ${target} is missing ${componentId}`)
        continue
      }
      if (typeof asset.asset !== 'string' || !safeAssetName.test(asset.asset)) {
        fail(`${label} ${target}/${componentId} has an unsafe asset name`)
      }
      if (asset.archiveType !== 'tar.xz') {
        fail(`${label} ${target}/${componentId} must use tar.xz`)
      }
      if (typeof asset.version !== 'string' || !asset.version) {
        fail(`${label} ${target}/${componentId} is missing version`)
      }
      if (!Number.isSafeInteger(asset.sizeBytes) || asset.sizeBytes <= 0) {
        fail(`${label} ${target}/${componentId} has an invalid sizeBytes`)
      }
      if (!Number.isSafeInteger(asset.unpackedSizeBytes) || asset.unpackedSizeBytes <= 0) {
        fail(`${label} ${target}/${componentId} has an invalid unpackedSizeBytes`)
      }
      if (typeof asset.sha256 !== 'string' || !sha256Pattern.test(asset.sha256)) {
        fail(`${label} ${target}/${componentId} has an invalid sha256`)
      }
      if ('url' in asset || 'urls' in asset || 'sourceUrl' in asset) {
        fail(`${label} ${target}/${componentId} must not embed a mutable download URL`)
      }
    }
    if (!target.startsWith('windows-') && 'gitBash' in components) {
      fail(`${label} ${target} must not declare Git Bash`)
    }
  }
}

const manifest = readJson(manifestPath, 'runtime layout manifest')
if (manifest && manifest.schemaVersion !== 1) {
  fail('runtime layout manifest must use schemaVersion 1')
}
verifyCatalog(readJson(catalogPath, 'runtime-pack catalog'), 'runtime-pack catalog', {
  requireFinalized: process.argv.includes('--release-source'),
})

const packageJson = readJson(packageJsonPath, 'Electron package.json')
if (packageJson) {
  const resources = packageJson.build?.extraResources
  const expectedMappings = new Map([
    ['runtime/gateway', 'runtime/gateway'],
    ['runtime/runtime-manifest.json', 'runtime/runtime-manifest.json'],
    ['runtime/runtime-pack-catalog.json', 'runtime/runtime-pack-catalog.json'],
  ])
  if (!Array.isArray(resources)) {
    fail('Electron extraResources must be an explicit allowlist')
  } else {
    for (const [from, to] of expectedMappings) {
      if (!resources.some(entry => entry?.from === from && entry?.to === to)) {
        fail(`Electron extraResources is missing ${from}`)
      }
    }
    for (const entry of resources) {
      const from = String(entry?.from || '').replaceAll('\\', '/')
      const to = String(entry?.to || '').replaceAll('\\', '/')
      if (from === 'runtime' || to === 'runtime') {
        fail('Electron extraResources must not copy the entire runtime directory')
      }
      if (to.startsWith('runtime/') && expectedMappings.get(from) !== to) {
        fail(`Electron extraResources contains an unexpected runtime mapping: ${from} -> ${to}`)
      }
    }
  }

  if (packageJson.build?.nsis?.include !== undefined) {
    fail('Electron NSIS must use managed upgrade cleanup without a custom recursive delete')
  }
  if (isFile(join(repoRoot, 'desktop', 'electron', 'build', 'installer.nsh'))) {
    fail('Electron NSIS custom installer cleanup must not be present')
  }
}

const mainSource = readFileSync(
  join(repoRoot, 'desktop', 'electron', 'src', 'main.ts'),
  'utf8',
)
if (/opensquilla-gateway(?:\.exe)?['"`]?\s*,\s*\[\s*['"]-m['"]/.test(mainSource)) {
  fail('desktop source launches the frozen gateway with the invalid -m entrypoint')
}
if (!mainSource.includes('reportSandboxUnavailable')) {
  fail('desktop source is missing the sandbox-unavailable soft-landing prompt')
}

const packageArgumentIndex = process.argv.indexOf('--package')
if (packageArgumentIndex >= 0) {
  const packageRoot = resolve(process.argv[packageArgumentIndex + 1] || '')
  const resources = existsSync(join(packageRoot, 'resources'))
    ? join(packageRoot, 'resources')
    : packageRoot
  const packagedRuntimeRoot = join(resources, 'runtime')
  const packagedManifest = join(packagedRuntimeRoot, 'runtime-manifest.json')
  const packagedCatalog = join(packagedRuntimeRoot, 'runtime-pack-catalog.json')
  if (!isFile(packagedManifest)) fail('package is missing runtime/runtime-manifest.json')
  if (!isFile(packagedCatalog)) {
    fail('package is missing runtime/runtime-pack-catalog.json')
  } else {
    verifyCatalog(
      readJson(packagedCatalog, 'packaged runtime-pack catalog'),
      'packaged runtime-pack catalog',
      { requireFinalized: process.argv.includes('--release-package') },
    )
  }
  if (existsSync(join(packagedRuntimeRoot, 'developer'))) {
    fail('package must not contain bundled developer runtimes')
  }
  const gatewayNames = process.platform === 'win32'
    ? ['opensquilla-gateway.exe']
    : ['opensquilla-gateway']
  if (!gatewayNames.some(name => (
    isFile(join(packagedRuntimeRoot, 'gateway', name))
    || isFile(join(packagedRuntimeRoot, 'gateway', 'opensquilla-gateway', name))
  ))) {
    fail('package is missing its frozen gateway executable')
  }
}

if (failures.length) {
  console.error('Sandbox package contract failed:')
  for (const failure of failures) console.error(`- ${failure}`)
  process.exit(1)
}
console.log('Sandbox package contract passed.')
