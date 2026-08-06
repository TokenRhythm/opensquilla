import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')
const manifestPath = join(
  repoRoot,
  'desktop',
  'electron',
  'runtime',
  'runtime-manifest.json',
)
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
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

const requiredTargets = [
  'windows-x64',
  'windows-arm64',
  'linux-x64',
  'linux-arm64',
  'darwin-x64',
  'darwin-arm64',
]
for (const target of requiredTargets) {
  const assets = manifest.assets?.[target]
  if (!assets) {
    fail(`runtime manifest is missing ${target}`)
    continue
  }
  for (const key of ['python', 'node']) {
    if (!assets[key]?.version || !assets[key]?.sha256) {
      fail(`${target} is missing pinned ${key} metadata`)
    }
  }
  if (target.startsWith('windows-') && !assets.gitBash?.executables?.bash) {
    fail(`${target} is missing the bundled Git Bash runtime`)
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
  const packagedManifest = join(resources, 'runtime', 'runtime-manifest.json')
  if (!isFile(packagedManifest)) fail('package is missing runtime/runtime-manifest.json')
  const packagedRuntimeRoot = join(resources, 'runtime', 'developer')
  const packagedTargets = existsSync(packagedRuntimeRoot)
    ? readdirSync(packagedRuntimeRoot, { withFileTypes: true })
      .filter(entry => entry.isDirectory())
      .map(entry => entry.name)
    : []
  if (!packagedTargets.length) {
    fail('package is missing bundled developer runtimes')
  }
  const packagedRuntimeManifest = isFile(packagedManifest)
    ? JSON.parse(readFileSync(packagedManifest, 'utf8'))
    : manifest
  for (const target of packagedTargets) {
    const assets = packagedRuntimeManifest.assets?.[target]
    if (!assets) {
      fail(`package contains an unknown bundled runtime target: ${target}`)
      continue
    }
    const runtimeKeys = target.startsWith('windows-')
      ? ['python', 'node', 'gitBash']
      : ['python', 'node']
    for (const key of runtimeKeys) {
      const asset = assets[key]
      if (!asset?.installDir || !asset.executables) {
        fail(`package manifest is missing bundled ${target}/${key} metadata`)
        continue
      }
      for (const [name, relativePath] of Object.entries(asset.executables)) {
        const executable = join(
          packagedRuntimeRoot,
          target,
          asset.installDir,
          String(relativePath),
        )
        if (!isFile(executable)) {
          fail(`package is missing bundled ${target}/${key}/${name}: ${relativePath}`)
        }
      }
    }
  }
  const gatewayNames = process.platform === 'win32'
    ? ['opensquilla-gateway.exe']
    : ['opensquilla-gateway']
  if (!gatewayNames.some(name => (
    isFile(join(resources, 'runtime', 'gateway', name))
    || isFile(join(resources, 'runtime', 'gateway', 'opensquilla-gateway', name))
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
