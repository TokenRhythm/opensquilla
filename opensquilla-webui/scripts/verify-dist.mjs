import { createHash } from 'node:crypto'
import {
  existsSync,
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { dirname, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

export const MANIFEST_NAME = 'webui-artifact-manifest.json'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const webuiRoot = resolve(scriptDir, '..')
const defaultDistDir = resolve(
  scriptDir,
  '../../src/opensquilla/gateway/static/dist',
)
const sourceInputRoots = [
  '.node-version',
  '.env',
  '.env.local',
  '.env.production',
  '.env.production.local',
  'index.html',
  'package.json',
  'package-lock.json',
  'vite.config.ts',
  'tsconfig.json',
  'tsconfig.app.json',
  'tsconfig.node.json',
  'public',
  'scripts',
  'src',
]
const normalizedTextSuffixes = new Set([
  '.css',
  '.html',
  '.js',
  '.json',
  '.md',
  '.mjs',
  '.svg',
  '.ts',
  '.tsx',
  '.txt',
  '.vue',
  '.webmanifest',
  '.yaml',
  '.yml',
])
// Keep this platform-independent and mirrored in the Python verifier.
// Canonical build normalization removes this OS metadata before manifest
// generation, and source distributions omit it inside canonical source roots.
const ignoredSourceFileNames = new Set(['.DS_Store'])
const forbiddenArtifactFileNames = new Set(['.ds_store', '.npmrc'])
const forbiddenArtifactSuffixes = new Set(['.key', '.pem'])
const officialMusicFiles = new Set(['music/README.md', 'music/playlist.json'])

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

function toPosixPath(path) {
  return path.split(sep).join('/')
}

function compareUtf8(a, b) {
  return Buffer.compare(Buffer.from(a, 'utf8'), Buffer.from(b, 'utf8'))
}

function listFiles(root) {
  const files = []

  function walk(dir) {
    for (const entry of readdirSync(dir, { withFileTypes: true }).sort((a, b) =>
      compareUtf8(a.name, b.name),
    )) {
      const path = resolve(dir, entry.name)
      if (entry.isSymbolicLink() || lstatSync(path).isSymbolicLink()) {
        throw new Error(`Web UI artifact must not contain symlinks: ${path}`)
      }
      if (entry.isDirectory()) {
        walk(path)
      } else if (entry.isFile()) {
        files.push(path)
      }
    }
  }

  walk(root)
  return files
}

function recordsFor(root) {
  return listFiles(root)
    .map((path) => ({
      path: toPosixPath(relative(root, path)),
      size: statSync(path).size,
      sha256: sha256(path),
    }))
    .filter((record) => record.path !== MANIFEST_NAME)
    .sort((a, b) => compareUtf8(a.path, b.path))
}

function isForbiddenArtifactPath(path) {
  const name = path.split('/').at(-1)
  const lowered = name.toLowerCase()
  const dot = lowered.lastIndexOf('.')
  const suffix = dot >= 0 ? lowered.slice(dot) : ''
  return (
    forbiddenArtifactFileNames.has(lowered) ||
    lowered === '.env' ||
    lowered.startsWith('.env.') ||
    forbiddenArtifactSuffixes.has(suffix)
  )
}

function sourceFiles(rootDirectory = webuiRoot) {
  const sourceRoot = resolve(rootDirectory)
  const files = []
  for (const relativeRoot of sourceInputRoots) {
    const root = resolve(sourceRoot, relativeRoot)
    if (!existsSync(root)) continue
    if (lstatSync(root).isSymbolicLink()) {
      throw new Error(`Web UI build input must not be a symlink: ${root}`)
    }
    const stat = statSync(root)
    if (stat.isFile()) {
      files.push(root)
    } else if (stat.isDirectory()) {
      files.push(
        ...listFiles(root).filter(
          (path) => !ignoredSourceFileNames.has(path.split(sep).at(-1)),
        ),
      )
    }
  }
  return [...new Set(files)].sort((a, b) =>
    compareUtf8(toPosixPath(relative(sourceRoot, a)), toPosixPath(relative(sourceRoot, b))),
  )
}

export function sourceFingerprint(rootDirectory = webuiRoot) {
  const sourceRoot = resolve(rootDirectory)
  const hash = createHash('sha256')
  for (const path of sourceFiles(sourceRoot)) {
    const relativePath = toPosixPath(relative(sourceRoot, path))
    const suffix = relativePath.slice(relativePath.lastIndexOf('.')).toLowerCase()
    let content = readFileSync(path)
    if (
      relativePath === '.node-version' ||
      relativePath.startsWith('.env') ||
      normalizedTextSuffixes.has(suffix)
    ) {
      content = Buffer.from(content.toString('utf8').replace(/\r\n/g, '\n').replace(/\r/g, '\n'))
    }
    hash.update(relativePath)
    hash.update('\0')
    hash.update(content)
    hash.update('\0')
  }
  return hash.digest('hex')
}

function referencedEntryAssets(indexHtml) {
  const references = [...indexHtml.matchAll(/\b(?:src|href)="([^"]+)"/g)]
    .map((match) => match[1])
    .filter((value) => !value.startsWith('data:'))
    .filter((value) => !value.startsWith('http://'))
    .filter((value) => !value.startsWith('https://'))
    .filter((value) => !value.startsWith('//'))
    .filter((value) => !value.startsWith('#'))
    .map((value) => value.split(/[?#]/, 1)[0])
    .map((value) => value.replace(/^\.\//, ''))
    .filter(Boolean)

  for (const value of references) {
    if (value.startsWith('/') || value.split('/').includes('..')) {
      throw new Error(`Web UI entry asset must stay inside dist: ${value}`)
    }
  }
  return references
}

export function writeManifest(distDir = defaultDistDir) {
  const root = resolve(distDir)
  if (!existsSync(resolve(root, 'index.html'))) {
    throw new Error(`Built Web UI entrypoint is missing: ${resolve(root, 'index.html')}`)
  }
  const manifest = {
    schemaVersion: 1,
    sourceFingerprint: sourceFingerprint(),
    files: recordsFor(root),
  }
  writeFileSync(
    resolve(root, MANIFEST_NAME),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  )
  return manifest
}

export function verifyDist(
  distDir = defaultDistDir,
  { forbidPersonalBgm = false } = {},
) {
  const root = resolve(distDir)
  const indexPath = resolve(root, 'index.html')
  const manifestPath = resolve(root, MANIFEST_NAME)
  if (!existsSync(indexPath)) {
    throw new Error(`Built Web UI entrypoint is missing: ${indexPath}`)
  }
  if (!existsSync(manifestPath)) {
    throw new Error(`Web UI artifact manifest is missing: ${manifestPath}`)
  }

  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
  if (
    manifest.schemaVersion !== 1 ||
    typeof manifest.sourceFingerprint !== 'string' ||
    !Array.isArray(manifest.files)
  ) {
    throw new Error(`Unsupported Web UI artifact manifest: ${manifestPath}`)
  }
  const currentSourceFingerprint = sourceFingerprint()
  if (manifest.sourceFingerprint !== currentSourceFingerprint) {
    throw new Error(
      'Web UI artifact is stale for the current frontend source. Rebuild it with `npm run build`.',
    )
  }

  const expected = JSON.stringify(manifest.files)
  const actualRecords = recordsFor(root)
  const forbidden = actualRecords
    .map((record) => record.path)
    .filter(isForbiddenArtifactPath)
  if (forbidden.length > 0) {
    throw new Error(
      `Web UI artifact contains forbidden metadata or sensitive files: ${forbidden.join(', ')}`,
    )
  }
  const actual = JSON.stringify(actualRecords)
  if (actual !== expected) {
    throw new Error(
      'Web UI artifact does not match its manifest. Rebuild it with `npm run build`.',
    )
  }
  if (forbidPersonalBgm) {
    const personalBgm = actualRecords
      .map((record) => record.path)
      .filter((path) => path.startsWith('music/') && !officialMusicFiles.has(path))
    if (personalBgm.length > 0) {
      throw new Error(
        `Personal BGM content is forbidden in official Web UI artifacts: ${personalBgm.join(', ')}`,
      )
    }
    const playlistPath = resolve(root, 'music/playlist.json')
    if (existsSync(playlistPath)) {
      let playlist
      try {
        playlist = JSON.parse(readFileSync(playlistPath, 'utf8'))
      } catch (error) {
        throw new Error(
          `Official music/playlist.json is invalid: ${error instanceof Error ? error.message : error}`,
        )
      }
      if (
        playlist === null ||
        typeof playlist !== 'object' ||
        Array.isArray(playlist) ||
        !Array.isArray(playlist.tracks) ||
        playlist.tracks.length !== 0
      ) {
        throw new Error(
          'Official music/playlist.json must keep its tracks list empty; use playlist.local.json only for private builds.',
        )
      }
    }
  }

  const indexHtml = readFileSync(indexPath, 'utf8')
  const references = referencedEntryAssets(indexHtml)
  if (!references.some((path) => path.endsWith('.js'))) {
    throw new Error('Web UI index.html does not reference an entry JavaScript module.')
  }
  if (!references.some((path) => path.endsWith('.css'))) {
    throw new Error('Web UI index.html does not reference an entry stylesheet.')
  }
  for (const asset of references) {
    if (!existsSync(resolve(root, asset))) {
      throw new Error(`Web UI index.html references a missing asset: ${asset}`)
    }
  }

  return manifest
}

function main(argv) {
  const args = []
  let write = false
  let forbidPersonalBgm = false
  for (const arg of argv) {
    if (arg === '--write') write = true
    else if (arg === '--forbid-personal-bgm') forbidPersonalBgm = true
    else args.push(arg)
  }
  if (args.length > 1) {
    throw new Error(
      'usage: node verify-dist.mjs [--write] [--forbid-personal-bgm] [dist-directory]',
    )
  }
  const distDir = args[0] ? resolve(args[0]) : defaultDistDir
  if (write) writeManifest(distDir)
  const manifest = verifyDist(distDir, { forbidPersonalBgm })
  console.log(
    `Web UI artifact verified: ${manifest.files.length} files in ${distDir}`,
  )
}

if (
  process.argv[1] &&
  existsSync(process.argv[1]) &&
  realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
) {
  try {
    main(process.argv.slice(2))
  } catch (error) {
    console.error(`verify-dist: ${error instanceof Error ? error.message : error}`)
    process.exit(1)
  }
}
