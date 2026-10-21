import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs/promises'
import { mkdtemp, open, realpath, rm, symlink, writeFile } from 'node:fs/promises'
import { syncBuiltinESMExports } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { mock, test } from 'node:test'
import { chooseLocalFilePaths } from '../dist/native-path-picker.js'

const binding = { instanceId: 'owned', profile: 'profile', nonce: 'private-nonce',
  url: 'http://127.0.0.1:1', authToken: 'private-token' }
const request = { gatewayInstanceId: 'owned' }

test('empty and 80 MiB files return canonical paths without content reads/hash/upload', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'opensquilla-path-picker-'))
  try {
    const empty = join(dir, '空 格 #` file.txt')
    const large = join(dir, 'large.bin')
    await writeFile(empty, '')
    const handle = await open(large, 'w')
    try { await handle.truncate(80 * 1024 * 1024) } finally { await handle.close() }
    const forbidden = () => { throw new Error('Content operation is forbidden') }
    const spies = [mock.method(fs, 'readFile', forbidden), mock.method(fs, 'open', forbidden),
      mock.method(crypto, 'createHash', forbidden), mock.method(globalThis, 'fetch', forbidden)]
    syncBuiltinESMExports()
    // No session or workspace is required; the broker has no read/upload dependencies.
    try {
      const result = await chooseLocalFilePaths(request, { connection: () => binding, pick: async () => [empty, large] })
      assert.deepEqual(result, await Promise.all([empty, large].map(path => realpath(path))))
      assert.equal(JSON.stringify(result).includes(binding.nonce), false)
      for (const spy of spies) assert.equal(spy.mock.callCount(), 0)
    } finally { mock.restoreAll(); syncBuiltinESMExports() }
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('file replacement/deletion during metadata checks is rejected, not silently quoted', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'opensquilla-path-picker-race-'))
  try {
    for (const remove of [false, true]) {
      const selected = join(dir, 'selected.txt')
      await writeFile(selected, 'before')
      const originalRealpath = fs.realpath
      let changed = false
      mock.method(fs, 'realpath', async (...args) => {
        const canonical = await originalRealpath(...args)
        if (!changed) {
          changed = true
          if (remove) await rm(selected); else await writeFile(selected, 'different size')
        }
        return canonical
      })
      syncBuiltinESMExports()
      try {
        await assert.rejects(chooseLocalFilePaths(request, { connection: () => binding,
          pick: async () => [selected] }), /changed or is unavailable/)
      } finally { mock.restoreAll(); syncBuiltinESMExports() }
    }
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('cancel returns no paths, too many files and arbitrary renderer paths are rejected', async () => {
  assert.deepEqual(await chooseLocalFilePaths(request, { connection: () => binding, pick: async () => [] }), [])
  await assert.rejects(chooseLocalFilePaths(request, { connection: () => binding, pick: async () => Array(11).fill('x') }), /at most 10/)
  let opened = false
  await assert.rejects(chooseLocalFilePaths({ ...request, path: 'C:\\secret' }, {
    connection: () => binding, pick: async () => { opened = true; return [] },
  }), /Invalid/)
  assert.equal(opened, false)
})

test('unowned localhost and stale instance cannot open the OS picker', async () => {
  for (const connection of [null, { ...binding, instanceId: 'replacement' }]) {
    let opened = false
    await assert.rejects(chooseLocalFilePaths(request, { connection: () => connection,
      pick: async () => { opened = true; return [] } }), /unavailable/)
    assert.equal(opened, false)
  }
})

test('window destruction, navigation and owned child/profile/credentials changes discard selection', async () => {
  for (const changed of [null, ...Object.keys(binding).map(key => ({ ...binding, [key]: 'changed' }))]) {
    let current = binding
    await assert.rejects(chooseLocalFilePaths(request, { connection: () => current,
      pick: async () => { current = changed; return [] } }), /expired/)
  }
})

test('directories, final symlinks, invalid paths and missing files do not become text', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'opensquilla-path-picker-invalid-'))
  try {
    for (const path of [dir, join(dir, 'missing'), 'relative.txt', `${dir}\nsecret`]) {
      await assert.rejects(chooseLocalFilePaths(request, { connection: () => binding, pick: async () => [path] }))
    }
    const link = join(dir, 'link')
    // Junction creation does not require Windows symlink privileges.
    await symlink(dir, link, process.platform === 'win32' ? 'junction' : 'dir')
    await assert.rejects(chooseLocalFilePaths(request, { connection: () => binding, pick: async () => [link] }), /regular file/)
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('production broker imports metadata APIs only, not content reads, hashing or networking', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/native-path-picker.ts', import.meta.url), 'utf8')
  assert.match(source, /import \{ lstat, realpath \} from 'node:fs\/promises'/)
  assert.doesNotMatch(source, /\b(readFile|readFileSync|open|createHash|fetch|Buffer|sessionId|sessionEpoch)\s*\(/)
})
