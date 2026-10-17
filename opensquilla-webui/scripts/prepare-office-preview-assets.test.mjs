import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { verifyFontBytes } from './prepare-office-preview-assets.mjs'

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
