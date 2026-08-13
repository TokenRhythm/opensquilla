import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { _electron as electron } from 'playwright'

export function requiredOption(name) {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) {
    throw new Error(`Missing required option ${name}`)
  }
  return process.argv[index + 1]
}

export async function waitFor(check, label, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  let lastError
  while (Date.now() < deadline) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(250)
  }
  const detail = lastError ? ` Last error: ${lastError.message || lastError}` : ''
  throw new Error(`Timed out waiting for ${label}.${detail}`)
}

export async function writeSyntheticCredential(
  userDataDir,
  {
    baseUrl = 'http://127.0.0.1:11434',
    disableNetworkObservability = false,
    model = 'opensquilla-packaged-smoke',
  } = {},
) {
  await mkdir(userDataDir, { recursive: true })
  const now = new Date().toISOString()
  await writeFile(
    resolve(userDataDir, 'desktop-credential.json'),
    JSON.stringify({
      provider: 'ollama',
      model,
      baseUrl,
      apiKeyEnv: '',
      encryptedApiKey: '',
      modelRoutingMode: 'direct',
      routerMode: 'disabled',
      routerDefaultTier: 'c1',
      routerTiers: {},
      searchProvider: 'duckduckgo',
      searchApiKeyEnv: '',
      encryptedSearchApiKey: '',
      encryption: 'plain',
      disableNetworkObservability,
      createdAt: now,
      updatedAt: now,
    }, null, 2),
    { mode: 0o600 },
  )
}

export async function launchPackagedCandidate({
  executablePath,
  userDataDir,
  baseUrl,
  disableNetworkObservability = false,
  model,
  env = {},
  scrubProviderSecrets = false,
}) {
  await writeSyntheticCredential(userDataDir, {
    baseUrl,
    disableNetworkObservability,
    model,
  })
  const inheritedEnv = scrubProviderSecrets
    ? environmentWithoutProviderSecrets(process.env)
    : process.env
  return electron.launch({
    executablePath,
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
    ],
    env: {
      ...inheritedEnv,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
      ...env,
    },
  })
}

const PROVIDER_SECRET_ENV_NAME = /(?:^|_)(?:API_?KEY|ACCESS_?KEY|AUTH_?TOKEN|ACCESS_?TOKEN|SESSION_?TOKEN|BEARER_?TOKEN|TOKEN|PASSWORD|PASSWD|PRIVATE_?KEY|CLIENT_?SECRET|SECRET|CREDENTIALS?|WEBHOOK|KEY)(?:$|_)/i

/**
 * Copy process metadata without ever reading values whose names identify
 * provider credentials. Packaged offline gates use this before launching the
 * candidate so a developer's shell credentials cannot reach the child.
 */
export function environmentWithoutProviderSecrets(source) {
  const output = {}
  for (const name of Object.keys(source)) {
    if (PROVIDER_SECRET_ENV_NAME.test(name)) continue
    const value = source[name]
    if (value !== undefined) output[name] = value
  }
  return output
}
