import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { _electron as electron } from 'playwright'

function option(name) {
  const index = process.argv.indexOf(name)
  if (index < 0 || !process.argv[index + 1]) {
    throw new Error(`Missing required option ${name}`)
  }
  return process.argv[index + 1]
}

async function waitFor(check, label, timeoutMs = 60_000) {
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

async function writeSyntheticCredential(userDataDir, disableNetworkObservability) {
  await mkdir(userDataDir, { recursive: true })
  const now = new Date().toISOString()
  await writeFile(
    resolve(userDataDir, 'desktop-credential.json'),
    JSON.stringify({
      provider: 'ollama',
      model: 'opensquilla-release-update-smoke',
      baseUrl: 'http://127.0.0.1:11434',
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

function updateCachePath(userDataDir) {
  return resolve(userDataDir, 'opensquilla', 'state', 'update_check_rc.json')
}

async function writeSyntheticUpdateCache(userDataDir, baseVersion, timestamp) {
  const path = updateCachePath(userDataDir)
  await mkdir(dirname(path), { recursive: true })
  await writeFile(
    path,
    JSON.stringify({
      schema_version: 2,
      cache_scope: `rc:${baseVersion}`,
      latest_version: null,
      release_url: 'https://github.com/opensquilla/opensquilla/releases',
      checked_at: new Date(timestamp * 1000).toISOString(),
      checked_ts: timestamp,
      last_attempt_ts: timestamp,
      last_error: null,
    }, null, 2),
    { mode: 0o600 },
  )
}

async function writeSyntheticCanonicalWorkspace(userDataDir) {
  const workspaceDir = resolve(userDataDir, 'opensquilla', 'workspace')
  await mkdir(workspaceDir, { recursive: true })
  await writeFile(
    resolve(workspaceDir, 'SOUL.md'),
    'synthetic packaged update-banner smoke\n',
    { mode: 0o600 },
  )
}

async function launchCandidate(
  executablePath,
  userDataDir,
  endpoint,
  privacyDisabled,
  baseVersion,
) {
  await writeSyntheticCredential(userDataDir, privacyDisabled)
  // Seed a fresh successful "no candidate" result before launch. This keeps
  // the gateway's startup check deterministic and lets the page open before
  // the mock release exists without hanging an HTTP request past the product's
  // three-second timeout.
  await writeSyntheticUpdateCache(userDataDir, baseVersion, Math.floor(Date.now() / 1000))
  return electron.launch({
    executablePath,
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
    ],
    env: {
      ...process.env,
      OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
      OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
      OPENSQUILLA_UPDATE_CHECK_ENDPOINT: endpoint,
      OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: privacyDisabled ? '1' : '0',
      // Release jobs run under GitHub Actions, while the product intentionally
      // suppresses passive checks in CI. This isolated smoke supplies a local
      // endpoint and must exercise the packaged runtime's real check path.
      GITHUB_ACTIONS: '0',
      OPENSQUILLA_TESTING: '0',
    },
  })
}

const executablePath = resolve(option('--executable'))
const userDataDir = resolve(option('--user-data-dir'))
const candidateName = option('--candidate-name')
const versionMatch = candidateName.match(/(\d+\.\d+\.\d+)-rc(\d+)/i)

if (!versionMatch) {
  console.log(`Skipping RC update-banner smoke for non-RC candidate: ${candidateName}`)
  process.exit(0)
}

const baseVersion = versionMatch[1]
const currentRc = Number(versionMatch[2])
const nextRc = currentRc + 1
const currentTag = `v${baseVersion}rc${currentRc}`
const nextTag = `v${baseVersion}rc${nextRc}`
const currentVersion = `${baseVersion}-rc${currentRc}`
const nextVersion = `${baseVersion}-rc${nextRc}`
const releaseUrl = `https://github.com/opensquilla/opensquilla/releases/tag/${nextTag}`

let requestCount = 0
let releasePublished = false
const channelManifest = (tag, version) => ({
  schemaVersion: 1,
  tag,
  version,
  baseVersion,
  prerelease: true,
  publishedAt: '2026-07-15T00:00:00Z',
  releaseUrl: `https://github.com/opensquilla/opensquilla/releases/tag/${tag}`,
  sha256sums: 'SHA256SUMS',
  platforms: {
    'darwin-arm64': {
      feed: 'latest-mac.yml',
      archive: `OpenSquilla-${version}-mac-arm64.zip`,
      installer: `OpenSquilla-${version}-mac-arm64.dmg`,
    },
    'win32-x64': {
      feed: 'latest.yml',
      installer: `OpenSquilla-${version}-win-x64.exe`,
    },
  },
})
const payload = () => JSON.stringify(
  releasePublished
    ? channelManifest(nextTag, nextVersion)
    : channelManifest(currentTag, currentVersion),
)

const server = createServer((request, response) => {
  if (request.url !== '/releases') {
    response.writeHead(404)
    response.end()
    return
  }
  requestCount += 1
  response.writeHead(200, {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store',
  })
  response.end(payload())
})
server.listen(0, '127.0.0.1')
await once(server, 'listening')
const address = server.address()
assert.ok(address && typeof address === 'object')
const endpoint = `http://127.0.0.1:${address.port}/releases`

let app
let privacyApp
const privacyUserDataDir = `${userDataDir}-privacy-update-smoke`

try {
  app = await launchCandidate(executablePath, userDataDir, endpoint, false, baseVersion)
  const page = await app.firstWindow({ timeout: 60_000 })
  await waitFor(() => page.url().includes('/control/chat'), 'candidate Control UI')

  const nativeEnabled = await page.evaluate(
    () => window.opensquillaDesktop?.isAutoUpdateEnabled?.(),
  )
  assert.equal(nativeEnabled, false, 'unsigned Windows must use the passive update banner')
  assert.equal(
    await page.locator('[data-testid="update-banner"]').count(),
    0,
    'the banner must be absent before the later RC is published',
  )
  assert.equal(requestCount, 0, 'the fresh empty cache must suppress startup network access')

  releasePublished = true
  // Expire only the synthetic cache after the page is already open. The next
  // visibility-triggered local API call starts one real background lookup.
  await writeSyntheticUpdateCache(userDataDir, baseVersion, 0)

  const banner = page.locator('[data-testid="update-banner"]')
  await waitFor(async () => {
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    return await banner.count()
  }, 'the long-running page to receive the later RC')
  await banner.waitFor({ state: 'visible', timeout: 30_000 })
  assert.match(await banner.textContent(), new RegExp(nextVersion.replaceAll('.', '\\.')))
  assert.equal(await banner.locator('a').getAttribute('href'), releaseUrl)
  assert.equal(requestCount, 1, 'gateway cache/single-flight must prevent duplicate channel requests')

  await app.close()
  app = null

  const beforePrivacyLaunch = requestCount
  await writeSyntheticCanonicalWorkspace(privacyUserDataDir)
  privacyApp = await launchCandidate(
    executablePath,
    privacyUserDataDir,
    endpoint,
    true,
    baseVersion,
  )
  const privacyPage = await privacyApp.firstWindow({ timeout: 60_000 })
  await waitFor(() => privacyPage.url().includes('/control/chat'), 'privacy-disabled Control UI')
  // Remove the TTL shield before probing privacy. If the unified privacy gate
  // regresses, this visibility-triggered call would now reach the mock release
  // server and increment requestCount, so the zero-request assertion is real.
  await writeSyntheticUpdateCache(privacyUserDataDir, baseVersion, 0)
  await privacyPage.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await delay(2_000)
  assert.equal(
    requestCount,
    beforePrivacyLaunch,
    'privacy-disabled candidate must not contact the release endpoint',
  )
  assert.equal(await privacyPage.locator('[data-testid="update-banner"]').count(), 0)

  console.log(JSON.stringify({
    ok: true,
    currentTag,
    discoveredTag: nextTag,
    executable: basename(executablePath),
    releaseUrl,
    requestCount,
  }, null, 2))
} finally {
  await app?.close().catch(() => {})
  await privacyApp?.close().catch(() => {})
  await new Promise((resolveClose) => server.close(resolveClose))
  await rm(privacyUserDataDir, { recursive: true, force: true }).catch(() => {})
}
