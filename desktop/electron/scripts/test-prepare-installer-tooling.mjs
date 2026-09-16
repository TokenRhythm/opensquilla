import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import { isTransientDownloadError, prepareAttempt, prepareWithRetry } from './prepare-installer-tooling.mjs'

const noWait = { sleep: async () => {}, warn: () => {} }

for (const statusCode of [502, 503, 504]) {
  test(`HTTP ${statusCode} retries preparation, not tests`, async () => {
    let calls = 0
    const pauses = []
    const tooling = { executable: '/fixture/makensis' }
    const result = await prepareWithRetry(async () => {
      if (++calls < 3) throw Object.assign(new Error('download failed'), { statusCode })
      return tooling
    }, { ...noWait, sleep: async (ms) => pauses.push(ms) })
    assert.equal(result, tooling)
    assert.equal(calls, 3)
    assert.deepEqual(pauses, [1000, 2000])
  })
}

test('continuous network failure stops after three attempts', async () => {
  let calls = 0
  const error = Object.assign(new Error('timeout'), { code: 'ETIMEDOUT' })
  await assert.rejects(prepareWithRetry(async () => { calls++; throw error }, noWait), error)
  assert.equal(calls, 3)
})

for (const error of [
  new Error('SHA256 checksum mismatch'),
  Object.assign(new Error('integrity check failed'), { statusCode: 503 }),
  Object.assign(new Error('forbidden'), { statusCode: 403 }),
  Object.assign(new Error('not found'), { statusCode: 404 }),
  Object.assign(new Error('invalid configuration'), { code: 'ENOENT' }),
  new assert.AssertionError({ message: 'compile failed' }),
]) {
  test(`${error.message} fails immediately`, async () => {
    let calls = 0
    await assert.rejects(prepareWithRetry(async () => { calls++; throw error }, noWait), error)
    assert.equal(calls, 1)
  })
}

test('retry policy recognizes only selected structured network failures', () => {
  for (const code of ['ETIMEDOUT', 'ESOCKETTIMEDOUT', 'ECONNRESET', 'EAI_AGAIN']) {
    assert.equal(isTransientDownloadError({ code }), true)
  }
  assert.equal(isTransientDownloadError({ response: { statusCode: 504 } }), true)
  assert.equal(isTransientDownloadError(new Error('504 appears in assertion text')), false)
  assert.equal(isTransientDownloadError({ statusCode: 401 }), false)
})

test('each retry runs in a fresh process, not a cached rejected promise', async () => {
  const root = await mkdtemp(join(tmpdir(), 'installer-tooling-worker-'))
  try {
    const worker = join(root, 'worker.mjs')
    const record = join(root, 'pids.json')
    await writeFile(worker, `
      import { readFileSync, writeFileSync } from 'node:fs'
      const record = ${JSON.stringify(record)}
      let pids = []
      try { pids = JSON.parse(readFileSync(record, 'utf8')) } catch {}
      pids.push(process.pid)
      writeFileSync(record, JSON.stringify(pids))
      const ok = pids.length === 3
      process.send(ok ? { tooling: { executable: 'fixture' } }
        : { error: { message: 'download failed', statusCode: 504 } },
        () => process.exit(ok ? 0 : 1))
    `)
    assert.deepEqual(await prepareWithRetry(() => prepareAttempt(worker), noWait), {
      executable: 'fixture',
    })
    const pids = JSON.parse(await readFile(record, 'utf8'))
    assert.equal(pids.length, 3)
    assert.equal(new Set(pids).size, 3)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('missing prepared manifest fails without downloading tooling', async () => {
  const root = await mkdtemp(join(tmpdir(), 'installer-tooling-manifest-'))
  try {
    const contract = fileURLToPath(new URL('./test-installer-progress-contract.mjs', import.meta.url))
    const result = spawnSync(process.execPath, [contract], {
      encoding: 'utf8', timeout: 15_000,
      env: { ...process.env, OPENSQUILLA_INSTALLER_TOOLING_FILE: join(root, 'missing.json') },
    })
    assert.equal(result.error, undefined)
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /ENOENT.*missing\.json/s)
    assert.doesNotMatch(result.stdout + result.stderr, /downloaded|HTTPError/)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})
