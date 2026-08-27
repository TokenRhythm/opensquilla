import assert from 'node:assert/strict'

import { SecretDecryptionCache } from '../dist/secret-decryption-cache.js'

const cache = new SecretDecryptionCache()
let decryptCalls = 0
const decrypt = () => {
  decryptCalls += 1
  return 'synthetic-decrypted-value'
}

assert.equal(cache.resolve('profile-a', 'cipher-a', 'safeStorage', decrypt), 'synthetic-decrypted-value')
assert.equal(cache.resolve('profile-a', 'cipher-a', 'safeStorage', decrypt), 'synthetic-decrypted-value')
assert.equal(decryptCalls, 1, 'one ciphertext must be decrypted once per active profile')

cache.resolve('profile-a', 'cipher-b', 'safeStorage', decrypt)
assert.equal(decryptCalls, 2, 'a different ciphertext must be resolved independently')

cache.resolve('profile-a', 'cipher-b', 'plain', decrypt)
assert.equal(decryptCalls, 3, 'the encryption backend must participate in the cache key')

cache.resolve('profile-b', 'cipher-a', 'safeStorage', decrypt)
assert.equal(decryptCalls, 4, 'changing profiles must discard the previous profile plaintext')

cache.clear()
cache.resolve('profile-b', 'cipher-a', 'safeStorage', decrypt)
assert.equal(decryptCalls, 5, 'explicit invalidation must force a fresh resolution')

let failureCalls = 0
const fail = () => {
  failureCalls += 1
  throw new Error('synthetic keychain failure')
}
assert.throws(() => cache.resolve('profile-b', 'cipher-failure', 'safeStorage', fail))
assert.throws(() => cache.resolve('profile-b', 'cipher-failure', 'safeStorage', fail))
assert.equal(failureCalls, 2, 'failed resolutions must never be cached')

cache.remember('profile-b', 'cipher-known', 'safeStorage', 'synthetic-known-value')
assert.equal(
  cache.resolve('profile-b', 'cipher-known', 'safeStorage', () => {
    throw new Error('remembered values must not be decrypted again')
  }),
  'synthetic-known-value',
)

console.log('secret decryption cache contract passed')
