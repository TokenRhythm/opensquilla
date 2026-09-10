import assert from 'node:assert/strict'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DesktopLocalFileGrantManager } from '../dist/desktop-local-file-grant.js'

const root = await mkdtemp(join(tmpdir(), 'opensquilla-local-file-grant-'))
try {
  const path = join(root, 'report.txt')
  await writeFile(path, 'synthetic report')
  let now = 1_000
  let state = { owned: true, status: 'ready', instanceId: 'instance-a' }
  const manager = new DesktopLocalFileGrantManager(() => state, 5_000, () => true)

  const issued = manager.issue({
    path,
    name: 'report.txt',
    mime: 'text/plain',
    size: 16,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
    now,
  })
  assert.equal(issued.ok, true)
  if (!issued.ok) throw new Error('grant was not issued')
  assert.equal('path' in issued.value, false)
  assert.equal(manager.capabilities().available, true)
  assert.deepEqual(manager.resolve({
    grant: issued.value.grant,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
    now: now + 1,
  }), { ok: true, path })

  assert.equal(manager.resolve({
    grant: issued.value.grant,
    subject: 'webcontents:8',
    executionEnvironment: 'default',
    now: now + 1,
  }).ok, false)
  state = { ...state, instanceId: 'instance-b' }
  assert.equal(manager.resolve({
    grant: issued.value.grant,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
    now: now + 1,
  }).ok, false)

  state = { owned: true, status: 'ready', instanceId: 'instance-a' }
  const changed = manager.issue({
    path,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
    now,
  })
  assert.equal(changed.ok, true)
  if (!changed.ok) throw new Error('second grant was not issued')
  await writeFile(path, 'changed report')
  assert.equal(manager.resolve({
    grant: changed.value.grant,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
    now: now + 1,
  }).ok, false)

  const unavailable = new DesktopLocalFileGrantManager(
    () => ({ owned: false, status: 'ready', instanceId: null }),
  )
  assert.equal(unavailable.capabilities().available, false)
  assert.equal(unavailable.issue({
    path,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
  }).ok, false)

  const noConsumer = new DesktopLocalFileGrantManager(() => state)
  assert.equal(noConsumer.capabilities().available, false)

  now += 10_000
  assert.equal(manager.resolve({
    grant: issued.value.grant,
    subject: 'webcontents:7',
    executionEnvironment: 'default',
    now,
  }).ok, false)
} finally {
  await rm(root, { recursive: true, force: true })
}

console.log('desktop local file grant contract: ok')
