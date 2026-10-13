import { createHash } from 'node:crypto'
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptDirectory = dirname(fileURLToPath(import.meta.url))
const webuiRoot = resolve(scriptDirectory, '..')
export const officeFontDirectory = resolve(webuiRoot, 'node_modules/.cache/office-preview-fonts')

export function verifyFontBytes(bytes, font) {
  if (bytes.length !== font.bytes) throw new Error(`${font.name}: expected ${font.bytes} bytes, received ${bytes.length}`)
  const gitBlobSha1 = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex')
  if (gitBlobSha1 !== font.gitBlobSha1) throw new Error(`${font.name}: pinned Git blob hash mismatch`)
  const sha256 = createHash('sha256').update(bytes).digest('hex')
  if (font.sha256 && sha256 !== font.sha256) throw new Error(`${font.name}: SHA-256 mismatch`)
  return sha256
}

export async function prepareOfficePreviewAssets({ offline = false } = {}) {
  const manifest = JSON.parse(await readFile(resolve(scriptDirectory, 'office-preview-fonts.json'), 'utf8'))
  await mkdir(officeFontDirectory, { recursive: true })
  const records = []
  for (const font of manifest.fonts) {
    const destination = resolve(officeFontDirectory, font.name)
    let bytes
    try {
      bytes = await readFile(destination)
      verifyFontBytes(bytes, font)
    } catch (error) {
      if (offline) throw new Error(`Office preview font cache is missing or invalid: ${font.name}. Run npm run prepare:office-assets once with network access.`, { cause: error })
      const url = `https://raw.githubusercontent.com/notofonts/noto-cjk/${manifest.sourceRevision}/${font.path}`
      const response = await fetch(url, { signal: AbortSignal.timeout(120_000) })
      if (!response.ok) throw new Error(`${font.name}: download failed (${response.status})`)
      // Reject unexpected response sizes before consuming an unbounded payload.
      const declaredLength = response.headers.get('content-length')
      if (declaredLength && Number(declaredLength) !== font.bytes) throw new Error(`${font.name}: unexpected Content-Length`)
      const parts = []
      let length = 0
      for await (const part of response.body) {
        length += part.length
        if (length > font.bytes) throw new Error(`${font.name}: download exceeds pinned size`)
        parts.push(part)
      }
      bytes = Buffer.concat(parts)
      verifyFontBytes(bytes, font)
      const temporary = `${destination}.${process.pid}.tmp`
      try {
        await writeFile(temporary, bytes)
        await rename(temporary, destination)
      } finally {
        await rm(temporary, { force: true })
      }
    }
    records.push({ name: font.name, bytes: bytes.length, sha256: verifyFontBytes(bytes, font) })
  }
  console.log(`Office preview fonts verified: ${records.length} files, ${records.reduce((total, font) => total + font.bytes, 0)} bytes; runtime is offline.`)
  return records
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try { await prepareOfficePreviewAssets({ offline: process.argv.includes('--offline') }) }
  catch (error) { console.error(error instanceof Error ? error.message : error); process.exitCode = 1 }
}
