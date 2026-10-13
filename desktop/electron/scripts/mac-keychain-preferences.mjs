function parsePaths(output) {
  return output.split('\n').map(line => line.trim()).filter(Boolean).map(line => JSON.parse(line))
}

// Creating a keychain can change account-wide preferences even when its file
// lives in a temporary directory. Restore them immediately, including on error.
export function preserveKeychainPreferences(operation, runSecurity) {
  const searchList = parsePaths(runSecurity(['list-keychains', '-d', 'user']))
  const defaults = parsePaths(runSecurity(['default-keychain', '-d', 'user']))
  if (defaults.length !== 1 || searchList.length === 0) {
    throw new Error('Cannot initialize signing with missing user Keychain preferences.')
  }
  try {
    return operation()
  } finally {
    try {
      const current = parsePaths(runSecurity(['default-keychain', '-d', 'user']))
      if (JSON.stringify(current) !== JSON.stringify(defaults)) {
        runSecurity(['default-keychain', '-d', 'user', '-s', defaults[0]])
      }
    } finally {
      const current = parsePaths(runSecurity(['list-keychains', '-d', 'user']))
      if (JSON.stringify(current) !== JSON.stringify(searchList)) {
        runSecurity(['list-keychains', '-d', 'user', '-s', ...searchList])
      }
    }
  }
}

// codesign needs the signing keychain in the user's search list even when its
// --keychain argument restricts identity matching. Append it without replacing
// existing entries, and remove only our addition after the asynchronous build.
export async function withSigningKeychainSearch(keychainPath, operation, runSecurity) {
  const searchList = parsePaths(runSecurity(['list-keychains', '-d', 'user']))
  const defaults = parsePaths(runSecurity(['default-keychain', '-d', 'user']))
  if (defaults.length !== 1 || searchList.length === 0) {
    throw new Error('Cannot sign with missing user Keychain preferences.')
  }
  const addedHere = !searchList.includes(keychainPath)
  let operationFailed = false
  let operationError
  try {
    if (addedHere) {
      runSecurity(['list-keychains', '-d', 'user', '-s', ...searchList, keychainPath])
    }
    return await operation()
  } catch (error) {
    operationFailed = true
    operationError = error
    throw error
  } finally {
    try {
      try {
        const currentDefaults = parsePaths(runSecurity(['default-keychain', '-d', 'user']))
        if (JSON.stringify(currentDefaults) !== JSON.stringify(defaults)) {
          runSecurity(['default-keychain', '-d', 'user', '-s', defaults[0]])
        }
      } finally {
        if (addedHere) {
          // Read again so a keychain added by another application during signing
          // survives cleanup, in its current order with the original entries.
          const current = parsePaths(runSecurity(['list-keychains', '-d', 'user']))
          const remaining = current.filter(path => path !== keychainPath)
          if (JSON.stringify(current) !== JSON.stringify(remaining)) {
            runSecurity(['list-keychains', '-d', 'user', '-s', ...remaining])
          }
        }
      }
    } catch (cleanupError) {
      if (operationFailed) {
        throw new AggregateError([operationError, cleanupError],
          'Local signing failed and Keychain preferences could not be restored.')
      }
      throw cleanupError
    }
  }
}
