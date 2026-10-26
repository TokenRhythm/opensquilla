import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { closeSync, constants, existsSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

export const LOCAL_ACCOUNT_FILE = 'account.local.enc'
const KEY_FILE = 'account.local.key'
const PREFIX = 'local-v1:'
const CONTEXT = Buffer.from('opensquilla-desktop-account-v1')

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
        if (!info.isFile() || info.nlink !== 1 || info.size !== 32 || (process.platform !== 'win32' &&
          (info.uid !== process.getuid!() || (info.mode & 0o077) !== 0))) {
          throw new Error('Invalid local account encryption key')
        }
        return readFileSync(file)
      } finally { closeSync(file) }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
      if (!create || existsSync(join(this.directory, LOCAL_ACCOUNT_FILE))) {
        throw new Error('Local account encryption key is missing', { cause: error })
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
    try {
      const iv = randomBytes(12)
      const cipher = createCipheriv('aes-256-gcm', key, iv)
      cipher.setAAD(CONTEXT)
      const encrypted = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()])
      return PREFIX + Buffer.concat([iv, cipher.getAuthTag(), encrypted]).toString('base64')
    } finally { key.fill(0) }
  }

  decrypt(value: string): string {
    if (!value.startsWith(PREFIX)) throw new Error('Invalid local account ciphertext')
    const payload = Buffer.from(value.slice(PREFIX.length), 'base64')
    if (payload.length < 28) throw new Error('Invalid local account ciphertext')
    const key = this.key(false)
    try {
      const decipher = createDecipheriv('aes-256-gcm', key, payload.subarray(0, 12))
      decipher.setAAD(CONTEXT)
      decipher.setAuthTag(payload.subarray(12, 28))
      return Buffer.concat([decipher.update(payload.subarray(28)), decipher.final()]).toString('utf8')
    } finally { key.fill(0) }
  }
}
