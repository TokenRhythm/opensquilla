import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '..', '..')
const runtimeGatewayDir = join(packageRoot, 'runtime', 'gateway')
const pyinstallerWorkDir = join(packageRoot, '.pyinstaller')
const entryPath = join(scriptDir, 'gateway-entry.py')
const caRuntimeHookPath = join(scriptDir, 'pyinstaller_runtime_hooks', 'ensure_ca_trust.py')
const controlUiDistDir = join(repoRoot, 'src', 'opensquilla', 'gateway', 'static', 'dist')
const controlUiVerifier = join(repoRoot, 'opensquilla-webui', 'scripts', 'verify-dist.mjs')
const routerBundleDir = join(repoRoot, 'src', 'opensquilla', 'squilla_router', 'models', 'v4.2_phase3_inference')
const addDataSeparator = process.platform === 'win32' ? ';' : ':'
const gitLfsPointerHeader = 'version https://git-lfs.github.com/spec/v1'

function findFilesByName(root, fileName) {
  const matches = []

  function walk(dir) {
    let entries
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      return
    }

    for (const entry of entries) {
      const path = join(dir, entry.name)
      if (entry.isDirectory()) {
        walk(path)
      } else if (entry.isFile() && entry.name === fileName) {
        matches.push(path)
      }
    }
  }

  walk(root)
  return matches
}

function findMacLibomp() {
  const candidates = []
  candidates.push(...findFilesByName(runtimeGatewayDir, 'libomp.dylib'))

  const brew = spawnSync('brew', ['--prefix', 'libomp'], {
    encoding: 'utf8',
    windowsHide: true,
  })

  if (brew.status === 0) {
    const prefix = brew.stdout.trim()
    if (prefix) candidates.push(join(prefix, 'lib', 'libomp.dylib'))
  }

  candidates.push(
    '/opt/homebrew/opt/libomp/lib/libomp.dylib',
    '/usr/local/opt/libomp/lib/libomp.dylib',
    '/opt/local/lib/libomp/libomp.dylib',
  )

  return candidates.find((candidate) => existsSync(candidate))
}

function signMacBinary(path) {
  if (process.platform !== 'darwin') return
  const result = spawnSync('codesign', ['--force', '--sign', '-', path], {
    encoding: 'utf8',
    windowsHide: true,
  })
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`codesign failed for ${path} with exit ${result.status}:\n${result.stderr || result.stdout}`)
  }
}

function pythonPackageFile(packageName, relativePath) {
  const code = [
    'import importlib.util',
    'import pathlib',
    'import sys',
    `spec = importlib.util.find_spec(${JSON.stringify(packageName)})`,
    'if spec is None or spec.origin is None:',
    '    sys.exit(1)',
    `path = pathlib.Path(spec.origin).parent / ${JSON.stringify(relativePath)}`,
    'if not path.exists():',
    '    sys.exit(2)',
    'print(path)',
  ].join('\n')
  const result = spawnSync(
    'uv',
    ['run', '--extra', 'recommended', 'python', '-c', code],
    {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8:replace',
      },
      encoding: 'utf8',
      windowsHide: true,
    },
  )
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(
      `Could not locate ${packageName}/${relativePath} for the desktop gateway bundle.\n${result.stderr || result.stdout}`,
    )
  }

  const path = result.stdout.trim().split(/\r?\n/).filter(Boolean).at(-1)
  if (!path || !existsSync(path)) {
    throw new Error(`Resolved ${packageName}/${relativePath} to an invalid path: ${path || '<empty>'}`)
  }
  return path
}

function addBinaryArg(sourcePath, destinationDir) {
  return ['--add-binary', `${sourcePath}${addDataSeparator}${destinationDir}`]
}

function platformLightgbmLibraryPath() {
  if (process.platform === 'win32') return join('bin', 'lib_lightgbm.dll')
  if (process.platform === 'darwin') return join('lib', 'lib_lightgbm.dylib')
  return join('lib', 'lib_lightgbm.so')
}

function platformLightgbmBundleDir() {
  return process.platform === 'win32' ? 'lightgbm/bin' : 'lightgbm/lib'
}

function readFileHead(path, bytes = 96) {
  return readFileSync(path).subarray(0, bytes).toString('utf8')
}

function assertRouterAssetsReady() {
  const manifestPath = join(routerBundleDir, 'artifact_manifest.json')
  if (!existsSync(manifestPath)) {
    throw new Error(`Router artifact manifest not found at ${manifestPath}.`)
  }

  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  const problems = []
  for (const file of manifest.files || []) {
    const relPath = String(file.path || '')
    if (!relPath) continue
    const path = join(routerBundleDir, relPath)
    if (!existsSync(path)) {
      problems.push(`${relPath}: missing`)
      continue
    }
    const actualSize = statSync(path).size
    if (typeof file.size_bytes === 'number' && actualSize !== file.size_bytes) {
      problems.push(`${relPath}: size ${actualSize} != manifest ${file.size_bytes}`)
      continue
    }
    if (readFileHead(path).startsWith(gitLfsPointerHeader)) {
      problems.push(`${relPath}: Git LFS pointer file, not the real router artifact`)
    }
  }

  if (problems.length > 0) {
    throw new Error(
      [
        'Router V4 Phase 3 assets are incomplete; refusing to build a desktop gateway that degrades routing.',
        'Run `git lfs pull --include="src/opensquilla/squilla_router/models/v4.2_phase3_inference/**"` and rebuild.',
        ...problems.map((problem) => `- ${problem}`),
      ].join('\n'),
    )
  }
}

function assertControlUiArtifactReady() {
  if (!existsSync(join(controlUiDistDir, 'index.html'))) {
    throw new Error(
      `Built Control UI not found at ${controlUiDistDir}. Run npm run build:web before npm run build:gateway.`,
    )
  }
  const result = spawnSync(process.execPath, [controlUiVerifier, controlUiDistDir], {
    encoding: 'utf8',
    windowsHide: true,
  })
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(
      `Built Control UI failed artifact verification. Re-run npm run build:web.\n${result.stderr || result.stdout}`,
    )
  }
}

function patchMacLightgbmRuntime() {
  if (process.platform !== 'darwin') return

  const lightgbmLibs = findFilesByName(runtimeGatewayDir, 'lib_lightgbm.dylib')
  if (lightgbmLibs.length === 0) {
    throw new Error('LightGBM was requested for the desktop gateway, but lib_lightgbm.dylib was not bundled.')
  }

  const libomp = findMacLibomp()
  if (!libomp) {
    throw new Error(
      'macOS LightGBM runtime requires libomp.dylib. Ensure scikit-learn is bundled, or install libomp on the build host, for example `brew install libomp`, then rebuild the desktop gateway.',
    )
  }

  for (const lightgbmLib of lightgbmLibs) {
    const bundledLibomp = join(dirname(lightgbmLib), 'libomp.dylib')
    copyFileSync(libomp, bundledLibomp)
    signMacBinary(bundledLibomp)

    const result = spawnSync(
      'install_name_tool',
      ['-change', '@rpath/libomp.dylib', '@loader_path/libomp.dylib', lightgbmLib],
      { encoding: 'utf8', windowsHide: true },
    )
    if (result.error) throw result.error
    if (result.status !== 0) {
      throw new Error(
        `install_name_tool failed for ${lightgbmLib} with exit ${result.status}:\n${result.stderr || result.stdout}`,
      )
    }
    signMacBinary(lightgbmLib)
  }
}

assertControlUiArtifactReady()
assertRouterAssetsReady()

rmSync(runtimeGatewayDir, { recursive: true, force: true })
mkdirSync(runtimeGatewayDir, { recursive: true })
mkdirSync(pyinstallerWorkDir, { recursive: true })

const lightgbmBinaryArgs = addBinaryArg(
  pythonPackageFile('lightgbm', platformLightgbmLibraryPath()),
  platformLightgbmBundleDir(),
)
const macOpenMpBinaryArgs = process.platform === 'darwin'
  ? addBinaryArg(pythonPackageFile('sklearn', join('.dylibs', 'libomp.dylib')), '.')
  : []

const args = [
  'run',
  '--extra',
  'recommended',
  '--extra',
  'mcp',
  '--extra',
  'msg',
  '--extra',
  'matrix',
  '--extra',
  'document-extras',
  '--with',
  'pyinstaller',
  'pyinstaller',
  '--noconfirm',
  '--clean',
  '--onedir',
  '--name',
  'opensquilla-gateway',
  '--distpath',
  runtimeGatewayDir,
  '--workpath',
  pyinstallerWorkDir,
  '--specpath',
  pyinstallerWorkDir,
  '--collect-all',
  'opensquilla',
  '--collect-all',
  'sqlite_vec',
  '--collect-data',
  'certifi',
  '--hidden-import',
  'certifi',
  '--collect-binaries',
  'sklearn',
  '--copy-metadata',
  'opensquilla',
  '--copy-metadata',
  'scikit-learn',
  '--copy-metadata',
  'lightgbm',
  '--copy-metadata',
  'yoyo-migrations',
  '--hidden-import',
  'joblib',
  '--hidden-import',
  'sklearn',
  '--hidden-import',
  'sklearn.feature_extraction.text',
  '--hidden-import',
  'sklearn.decomposition._truncated_svd',
  '--hidden-import',
  'sklearn.decomposition._pca',
  '--hidden-import',
  'sklearn.preprocessing._data',
  '--hidden-import',
  'lightgbm',
  '--hidden-import',
  'tokenizers',
  '--hidden-import',
  'tiktoken',
  '--hidden-import',
  'onnxruntime',
  '--hidden-import',
  'mcp',
  '--hidden-import',
  'yoyo.backends.core.sqlite3',
  '--runtime-hook',
  caRuntimeHookPath,
  '--add-data',
  `${join(repoRoot, 'migrations')}${addDataSeparator}opensquilla/_migrations`,
  '--add-data',
  `${controlUiDistDir}${addDataSeparator}opensquilla/gateway/static/dist`,
  ...lightgbmBinaryArgs,
  ...macOpenMpBinaryArgs,
  entryPath,
]

const result = spawnSync('uv', args, {
  cwd: repoRoot,
  env: {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8:replace',
  },
  stdio: 'inherit',
})

if (result.error) {
  throw result.error
}
if (result.status !== 0) {
  process.exit(result.status ?? 1)
}

patchMacLightgbmRuntime()
