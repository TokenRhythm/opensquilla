import { lstat, open, realpath } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { redactRendererLogText } from '../../../dist/desktop-renderer-log.js'

const MAX_LOG_BYTES = 64 * 1024
const MAX_LINE_CHARS = 8 * 1024 // The existing redactor's complete scan bound.
const samePath = (left, right) => process.platform === 'win32'
  ? resolve(left).toLowerCase() === resolve(right).toLowerCase()
  : resolve(left) === resolve(right)

function unsafePath() {
  return Object.assign(new Error('Diagnostic log path is not a regular synthetic-profile file'), { code: 'UNSAFE_PATH' })
}

export function sameLogFileIdentity(before, opened, platform = process.platform) {
  // Node 22 on Windows reports dev=0 for path stat/lstat while fstat returns
  // the volume ID. Only that unknown-device value is compatible; inode and
  // every available device identity must still match.
  return before.ino === opened.ino && (before.dev === opened.dev
    || (platform === 'win32' && before.dev === 0))
}

async function canonicalDirectory(path) {
  const info = await lstat(path)
  if (!info.isDirectory() || info.isSymbolicLink() || !samePath(await realpath(path), path)) throw unsafePath()
}

async function logTail(path, userDataDir) {
  await canonicalDirectory(dirname(path))
  const before = await lstat(path)
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1
    || !samePath(await realpath(path), path)) throw unsafePath()
  const handle = await open(path, 'r')
  try {
    const info = await handle.stat()
    if (!info.isFile() || !sameLogFileIdentity(before, info) || info.nlink !== 1) throw unsafePath()
    const start = Math.max(0, info.size - MAX_LOG_BYTES)
    const buffer = Buffer.alloc(Math.min(info.size, MAX_LOG_BYTES))
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, start)
    let text = buffer.subarray(0, bytesRead).toString('utf8')
    // Never expose a credential suffix from a cut leading line, or a partially
    // written final line. Gateway log records are newline terminated.
    const leadingPartialLine = start > 0
    if (leadingPartialLine) text = text.includes('\n') ? text.slice(text.indexOf('\n') + 1) : ''
    const trailingPartialLine = text.length > 0 && !text.endsWith('\n')
    if (trailingPartialLine) text = text.slice(0, text.lastIndexOf('\n') + 1)
    let oversizedLines = 0
    const lines = text.split('\n').filter(Boolean).map(line => {
      if (line.length > MAX_LINE_CHARS) {
        oversizedLines += 1
        return '[oversized diagnostic line omitted]'
      }
      return redactRendererLogText(line, userDataDir)
    })
    // Redaction can expand short values; cap the final payload as well as reads.
    const kept = []
    let outputBytes = 0
    let outputTruncated = false
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const next = lines[index] + '\n'
      const bytes = Buffer.byteLength(next)
      if (bytes + outputBytes > MAX_LOG_BYTES) {
        outputTruncated = true
        break
      }
      kept.push(next)
      outputBytes += bytes
    }
    return { sourceBytes: info.size, capturedBytes: bytesRead, truncated: start > 0 || outputTruncated,
      leadingPartialLine, trailingPartialLine, oversizedLines, tail: kept.reverse().join('') }
  } finally {
    await handle.close()
  }
}

export async function collectRetainedGatewayDiagnostics(plan) {
  const sources = [
    ['gateway.log', join(plan.userDataDir, 'logs', 'gateway.log')],
    ['debug.log', join(plan.profile, 'logs', 'debug.log')],
    ['debug.log.1', join(plan.profile, 'logs', 'debug.log.1')],
  ]
  return Promise.all(sources.map(async ([name, path]) => {
    try {
      if (!samePath(plan.profile, join(plan.userDataDir, 'opensquilla'))) throw unsafePath()
      await canonicalDirectory(plan.userDataDir)
      await canonicalDirectory(plan.profile)
      return { name, ...await logTail(path, plan.userDataDir) }
    } catch (error) {
      // Error messages may include paths or payloads; expose only a safe code.
      return { name, unavailable: true, code: /^[A-Z_0-9]+$/.test(error?.code || '') ? error.code : 'DIAGNOSTIC_ERROR' }
    }
  }))
}
