export type CachedSecretEncryption = 'safeStorage' | 'plain'

/**
 * Keeps successful secret resolutions for the lifetime of one active profile.
 *
 * Electron's safeStorage API is synchronous, so main-process execution already
 * serializes competing lookups. Caching the result before returning gives that
 * synchronous path single-flight semantics: every ciphertext is decrypted at
 * most once until the profile or credential changes. Failed resolutions are
 * deliberately not inserted and are therefore retried.
 */
export class SecretDecryptionCache {
  private profileScope: string | null = null
  private readonly values = new Map<string, string>()

  resolve(
    profileScope: string,
    encryptedValue: string,
    encryption: CachedSecretEncryption,
    decrypt: () => string,
  ): string {
    this.selectProfile(profileScope)
    const key = JSON.stringify([encryption, encryptedValue])
    const cached = this.values.get(key)
    if (cached !== undefined || this.values.has(key)) return cached ?? ''

    // Do not catch here. A failed Keychain operation must not poison the cache.
    const value = decrypt()
    this.values.set(key, value)
    return value
  }

  remember(
    profileScope: string,
    encryptedValue: string,
    encryption: CachedSecretEncryption,
    value: string,
  ): void {
    this.selectProfile(profileScope)
    this.values.set(JSON.stringify([encryption, encryptedValue]), value)
  }

  clear(): void {
    this.values.clear()
    this.profileScope = null
  }

  private selectProfile(profileScope: string): void {
    if (this.profileScope === profileScope) return
    this.values.clear()
    this.profileScope = profileScope
  }
}
