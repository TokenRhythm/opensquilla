import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { ORIGINAL_SHA256, patchBootstrap, UPSTREAM_VERSION } from './patch-generative-frame.mjs'

const installed = await readFile(new URL('../node_modules/generative-frame/dist/bootstrap.js', import.meta.url), 'utf8')
const patched = patchBootstrap(installed, UPSTREAM_VERSION)

test('patches the locked runtime once and is byte-for-byte idempotent', () => {
  assert.equal(patchBootstrap(patched, UPSTREAM_VERSION), patched)
  const original = patched
    .replace('new Set([`script`,`noscript`,`template`,`style`])', 'new Set([`script`,`noscript`,`template`])')
    .replace('zoom:${r.devicePixelRatio||1};width:${a}px;height:${o}px;overflow:hidden;', 'width:${a}px;height:${o}px;overflow:hidden;')
    .replace('0 0 ${a*(r.devicePixelRatio||1)} ${o*(r.devicePixelRatio||1)}', '0 0 ${a} ${o}')
  assert.equal(createHash('sha256').update(original).digest('hex'), ORIGINAL_SHA256)
  assert.equal(patchBootstrap(original, UPSTREAM_VERSION), patched)
})

test('rejects a different version or any unknown upstream edit without writing', () => {
  assert.throws(() => patchBootstrap(installed, '0.0.6'), /Unsupported/)
  assert.throws(() => patchBootstrap(`${installed}\n`, UPSTREAM_VERSION), /Unknown/)
  assert.throws(() => patchBootstrap(installed.replace('buildBootstrapHtml', 'unknownBootstrap'), UPSTREAM_VERSION), /Unknown/)
})

test('keeps the public screenshot protocol and the sandbox bootstrap unchanged', () => {
  assert.match(patched, /genframe\/screenshot/)
  assert.match(patched, /Content-Security-Policy/)
  assert.match(patched, /new Set\(\[`script`,`noscript`,`template`,`style`\]\)/)
  assert.match(patched, /zoom:\$\{r\.devicePixelRatio\|\|1\}/)
})
