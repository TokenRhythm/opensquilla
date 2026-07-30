import { createHash, randomUUID } from 'node:crypto'
import { mkdir, open, rename, rm } from 'node:fs/promises'
import { dirname } from 'node:path'
import { UpdateChannelError } from './update-channel.js'

const SHA256_HEX = /^[0-9a-f]{64}$/i
const SHA256SUM_LINE = /^([0-9a-f]{64})[ \t]+\*?([^\r\n]+)$/i

export interface VerifiedDownloadResult {
  path: string
  bytes: number
  sha256: string
}

export interface VerifiedDownloadOptions {
  maxBytes: number
  onProgress?: (receivedBytes: number, totalBytes: number | null) => void
}

function integrityFailure(message: string): never {
  throw new UpdateChannelError('integrity_failed', message)
}

export function parseSha256SumsForAsset(contents: string, asset: string): string {
  if (!asset || asset === '.' || asset === '..' || asset.includes('/') || asset.includes('\\')) {
    return integrityFailure('The checksum target must be one canonical asset filename.')
  }

  let matched: string | null = null
  for (const rawLine of String(contents ?? '').split(/\r?\n/)) {
    if (!rawLine.trim()) continue
    const parsed = SHA256SUM_LINE.exec(rawLine)
    if (!parsed) return integrityFailure('The canonical SHA256SUMS file is malformed.')
    const [, digest, filename] = parsed
    if (filename !== asset) continue
    if (matched !== null) {
      return integrityFailure(`The canonical SHA256SUMS file lists ${asset} more than once.`)
    }
    matched = digest.toLowerCase()
  }

  if (matched === null) {
    return integrityFailure(`The canonical SHA256SUMS file does not list ${asset}.`)
  }
  return matched
}

export async function readResponseTextWithLimit(response: Response, maxBytes: number): Promise<string> {
  if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    return integrityFailure('The checksum response size limit is invalid.')
  }
  const contentLength = response.headers.get('content-length')
  const declaredLength = contentLength === null ? null : Number(contentLength)
  if (declaredLength !== null && Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    return integrityFailure('The canonical SHA256SUMS response is unexpectedly large.')
  }
  if (!response.body) return ''

  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      received += value.byteLength
      if (received > maxBytes) {
        await reader.cancel().catch(() => {})
        return integrityFailure('The canonical SHA256SUMS response is unexpectedly large.')
      }
      chunks.push(value)
    }
  } finally {
    reader.releaseLock()
  }
  return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk))).toString('utf8')
}

export async function streamResponseToVerifiedFile(
  response: Response,
  destinationPath: string,
  expectedSha256: string,
  options: VerifiedDownloadOptions,
): Promise<VerifiedDownloadResult> {
  const expected = String(expectedSha256 ?? '').trim().toLowerCase()
  if (!SHA256_HEX.test(expected)) return integrityFailure('The expected installer SHA256 is invalid.')
  if (!response.body) {
    throw new UpdateChannelError('download_failed', 'The installer response has no body.')
  }
  if (!Number.isSafeInteger(options.maxBytes) || options.maxBytes <= 0) {
    throw new UpdateChannelError('download_failed', 'The installer size limit is invalid.')
  }

  const contentLength = response.headers.get('content-length')
  const declaredLength = contentLength === null ? null : Number(contentLength)
  const totalBytes = declaredLength !== null && Number.isSafeInteger(declaredLength) && declaredLength >= 0
    ? declaredLength
    : null
  if (totalBytes !== null && totalBytes > options.maxBytes) {
    throw new UpdateChannelError('download_failed', 'The installer is larger than the allowed download size.')
  }

  await mkdir(dirname(destinationPath), { recursive: true, mode: 0o700 })
  const temporaryPath = `${destinationPath}.${randomUUID()}.part`
  const handle = await open(temporaryPath, 'wx', 0o600)
  const reader = response.body.getReader()
  const digest = createHash('sha256')
  let received = 0
  try {
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        received += value.byteLength
        if (received > options.maxBytes) {
          await reader.cancel().catch(() => {})
          throw new UpdateChannelError('download_failed', 'The installer exceeded the allowed download size.')
        }
        digest.update(value)
        let written = 0
        while (written < value.byteLength) {
          const result = await handle.write(value, written, value.byteLength - written, null)
          if (result.bytesWritten <= 0) {
            throw new UpdateChannelError('download_failed', 'The installer could not be written completely.')
          }
          written += result.bytesWritten
        }
        options.onProgress?.(received, totalBytes)
      }
      if (totalBytes !== null && received !== totalBytes) {
        throw new UpdateChannelError(
          'download_failed',
          `The installer response was truncated (${received} of ${totalBytes} bytes).`,
        )
      }
      await handle.sync()
    } catch (err) {
      await reader.cancel().catch(() => {})
      throw err
    } finally {
      reader.releaseLock()
      await handle.close().catch(() => {})
    }
  } catch (err) {
    await rm(temporaryPath, { force: true }).catch(() => {})
    throw err
  }

  const actual = digest.digest('hex')
  if (actual !== expected) {
    await rm(temporaryPath, { force: true }).catch(() => {})
    return integrityFailure(`The installer SHA256 did not match the canonical GitHub checksum.`)
  }

  try {
    // Do not remove any previously verified file until the replacement itself
    // has passed hashing. Windows rename cannot replace an existing destination.
    await rm(destinationPath, { force: true })
    await rename(temporaryPath, destinationPath)
  } catch (err) {
    await rm(temporaryPath, { force: true }).catch(() => {})
    throw new UpdateChannelError(
      'download_failed',
      `The verified installer could not be finalized: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
  return { path: destinationPath, bytes: received, sha256: actual }
}
