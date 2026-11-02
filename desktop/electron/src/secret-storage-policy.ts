export type SecretStorageBackend = 'safeStorage' | 'plain'

export interface SecretStoragePolicyInput {
  envMode?: string
  platform: NodeJS.Platform
  appPackaged: boolean
  codesignDiagnostic?: string
}

export function macCodeSignatureIsAdHoc(diagnostic: string): boolean {
  return /\bSignature=adhoc\b/.test(diagnostic) || /\bflags=[^\n]*\badhoc\b/.test(diagnostic)
}

export function secretStorageBackendForPolicy(input: SecretStoragePolicyInput): SecretStorageBackend {
  const mode = (input.envMode || '').trim().toLowerCase()
  if (mode === 'plain' || mode === 'plaintext' || mode === 'none') return 'plain'
  if (mode === 'safe' || mode === 'safe-storage' || mode === 'safestorage') return 'safeStorage'

  // Default macOS credentials to user-private files, without native Keychain UI.
  // OS storage remains an explicit opt-in for installations that require it.
  if (input.platform === 'darwin') return 'plain'

  return 'safeStorage'
}

export function shouldUseChromiumMockKeychainForPolicy(input: SecretStoragePolicyInput): boolean {
  return input.platform === 'darwin' &&
    secretStorageBackendForPolicy(input) === 'plain'
}
