import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import {
  buildGeneratedFiles, MODULES, normalizeText, replaceExactlyOnce, sha256,
  skillRoot, syncGuidance, UPSTREAM_HASHES, UPSTREAM_VERSION, verifyUpstream,
} from './sync-visualize-guidance.mjs'

async function isolatedDirectory(t) {
  const prefix = path.resolve(tmpdir(), 'opensquilla-guidance-')
  const root = await mkdtemp(prefix)
  t.after(async () => {
    assert.ok(path.resolve(root).startsWith(prefix), 'Cleanup must remain in the test-owned directory')
    await rm(root, { recursive: true, force: true })
  })
  return root
}

test('generated references are deterministic and current with the locked upstream package', async () => {
  const first = await buildGeneratedFiles()
  assert.deepEqual(await buildGeneratedFiles(), first)
  await syncGuidance({ files: first })
  const manifest = JSON.parse(first.get('upstream-guidance.json'))
  assert.deepEqual(manifest.modules, MODULES)
  assert.equal(manifest.upstream.version, UPSTREAM_VERSION)
  assert.deepEqual(manifest.upstream.inputs, UPSTREAM_HASHES)
  for (const { file, sha256TextLF } of manifest.outputs) {
    assert.equal(sha256(first.get(file)), sha256TextLF, file)
  }
  for (const record of manifest.adaptations) assert.equal(record.matches, 1, record.id)
})

test('check mode reports missing or edited content without modifying it', async t => {
  const root = await isolatedDirectory(t)
  const files = new Map([['references/example.txt', 'approved\n']])
  await assert.rejects(syncGuidance({ root, files }), /missing or stale/)
  await syncGuidance({ root, files, write: true })
  const file = path.join(root, 'references/example.txt')
  await writeFile(file, 'manual change\n')
  await assert.rejects(syncGuidance({ root, files }), /missing or stale/)
  assert.equal(await readFile(file, 'utf8'), 'manual change\n')
  await syncGuidance({ root, files, write: true })
  assert.equal(await readFile(file, 'utf8'), 'approved\n')
})

test('checked-in text remains verifiable after a Windows CRLF checkout', async t => {
  const root = await isolatedDirectory(t)
  const files = new Map([['example.txt', 'first\nsecond\n']])
  await writeFile(path.join(root, 'example.txt'), 'first\r\nsecond\r\n')
  await syncGuidance({ root, files })
})

test('adaptation refuses changed or duplicate upstream text rather than guessing', () => {
  assert.throws(() => replaceExactlyOnce('new wording', 'old wording', 'host contract', 'test'), /found 0/)
  assert.throws(() => replaceExactlyOnce('old old', 'old', 'host contract', 'test'), /found 2/)
})

test('unknown package version or same-version modified source fails closed', async t => {
  const root = await isolatedDirectory(t)
  const file = path.join(root, 'package.json')
  await writeFile(file, JSON.stringify({ version: '0.0.6', license: 'MIT' }))
  await assert.rejects(verifyUpstream(root), /Unsupported generative-frame version/)
  await writeFile(file, JSON.stringify({ version: UPSTREAM_VERSION, license: 'MIT' }))
  const { mkdir } = await import('node:fs/promises')
  await mkdir(path.join(root, 'src/prompts'), { recursive: true })
  await writeFile(path.join(root, 'src/prompts/guidance.ts'), '// same version, unreviewed input')
  await assert.rejects(verifyUpstream(root), /Unknown upstream guidance input/)
})

test('published guidance does not advertise unavailable tools, network examples or automatic chat', async () => {
  const files = await buildGeneratedFiles()
  const references = [...files].filter(([file]) => file.startsWith('references/'))
  assert.equal(references.length, MODULES.length + 1)
  for (const [file, text] of references) {
    assert.doesNotMatch(text, /`(?:show_widget|edit_widget|read_me|genframe\.(?:callTool|updateModelContext))`/, file)
    assert.doesNotMatch(text, /<script src=|no script CDN is allowed:|Offer follow-up questions through buttons/, file)
    assert.doesNotMatch(text, /After sending, disable the form|sends `text` as the user's next chat/, file)
  }
  const base = files.get('references/guidance-base.txt')
  assert.match(base, /Plain elements are already styled to match the host/)
  assert.match(base, /--chart-6/)
  assert.match(base, /44px/)
  assert.match(base, /16px text in inputs/)
  assert.match(base, /setContext\(null\)/)
  assert.match(files.get('references/guidance-interactive.txt'), /Keep parameter changes and results local/)
  assert.match(files.get('references/guidance-elicitation.txt'), /Keep fields editable/)
})

test('all generated files carry source attribution and the complete MIT notice ships beside them', async () => {
  const files = await buildGeneratedFiles()
  for (const [file, text] of files) {
    if (file.startsWith('references/')) assert.match(text, /Copyright \(c\) 2026 AgentbaseAI Inc\./, file)
  }
  const license = files.get('LICENSE.generative-frame.txt')
  assert.match(license, /Permission is hereby granted/)
  assert.match(license, /THE SOFTWARE IS PROVIDED "AS IS"/)
  const actual = normalizeText(await readFile(path.join(skillRoot, 'LICENSE.generative-frame.txt'), 'utf8'))
  assert.equal(actual, license)
})
