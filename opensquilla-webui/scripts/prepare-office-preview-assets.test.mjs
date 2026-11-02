import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { downloadFontBytes, verifyFontBytes } from './prepare-office-preview-assets.mjs'

const downloadBytes = Buffer.from('downloaded font fixture')
const downloadFont = {
  name: 'fixture.otf', bytes: downloadBytes.length,
  gitBlobSha1: createHash('sha1').update(`blob ${downloadBytes.length}\0`).update(downloadBytes).digest('hex'),
}
const downloadUrl = 'https://fonts.invalid/fixture.otf'

test('font download recovers from a timeout and transient HTTP failure', async () => {
  let calls = 0
  const messages = []
  const waits = []
  const bytes = await downloadFontBytes(downloadFont, downloadUrl, {
    fetchImpl: async () => {
      calls++
      if (calls === 1) throw new DOMException('The operation was aborted due to timeout', 'TimeoutError')
      if (calls === 2) return new Response('unavailable', { status: 503 })
      return new Response(downloadBytes)
    },
    log: message => messages.push(message), wait: async ms => waits.push(ms),
  })
  assert.deepEqual(bytes, downloadBytes)
  assert.equal(calls, 3)
  assert.deepEqual(waits, [1000, 2000])
  assert.ok(messages.some(message => message.includes('timed out after 600s')))
})

test('exhausted network retries report the font, URL and recovery command', async () => {
  let calls = 0
  await assert.rejects(downloadFontBytes(downloadFont, downloadUrl, {
    fetchImpl: async () => { calls++; throw new TypeError('fetch failed') },
    log: () => {}, wait: async () => {},
  }), error => {
    assert.match(error.message, /fixture\.otf: fetch failed/)
    assert.ok(error.message.includes(downloadUrl))
    assert.match(error.message, /npm run prepare:office-assets/)
    return true
  })
  assert.equal(calls, 3)
})

test('permanent HTTP errors and corrupt fonts fail without retrying', async () => {
  for (const response of [new Response('missing', { status: 404 }), new Response(Buffer.alloc(downloadBytes.length))]) {
    let calls = 0
    await assert.rejects(downloadFontBytes(downloadFont, downloadUrl, {
      fetchImpl: async () => { calls++; return response }, log: () => {},
      wait: async () => assert.fail('must not retry'),
    }), /HTTP 404|hash mismatch/)
    assert.equal(calls, 1)
  }
})

test('font cache rejects corrupt or truncated bytes before packaging', () => {
  const bytes = Buffer.from('font integrity fixture')
  const font = {
    name: 'fixture.otf', bytes: bytes.length,
    gitBlobSha1: createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex'),
    sha256: createHash('sha256').update(bytes).digest('hex'),
  }
  assert.equal(verifyFontBytes(bytes, font), font.sha256)
  assert.throws(() => verifyFontBytes(bytes.subarray(1), font), /expected/)
  const corrupted = Buffer.from(bytes)
  corrupted[0] ^= 1
  assert.throws(() => verifyFontBytes(corrupted, font), /hash mismatch/)
  assert.throws(() => verifyFontBytes(bytes, { ...font, sha256: '0'.repeat(64) }), /SHA-256/)
})

test('fonts and Silurus are pinned and shipped notices retain attribution', () => {
  const manifest = JSON.parse(readFileSync(new URL('./office-preview-fonts.json', import.meta.url), 'utf8'))
  assert.match(manifest.sourceRevision, /^[a-f0-9]{40}$/)
  assert.equal(manifest.fonts.length, 4)
  for (const font of manifest.fonts) {
    assert.match(font.name, /^Noto(Sans|Serif)SC-(Regular|Bold)\.otf$/)
    assert.match(font.gitBlobSha1, /^[a-f0-9]{40}$/)
    assert.match(font.sha256, /^[a-f0-9]{64}$/)
  }
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(pkg.dependencies['@silurus/ooxml'], '0.88.0')
  const notices = new URL('../public-assets/assets/office-preview-licenses/', import.meta.url)
  assert.match(readFileSync(new URL('Silurus-LICENSE.txt', notices), 'utf8'), /Yuki Yokotani/)
  assert.match(readFileSync(new URL('Silurus-THIRD_PARTY_NOTICES.md', notices), 'utf8'), /Unicode License v3/)
  for (const family of ['Sans', 'Serif']) {
    assert.match(readFileSync(new URL(`Noto${family}-LICENSE.txt`, notices), 'utf8'), /SIL OPEN FONT LICENSE Version 1\.1/)
  }
})
