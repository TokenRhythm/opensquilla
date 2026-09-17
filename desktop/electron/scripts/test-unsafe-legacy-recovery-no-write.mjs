import { strict as assert } from 'node:assert'
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  realpath,
  rm,
  symlink,
  writeFile,
} from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '../..')
const recoveryId = '11234567-89ab-4cde-8fab-0123456789ab'

async function waitFor(check, label, timeoutMs = 90_000) {
  const startedAt = Date.now()
  let lastError
  while (Date.now() - startedAt < timeoutMs) {
    try {
      const value = await check()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await delay(250)
  }
  throw new Error(`Timed out waiting for ${label}: ${lastError?.message || lastError || ''}`)
}

function launchEnvironment(isolatedHome) {
  const inherited = { ...process.env }
  for (const name of Object.keys(inherited)) {
    if (name === 'DISPLAY' || name === 'XAUTHORITY') continue
    const upperName = name.toUpperCase()
    if (
      upperName.startsWith('OPENSQUILLA_')
      || ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'].includes(upperName)
      || /(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)/i.test(name)
      || /^(?:AWS|AZURE|GOOGLE|ANTHROPIC|OPENAI|OPENROUTER|MINIMAX|DEEPSEEK|GROQ|MISTRAL|COHERE|GEMINI|OLLAMA|XAI|MOONSHOT|DASHSCOPE|SILICONFLOW|ZHIPU|BAIDU|VOLCENGINE|TENCENT|ALIYUN|HF|HUGGINGFACE)_/i.test(name)
    ) {
      delete inherited[name]
    }
  }
  return {
    ...inherited,
    HOME: isolatedHome,
    USERPROFILE: isolatedHome,
    OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
    OPENSQUILLA_USER_STATE_DIR: join(isolatedHome, 'user-state'),
    OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_GATEWAY_WORKSPACE_DIR: '',
    OPENSQUILLA_WORKSPACE_DIR: '',
    OPENSQUILLA_GATEWAY_STATE_DIR: '',
    LANG: 'en_US.UTF-8',
    LC_ALL: 'en_US.UTF-8',
  }
}

const root = await realpath(await mkdtemp(join(tmpdir(), 'opensquilla-unsafe-legacy-recovery-')))
const userData = join(root, 'user-data')
const isolatedHome = join(root, 'home')
const outside = join(root, 'outside')
const recoveryRoot = join(userData, 'recovery-profiles')
const selectedRoot = join(recoveryRoot, recoveryId)

await mkdir(recoveryRoot, { recursive: true })
await mkdir(isolatedHome, { recursive: true })
await mkdir(outside, { recursive: true })
const unsafeUpdateState = join(outside, 'opensquilla', 'state', 'desktop-update.json')
const unsafeUpdateBytes = JSON.stringify({
  snoozedVersion: '0.0.1',
  snoozedUntil: '2099-01-01T00:00:00.000Z',
}, null, 2)
await mkdir(dirname(unsafeUpdateState), { recursive: true })
await writeFile(unsafeUpdateState, unsafeUpdateBytes, 'utf8')
await symlink(outside, selectedRoot, process.platform === 'win32' ? 'junction' : 'dir')
await writeFile(
  join(userData, 'desktop-profile-context.json'),
  JSON.stringify({
    schema_version: 1,
    active_profile_kind: 'recovery',
    active_recovery_id: recoveryId,
    attention_acknowledgement: null,
    updated_at: '2026-07-11T00:00:00.000Z',
  }, null, 2),
  'utf8',
)

let app
try {
  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userData}`, packageRoot],
    env: launchEnvironment(isolatedHome),
  })

  const page = await waitFor(async () => {
    for (const candidate of app.windows()) {
      if (candidate.isClosed()) continue
      await candidate.waitForLoadState('domcontentloaded', { timeout: 5_000 }).catch(() => {})
      if (
        await candidate.locator('#setup-form, #recoveryPanel.visible, #errorPanel.visible')
          .count()
          .catch(() => 0)
      ) {
        return candidate
      }
    }
    return null
  }, 'primary-only response to unsafe legacy recovery')

  await delay(1_000)
  assert.equal(
    await page.locator('#setup-form').count(),
    1,
    'unsafe legacy maintenance must not replace a usable onboarding path with recovery UI',
  )
  assert.equal(await page.locator('#recoveryPanel.visible').count(), 0)
  assert.equal(await page.locator('#errorPanel.visible').count(), 0)
  assert.deepEqual(await readdir(outside), ['opensquilla'])
  assert.equal(
    await readFile(unsafeUpdateState, 'utf8'),
    unsafeUpdateBytes,
    'legacy recovery discovery must not read or rewrite a linked external profile',
  )
  for (const removedId of [
    'recoveryProfiles',
    'copyCredential',
    'continueRecovery',
    'createRecovery',
    'retryPrimary',
    'returnPrimary',
  ]) {
    assert.equal(await page.locator(`#${removedId}`).count(), 0, removedId)
  }
  const removedBridgeTypes = await page.evaluate(() => ({
    launchSafeProfile: typeof window.opensquillaDesktop.launchSafeProfile,
    retryPrimaryProfile: typeof window.opensquillaDesktop.retryPrimaryProfile,
    returnPrimaryProfile: typeof window.opensquillaDesktop.returnPrimaryProfile,
    getDesktopProfileKind: typeof window.opensquillaDesktop.getDesktopProfileKind,
  }))
  assert.deepEqual(removedBridgeTypes, {
    launchSafeProfile: 'undefined',
    retryPrimaryProfile: 'undefined',
    returnPrimaryProfile: 'undefined',
    getDesktopProfileKind: 'undefined',
  })
  console.log(JSON.stringify({
    ok: true,
    outsideBytesUnchanged: true,
    recoveryChoiceUiPresent: false,
  }))
} finally {
  await app?.close().catch(() => {})
  await rm(root, { recursive: true, force: true }).catch(() => {})
}
