import assert from 'node:assert/strict'

import {
  macCodeSignatureIsAdHoc,
  secretStorageBackendForPolicy,
  shouldUseChromiumMockKeychainForPolicy,
} from '../dist/secret-storage-policy.js'

assert.equal(macCodeSignatureIsAdHoc('Signature=adhoc'), true)
assert.equal(macCodeSignatureIsAdHoc('CodeDirectory flags=0x10002(adhoc,runtime)'), true)
assert.equal(macCodeSignatureIsAdHoc('Authority=Developer ID Application: OpenSquilla'), false)

assert.equal(secretStorageBackendForPolicy({
  envMode: undefined,
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Signature=adhoc',
}), 'plain')

assert.equal(secretStorageBackendForPolicy({
  envMode: undefined,
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Authority=Developer ID Application: OpenSquilla',
}), 'plain')

assert.equal(secretStorageBackendForPolicy({
  envMode: 'safeStorage',
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Signature=adhoc',
}), 'safeStorage')

assert.equal(secretStorageBackendForPolicy({
  envMode: 'plain',
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Authority=Developer ID Application: OpenSquilla',
}), 'plain')

assert.equal(secretStorageBackendForPolicy({
  envMode: undefined,
  platform: 'win32',
  appPackaged: true,
  codesignDiagnostic: '',
}), 'safeStorage')

assert.equal(shouldUseChromiumMockKeychainForPolicy({
  envMode: undefined,
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Signature=adhoc',
}), true)

assert.equal(shouldUseChromiumMockKeychainForPolicy({
  envMode: 'safeStorage',
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Signature=adhoc',
}), false)

assert.equal(shouldUseChromiumMockKeychainForPolicy({
  envMode: undefined,
  platform: 'darwin',
  appPackaged: true,
  codesignDiagnostic: 'Authority=Developer ID Application: OpenSquilla',
}), true)

assert.equal(shouldUseChromiumMockKeychainForPolicy({
  platform: 'darwin', appPackaged: false,
}), true, 'development launches must also avoid native Keychain access')

assert.equal(secretStorageBackendForPolicy({
  platform: 'darwin', appPackaged: false,
}), 'plain')

assert.equal(shouldUseChromiumMockKeychainForPolicy({
  envMode: undefined,
  platform: 'win32',
  appPackaged: true,
  codesignDiagnostic: '',
}), false)

console.log('Secret storage policy tests passed.')
