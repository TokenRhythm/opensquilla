import assert from 'node:assert/strict'
import test from 'node:test'
import { chmod, mkdtemp, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LocalAccountSecrets, LOCAL_ACCOUNT_FILE } from '../dist/local-account-secrets.js'

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'os-local-secrets-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  return { directory, secrets: new LocalAccountSecrets(directory) }
}

test('local ciphertext uses randomized authenticated encryption and survives a new process instance', async t => {
  const { directory, secrets } = await fixture(t)
  const original = 'synthetic-refresh-token'
  const first = secrets.encrypt(original), second = secrets.encrypt(original)
  assert.notEqual(first, second)
  assert.ok(!first.includes(original))
  assert.equal(new LocalAccountSecrets(directory).decrypt(first), original)
  if (process.platform !== 'win32') {
    assert.equal((await stat(directory)).mode & 0o777, 0o700)
    assert.equal((await stat(join(directory, 'account.local.key'))).mode & 0o777, 0o600)
  }
  const payload = Buffer.from(first.slice('local-v1:'.length), 'base64')
  payload[payload.length - 1] ^= 1
  assert.throws(() => secrets.decrypt('local-v1:' + payload.toString('base64')))
  assert.throws(() => secrets.decrypt('old-keychain-ciphertext'))
})

test('another profile cannot decrypt a saved account', async t => {
  const first = await fixture(t), second = await fixture(t)
  const ciphertext = first.secrets.encrypt('synthetic-secret')
  second.secrets.encrypt('another-secret')
  assert.throws(() => second.secrets.decrypt(ciphertext))
})

test('missing or malformed keys never overwrite an existing account or key', async t => {
  const { directory, secrets } = await fixture(t)
  const ciphertext = secrets.encrypt('synthetic-secret')
  await writeFile(join(directory, LOCAL_ACCOUNT_FILE), ciphertext)
  const path = join(directory, 'account.local.key')
  await rm(path)
  assert.throws(() => secrets.encrypt('replacement'), /key is missing/)
  assert.throws(() => secrets.decrypt(ciphertext), /key is missing/)
  await assert.rejects(() => readFile(path), error => error.code === 'ENOENT')
  await writeFile(path, 'invalid', { mode: 0o600 })
  assert.throws(() => secrets.encrypt('replacement'), /Invalid local account/)
  assert.equal(await readFile(path, 'utf8'), 'invalid')
  assert.equal(await readFile(join(directory, LOCAL_ACCOUNT_FILE), 'utf8'), ciphertext)
})

test('nonprivate keys and symbolic links are refused', { skip: process.platform === 'win32' }, async t => {
  const { directory, secrets } = await fixture(t)
  secrets.encrypt('synthetic-secret')
  const path = join(directory, 'account.local.key')
  await chmod(path, 0o644)
  assert.throws(() => secrets.encrypt('replacement'), /Invalid local account/)
  await rm(path)
  const outside = join(directory, 'external-key')
  await writeFile(outside, Buffer.alloc(32), { mode: 0o600 })
  await symlink(outside, path)
  assert.throws(() => secrets.encrypt('replacement'), /Invalid local account/)
  assert.throws(() => secrets.recover('replacement'), /Invalid local account/)
})

test('a failed backup leaves the active credentials and missing key unchanged', { skip: process.platform === 'win32' }, async t => {
  const { directory, secrets } = await fixture(t)
  const ciphertext = secrets.encrypt('synthetic-secret')
  await writeFile(join(directory, LOCAL_ACCOUNT_FILE), ciphertext)
  await rm(join(directory, 'account.local.key'))
  await chmod(directory, 0o500)
  try {
    assert.throws(() => secrets.recover('replacement'))
    assert.equal(await readFile(join(directory, LOCAL_ACCOUNT_FILE), 'utf8'), ciphertext)
    await assert.rejects(() => readFile(join(directory, 'account.local.key')), error => error.code === 'ENOENT')
  } finally { await chmod(directory, 0o700) }
})
