import {
  closeSync,
  lstatSync,
  openSync,
  readSync,
  type Stats,
} from 'node:fs'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'

const COMMIT_ID_RE = /^[0-9a-f]{40}$/
const SAFE_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}$/
const MAX_METADATA_BYTES = 4 * 1024
const MAX_PACKED_REFS_BYTES = 2 * 1024 * 1024
const SOURCE_VERSION_SEPARATOR = '+source.'

/**
 * Read only the bounded Git metadata needed to identify a source checkout.
 *
 * This intentionally does not invoke Git (or inspect a branch, remote, path,
 * or working-tree state).  Electron calls it only for an unpackaged process
 * whose repository root has already been selected by the desktop launcher.
 */
export function readSourceCommitId(repoRoot: string): string | null {
  try {
    const root = resolve(repoRoot)
    if (!isRealDirectory(root)) return null
    // Do not treat an arbitrary Git repository supplied through the override as
    // an OpenSquilla source checkout.
    if (!isRegularFile(join(root, 'pyproject.toml'))) return null
    if (!isRealDirectory(join(root, 'src', 'opensquilla'))) return null

    const gitDirectory = resolveGitDirectory(join(root, '.git'))
    if (gitDirectory === null) return null
    const head = readBoundedText(join(gitDirectory, 'HEAD')).trim()
    const detachedCommit = normalizeCommitId(head)
    if (detachedCommit !== null) return detachedCommit
    if (!head.startsWith('ref: ')) return null

    const ref = head.slice('ref: '.length).trim()
    const refsRoot = resolveRefsRoot(gitDirectory)
    const refPath = safeRefPath(refsRoot, ref)
    if (refPath === null) return null

    try {
      const looseCommit = normalizeCommitId(readBoundedText(refPath).trim())
      if (looseCommit !== null) return looseCommit
    } catch {
      // A packed ref is the normal fallback after Git has packed references.
    }
    return readPackedRef(join(refsRoot, 'packed-refs'), ref)
  } catch {
    // Telemetry identity is best effort and must never affect Desktop startup.
    return null
  }
}

/**
 * Add a source commit to the existing application version without changing
 * the wire schema.  Invalid or oversized values deliberately fall back to the
 * ordinary version used by official packages.
 */
export function sourceTelemetryVersion(
  baseVersion: string,
  sourceCommitId: string | null,
): string {
  if (!SAFE_VERSION_RE.test(baseVersion) || !isCommitId(sourceCommitId)) {
    return baseVersion
  }
  const candidate = `${baseVersion}${SOURCE_VERSION_SEPARATOR}${sourceCommitId}`
  return candidate.length <= 64 ? candidate : baseVersion
}

function isCommitId(value: string | null): value is string {
  return typeof value === 'string' && COMMIT_ID_RE.test(value)
}

function normalizeCommitId(value: string): string | null {
  return COMMIT_ID_RE.test(value) ? value : null
}

function isRealDirectory(path: string): boolean {
  const metadata = lstat(path)
  return metadata !== null && metadata.isDirectory() && !metadata.isSymbolicLink()
}

function isRegularFile(path: string): boolean {
  const metadata = lstat(path)
  return metadata !== null && metadata.isFile() && !metadata.isSymbolicLink()
}

function lstat(path: string): Stats | null {
  try {
    return lstatSync(path)
  } catch {
    return null
  }
}

function resolveGitDirectory(markerPath: string): string | null {
  const metadata = lstat(markerPath)
  if (metadata === null || metadata.isSymbolicLink()) return null
  if (metadata.isDirectory()) return markerPath
  if (!metadata.isFile()) return null

  const marker = readBoundedText(markerPath).trim()
  if (!marker.startsWith('gitdir: ')) return null
  const rawPath = marker.slice('gitdir: '.length).trim()
  if (!rawPath) return null
  const gitDirectory = isAbsolute(rawPath)
    ? resolve(rawPath)
    : resolve(dirname(markerPath), rawPath)
  return isRealDirectory(gitDirectory) ? gitDirectory : null
}

function resolveRefsRoot(gitDirectory: string): string {
  try {
    const rawPath = readBoundedText(join(gitDirectory, 'commondir')).trim()
    if (!rawPath) return gitDirectory
    const commonDirectory = isAbsolute(rawPath)
      ? resolve(rawPath)
      : resolve(gitDirectory, rawPath)
    return isRealDirectory(commonDirectory) ? commonDirectory : gitDirectory
  } catch {
    return gitDirectory
  }
}

function safeRefPath(refsRoot: string, ref: string): string | null {
  if (!ref.startsWith('refs/') || ref.includes('\\')) return null
  const parts = ref.split('/')
  if (parts.some(part => part.length === 0 || part === '.' || part === '..')) return null
  const candidate = resolve(refsRoot, ...parts)
  const escaped = relative(resolve(refsRoot), candidate)
  if (escaped === '..' || escaped.startsWith(`..${sep}`) || isAbsolute(escaped)) {
    return null
  }
  return candidate
}

function readPackedRef(path: string, ref: string): string | null {
  let contents: string
  try {
    contents = readBoundedText(path, MAX_PACKED_REFS_BYTES)
  } catch {
    return null
  }
  for (const line of contents.split(/\r?\n/)) {
    if (!line || line.startsWith('#') || line.startsWith('^')) continue
    const separator = line.indexOf(' ')
    if (separator <= 0 || line.slice(separator + 1) !== ref) continue
    return normalizeCommitId(line.slice(0, separator))
  }
  return null
}

function readBoundedText(path: string, maximumBytes = MAX_METADATA_BYTES): string {
  if (!isRegularFile(path)) throw new Error('source metadata is not a regular file')
  const descriptor = openSync(path, 'r')
  try {
    const chunks: Buffer[] = []
    let totalBytes = 0
    // Read at most one byte beyond the contract so an oversized file is
    // rejected without ever loading its full contents into memory.
    while (totalBytes <= maximumBytes) {
      const remaining = maximumBytes + 1 - totalBytes
      const chunk = Buffer.alloc(Math.min(4 * 1024, remaining))
      const bytesRead = readSync(descriptor, chunk, 0, chunk.byteLength, null)
      if (bytesRead === 0) break
      chunks.push(chunk.subarray(0, bytesRead))
      totalBytes += bytesRead
    }
    if (totalBytes > maximumBytes) {
      throw new Error('source metadata exceeds its read limit')
    }
    return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))
  } finally {
    closeSync(descriptor)
  }
}
