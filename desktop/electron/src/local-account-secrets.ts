import { createCipheriv, createDecipheriv, randomBytes, randomUUID } from 'node:crypto'
import { closeSync, constants, existsSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export const LOCAL_ACCOUNT_FILE = 'account.local.enc'
const KEY_FILE = 'account.local.key'
const PREFIX = 'local-v1:'
const CONTEXT = Buffer.from('opensquilla-desktop-account-v1')

export class LocalAccountStorageError extends Error {
  constructor(readonly code: 'ACCOUNT_LOCAL_KEY_MISSING' | 'ACCOUNT_LOCAL_KEY_INVALID' | 'ACCOUNT_LOCAL_DATA_INVALID') {
    super(code === 'ACCOUNT_LOCAL_KEY_MISSING' ? 'Local account encryption key is missing' : 'Invalid local account data or key')
  }
}

export function canRecoverAccountStorage(code: string | null | undefined): boolean {
  return ['ACCOUNT_LOCAL_KEY_MISSING', 'ACCOUNT_LOCAL_KEY_INVALID', 'ACCOUNT_LOCAL_DATA_INVALID'].includes(code || '')
}

function encryptWithKey(value: string, key: Buffer): string {
  const iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(CONTEXT)
  const encrypted = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()])
  return PREFIX + Buffer.concat([iv, cipher.getAuthTag(), encrypted]).toString('base64')
}

/**
 * Noninteractive storage protected by the user's file permissions.
 * The key is stored alongside the ciphertext. This does not provide Keychain's
 * isolation from other processes running as the same user.
 */
export class LocalAccountSecrets {
  constructor(private readonly directory: string) {}

  assertAvailable(): void {
    mkdirSync(this.directory, { recursive: true, mode: 0o700 })
    const info = lstatSync(this.directory)
    if (!info.isDirectory() || info.isSymbolicLink() || (process.platform !== 'win32' &&
      (info.uid !== process.getuid!() || (info.mode & 0o077) !== 0))) {
      throw new Error('Account storage directory must be private to the current user')
    }
  }

  private key(create: boolean): Buffer {
    this.assertAvailable()
    const path = join(this.directory, KEY_FILE)
    try {
      if (lstatSync(path).isSymbolicLink()) throw new Error('Invalid local account encryption key')
      const file = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0))
      try {
        const info = fstatSync(file)
        if (!info.isFile() || info.nlink !== 1 || (process.platform !== 'win32' &&
          (info.uid !== process.getuid!() || (info.mode & 0o077) !== 0))) {
          throw new Error('Invalid local account encryption key')
        }
        if (info.size !== 32) throw new LocalAccountStorageError('ACCOUNT_LOCAL_KEY_INVALID')
        return readFileSync(file)
      } finally { closeSync(file) }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
      if (!create || existsSync(join(this.directory, LOCAL_ACCOUNT_FILE))) {
        throw new LocalAccountStorageError('ACCOUNT_LOCAL_KEY_MISSING')
      }
    }
    const key = randomBytes(32)
    try {
      writeFileSync(path, key, { mode: 0o600, flag: 'wx' })
      return key
    } catch (error) {
      // Another instance may have created the key first. Never replace it.
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
      return this.key(false)
    }
  }

  encrypt(value: string): string {
    const key = this.key(true)
    try { return encryptWithKey(value, key) } finally { key.fill(0) }
  }

  decrypt(value: string): string {
    if (!value.startsWith(PREFIX)) throw new LocalAccountStorageError('ACCOUNT_LOCAL_DATA_INVALID')
    const payload = Buffer.from(value.slice(PREFIX.length), 'base64')
    if (payload.length < 28) throw new LocalAccountStorageError('ACCOUNT_LOCAL_DATA_INVALID')
    const key = this.key(false)
    try {
      const decipher = createDecipheriv('aes-256-gcm', key, payload.subarray(0, 12))
      decipher.setAAD(CONTEXT)
      decipher.setAuthTag(payload.subarray(12, 28))
      try { return Buffer.concat([decipher.update(payload.subarray(28)), decipher.final()]).toString('utf8') }
      catch { throw new LocalAccountStorageError('ACCOUNT_LOCAL_DATA_INVALID') }
    } finally { key.fill(0) }
  }

  /** Called only after an explicit recovery confirmation. Keep originals in a private backup. */
  recover(value: string): void {
    this.assertAvailable()
    const backup = join(this.directory, `recovery-${Date.now()}-${randomUUID()}`)
    mkdirSync(backup, { mode: 0o700 })
    const originals = new Map<string, Buffer>()
    const key = randomBytes(32)
    const keyPath = join(this.directory, KEY_FILE)
    const recordPath = join(this.directory, LOCAL_ACCOUNT_FILE)
    const keyTemp = `${keyPath}.${randomUUID()}.tmp`
    const recordTemp = `${recordPath}.${randomUUID()}.tmp`
    let keyReplaced = false
    try {
      for (const name of [LOCAL_ACCOUNT_FILE, KEY_FILE]) {
        const path = join(this.directory, name)
        let exists = true
        try {
          if (lstatSync(path).isSymbolicLink()) throw new Error('Invalid local account storage file')
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
          exists = false
        }
        if (exists) {
          const fd = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0))
          try {
            const info = fstatSync(fd)
            if (!info.isFile() || info.nlink !== 1 || (process.platform !== 'win32' &&
                (info.uid !== process.getuid!() || (info.mode & 0o077) !== 0))) throw new Error('Account storage file must be private')
            const bytes = readFileSync(fd)
            originals.set(name, bytes)
            writeFileSync(join(backup, name), bytes, { flag: 'wx', mode: 0o600 })
          } finally { closeSync(fd) }
        }
      }
      // Write both replacements before touching the active pair. A crash between
      // the atomic renames leaves a detectable decryption failure, never a silent logout.
      writeFileSync(keyTemp, key, { flag: 'wx', mode: 0o600 })
      writeFileSync(recordTemp, encryptWithKey(value, key), { flag: 'wx', mode: 0o600 })
      renameSync(keyTemp, keyPath)
      keyReplaced = true
      renameSync(recordTemp, recordPath)
    } catch (error) {
      if (keyReplaced) {
        const original = originals.get(KEY_FILE)
        if (original) {
          writeFileSync(keyTemp, original, { flag: 'wx', mode: 0o600 })
          renameSync(keyTemp, keyPath)
        } else rmSync(keyPath, { force: true })
      }
      throw error
    } finally {
      key.fill(0)
      for (const bytes of originals.values()) bytes.fill(0)
      rmSync(keyTemp, { force: true })
      rmSync(recordTemp, { force: true })
    }
  }
}
