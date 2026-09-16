import { createHash } from 'node:crypto'
import { closeSync, existsSync, openSync, readdirSync, readFileSync, readlinkSync, readSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export const gatewayBuildRecord = 'gateway-build.json'
const routerRelative = 'squilla_router/models/v4.2_phase3_inference'
const lfsHeader = 'version https://git-lfs.github.com/spec/v1'
const rebuild = 'Run npm run build:web && npm run build:gateway before prepared packaging.'

function fail(message) {
  throw new Error(`Gateway integrity: ${message}. ${rebuild}`)
}

export function fileHash(path) {
  const hash = createHash('sha256')
  const fd = openSync(path, 'r')
  const buffer = Buffer.alloc(1024 * 1024)
  try {
    let size
    while ((size = readSync(fd, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, size))
  } finally {
    closeSync(fd)
  }
  return hash.digest('hex')
}

function inventory(root, { exclude = () => false } = {}) {
  const files = {}
  function walk(relative = '') {
    for (const entry of readdirSync(join(root, relative), { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
      const name = relative ? `${relative}/${entry.name}` : entry.name
      // Python smoke commands can create caches after a successful build.
      if (entry.name === '__pycache__' || exclude(name)) continue
      if (entry.isDirectory()) walk(name)
      else if (entry.isSymbolicLink()) files[name] = `link:${readlinkSync(join(root, name))}`
      else if (entry.isFile()) files[name] = fileHash(join(root, name))
    }
  }
  walk()
  return files
}

function assertInventory(actual, expected, label) {
  const problems = []
  for (const name of Object.keys(expected)) {
    if (!(name in actual)) problems.push(`${name}: missing`)
    else if (actual[name] !== expected[name]) problems.push(`${name}: content changed`)
  }
  for (const name of Object.keys(actual)) {
    if (!(name in expected)) problems.push(`${name}: unexpected`)
  }
  if (problems.length) fail(`${label}: ${problems.slice(0, 12).join('; ')}`)
}

export function assertRouterIntegrity(bundleDir, expectedBundleDir = bundleDir) {
  const manifestPath = join(bundleDir, 'artifact_manifest.json')
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  if (manifest.schema_version !== 1 || !Array.isArray(manifest.files) || manifest.files.length === 0) {
    fail('Router manifest must contain a nonempty version 1 file list')
  }
  if (fileHash(manifestPath) !== fileHash(join(expectedBundleDir, 'artifact_manifest.json'))) {
    fail('Router artifact_manifest.json differs from source')
  }
  const seen = new Set()
  for (const entry of manifest.files) {
    const name = entry.path
    if (typeof name !== 'string' || !name || /[\\:]/.test(name) || name.split('/').some((part) => !part || part === '.' || part === '..') || seen.has(name)) {
      fail(`invalid or duplicate Router manifest path: ${JSON.stringify(name)}`)
    }
    seen.add(name)
    const path = join(bundleDir, name)
    if (!existsSync(path) || !statSync(path).isFile()) fail(`Router ${name}: missing`)
    const fd = openSync(path, 'r')
    const header = Buffer.alloc(lfsHeader.length)
    try { readSync(fd, header, 0, header.length, 0) } finally { closeSync(fd) }
    if (header.toString('utf8') === lfsHeader) fail(`Router ${name}: Git LFS pointer file, not the real router artifact`)
    if (!Number.isInteger(entry.size_bytes) || statSync(path).size !== entry.size_bytes) fail(`Router ${name}: size mismatch`)
    if (!/^[a-f0-9]{64}$/.test(entry.sha256 || '') || fileHash(path) !== entry.sha256) fail(`Router ${name}: SHA256 mismatch`)
  }
}

export function assertGatewayResources(repoRoot, runtimeRoot) {
  // build-gateway uses PyInstaller onedir with its default _internal layout.
  // Validate the package the frozen interpreter resolves, not an arbitrary copy.
  const packageDir = join(runtimeRoot, 'opensquilla-gateway', '_internal', 'opensquilla')
  const migrationFiles = (root) => Object.fromEntries(
    Object.entries(inventory(root)).filter(([name]) => /^V[^/]*\.py$/.test(name)),
  )
  const expected = migrationFiles(join(repoRoot, 'migrations'))
  if (Object.keys(expected).length === 0) fail('source migration set is empty')
  assertInventory(migrationFiles(join(packageDir, '_migrations')), expected, 'migrations')
  assertRouterIntegrity(join(packageDir, routerRelative), join(repoRoot, 'src', 'opensquilla', routerRelative))
}

export function gatewayInputs(repoRoot) {
  const inputs = {}
  const roots = [
    'src/opensquilla', 'migrations', 'opensquilla-webui/dist',
    'desktop/electron/scripts/pyinstaller_runtime_hooks',
  ]
  for (const root of roots) {
    const files = inventory(join(repoRoot, root), {
      // The verified WebUI is copied separately, and editable installs may
      // stage migrations. Neither generated copy is a Gateway source input.
      exclude: (name) => root === 'src/opensquilla' && (
        name === '_migrations' || name === 'gateway/static/dist'
      ),
    })
    for (const [name, hash] of Object.entries(files)) inputs[`${root}/${name}`] = hash
  }
  for (const path of [
    'pyproject.toml', 'uv.lock', 'hatch_build.py',
    'desktop/electron/package.json', 'desktop/electron/package-lock.json',
    'desktop/electron/scripts/build-gateway.mjs', 'desktop/electron/scripts/gateway-entry.py',
    'desktop/electron/scripts/gateway-integrity.mjs',
  ]) inputs[path] = fileHash(join(repoRoot, path))
  return inputs
}

function runtimeInventory(runtimeRoot) {
  return inventory(runtimeRoot, { exclude: (name) => name === gatewayBuildRecord })
}

export function writeGatewayBuildRecord(repoRoot, runtimeRoot, inputs) {
  const recordPath = join(runtimeRoot, gatewayBuildRecord)
  rmSync(recordPath, { force: true })
  assertGatewayResources(repoRoot, runtimeRoot)
  assertInventory(gatewayInputs(repoRoot), inputs, 'inputs changed during Gateway build')
  writeFileSync(recordPath, `${JSON.stringify({
    schemaVersion: 1,
    platform: process.platform,
    arch: process.arch,
    inputs,
    outputs: runtimeInventory(runtimeRoot),
  }, null, 2)}\n`)
}

export function verifyGatewayIntegrity(repoRoot, runtimeRoot, { prepared = false, platform = process.platform } = {}) {
  assertGatewayResources(repoRoot, runtimeRoot)
  const record = JSON.parse(readFileSync(join(runtimeRoot, gatewayBuildRecord), 'utf8'))
  if (record.schemaVersion !== 1 || record.platform !== platform || (prepared && record.arch !== process.arch)
    || !record.inputs || !record.outputs || !Object.keys(record.outputs).length) fail('missing or incompatible Gateway build record')
  assertInventory(gatewayInputs(repoRoot), record.inputs, 'stale Gateway build inputs')
  // Signing changes native binaries. Bind every prepared output before signing;
  // final bundles still require source-matched migrations/models and provenance,
  // with the existing platform signing gate responsible for signed executables.
  if (prepared) assertInventory(runtimeInventory(runtimeRoot), record.outputs, 'prepared Gateway outputs')
}
