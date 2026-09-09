import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { lstat, readFile, realpath } from 'node:fs/promises'
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'

export const AUDIT_MARKER = 'retained-interaction-audit.json'
export const AUDIT_PURPOSE = 'opensquilla-synthetic-signed-update-audit'
export const sha256 = value => createHash('sha256').update(value).digest('hex')
const readJson = bytes => {
  try { return JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, '')) }
  catch { throw new Error('Audit input is not valid UTF-8 JSON') }
}
const samePath = (a, b) => process.platform === 'win32'
  ? resolve(a).toLowerCase() === resolve(b).toLowerCase()
  : resolve(a) === resolve(b)
const contains = (root, path) => {
  const suffix = relative(resolve(root), resolve(path))
  return suffix === '' || (!suffix.startsWith(`..${sep}`) && suffix !== '..' && !isAbsolute(suffix))
}

async function canonicalExisting(path, directory = false) {
  const info = await lstat(path)
  assert.equal(info.isSymbolicLink(), false, 'Audit inputs must not be links')
  assert.equal(directory ? info.isDirectory() : info.isFile(), true, 'Unexpected audit input type')
  assert.ok(samePath(await realpath(path), path), 'Audit inputs must not traverse redirected paths')
}

export function parseAuditManifest(value, manifestPath, outputDir) {
  assert.equal(value?.schemaVersion, 1, 'Unsupported audit marker schema')
  assert.equal(value.purpose, AUDIT_PURPOSE, 'Only this audit synthetic profile is permitted')
  assert.match(value.auditId || '', /^[0-9a-f]{32}$/, 'Invalid audit identity')
  assert.match(value.seedLabel || '', /^[A-Za-z0-9._-]{1,80}$/, 'Invalid preservation seed label')
  assert.match(value.expectedVersion || '', /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/, 'Expected a stable B version')
  assert.match(value.sourceSha || '', /^[0-9a-f]{40}$/, 'Pin the candidate source commit')
  for (const field of ['executableSha256', 'credentialSha256', 'configSha256']) {
    assert.match(value[field] || '', /^[0-9a-f]{64}$/, `Pin ${field} before this probe`)
  }
  for (const path of [manifestPath, outputDir, value.userDataDir, value.executablePath]) {
    assert.equal(typeof path, 'string')
    assert.ok(isAbsolute(path) && !path.includes('\0'), 'All audit paths must be explicit and absolute')
  }
  assert.ok(samePath(manifestPath, join(value.userDataDir, AUDIT_MARKER)), 'Marker must be inside the selected synthetic profile')
  assert.equal(basename(value.executablePath).toLowerCase(), 'opensquilla.exe', 'Select the installed B executable')
  const profile = join(value.userDataDir, 'opensquilla')
  const installRoot = dirname(value.executablePath)
  for (const root of [value.userDataDir, installRoot]) {
    assert.ok(!contains(root, outputDir) && !contains(outputDir, root), 'Evidence and application/profile directories must be separate')
  }
  assert.ok(!contains(installRoot, value.userDataDir) && !contains(value.userDataDir, installRoot), 'Installation and profile must be separate')
  if (value.externalSentinelsDir !== undefined) {
    assert.ok(isAbsolute(value.externalSentinelsDir), 'External sentinel path must be absolute')
  }
  return {
    ...value, manifestPath: resolve(manifestPath), outputDir: resolve(outputDir), profile,
    credentialPath: join(value.userDataDir, 'desktop-credential.json'),
    configPath: join(profile, 'config.toml'), workspace: join(profile, 'workspace'),
    stateDir: join(profile, 'state'), logPath: join(value.userDataDir, 'logs', 'desktop.log'),
  }
}

export function parseSyntheticCredential(value) {
  // Never include a rejected credential value in an assertion/report message.
  assert.ok(value?.provider === 'ollama', 'The retained profile must use the audit Ollama provider')
  assert.ok(value.encryption === 'plain', 'Only a synthetic credential is permitted')
  for (const field of ['apiKeyEnv', 'encryptedApiKey', 'searchApiKeyEnv', 'encryptedSearchApiKey']) {
    assert.ok(value[field] === '', 'Synthetic audit credentials must contain no secret or secret environment reference')
  }
  assert.ok(value.modelRoutingMode === 'direct' && value.routerMode === 'disabled', 'Synthetic routing must remain direct and disabled')
  assert.ok(/^opensquilla-(?:release-session-recovery-smoke|real-updater-rehearsal)$/.test(value.model || ''), 'Only the named synthetic audit model is permitted')
  const url = new URL(value.baseUrl)
  assert.ok(url.protocol === 'http:' && url.hostname === '127.0.0.1' && Number(url.port) > 0
    && url.pathname === '/' && !url.search && !url.hash && !url.username && !url.password,
  'Synthetic provider must use an explicit IPv4 loopback port')
  return { model: value.model, baseUrl: url.origin }
}

export async function verifyAuditInputs(manifestPath, outputDir) {
  await canonicalExisting(manifestPath)
  const manifestBytes = await readFile(manifestPath)
  const plan = parseAuditManifest(readJson(manifestBytes), manifestPath, outputDir)
  for (const directory of [plan.userDataDir, plan.profile, plan.workspace, plan.stateDir]) await canonicalExisting(directory, true)
  if (plan.externalSentinelsDir) await canonicalExisting(plan.externalSentinelsDir, true)
  for (const path of [plan.executablePath, plan.credentialPath, plan.configPath]) await canonicalExisting(path)
  const [executable, credential, config] = await Promise.all([
    readFile(plan.executablePath), readFile(plan.credentialPath), readFile(plan.configPath),
  ])
  assert.equal(sha256(executable), plan.executableSha256, 'Installed B changed before the probe')
  assert.equal(sha256(credential), plan.credentialSha256, 'Retained credentials changed before the probe')
  assert.equal(sha256(config), plan.configSha256, 'Retained configuration changed before the probe')
  return { ...plan, markerSha256: sha256(manifestBytes), provider: parseSyntheticCredential(readJson(credential)) }
}

export async function assertPreservedInputs(plan) {
  assert.equal(sha256(await readFile(plan.manifestPath)), plan.markerSha256, 'Audit marker changed')
  for (const [path, expected, label] of [
    [plan.credentialPath, plan.credentialSha256, 'credentials'],
    [plan.configPath, plan.configSha256, 'configuration'],
    [plan.executablePath, plan.executableSha256, 'installed executable'],
  ]) {
    await canonicalExisting(path)
    assert.equal(sha256(await readFile(path)), expected, `Retained ${label} changed`)
  }
}

export function auditMessages(auditId) {
  return {
    first: `Retained profile first send ${auditId}`,
    firstAnswer: `RETAINED_FIRST_OK ${auditId}`,
    tool: `Read the audit sentinel file with read_file ${auditId}`,
    toolAnswer: `RETAINED_TOOL_OK ${auditId}`,
    stop: `Hold this response until I press Stop ${auditId}`,
    stopPartial: `RETAINED_STOP_STARTED ${auditId}`,
    afterStop: `Retained profile send after Stop ${auditId}`,
    afterStopAnswer: `RETAINED_AFTER_STOP_OK ${auditId}`,
    restart: `Retained profile send after restart ${auditId}`,
    restartAnswer: `RETAINED_RESTART_OK ${auditId}`,
  }
}

export function assertStopEvidence(snapshot, sessionKey) {
  const aborts = snapshot.requests.filter(request => request.method === 'chat.abort')
  assert.equal(aborts.length, 1, 'Stop must send exactly one chat.abort')
  const abort = aborts[0]
  assert.equal(abort.params.sessionKey, sessionKey, 'Stop must target the tested retained-profile session')
  assert.equal(abort.params.source, 'webui_stop', 'Stop must come from the actual UI')
  assert.equal(abort.params.scope, 'task')
  assert.ok(typeof abort.params.taskId === 'string' && abort.params.taskId, 'Stop must identify its task')
  assert.ok(snapshot.events.some(event => event.taskId === abort.params.taskId
    && (!event.sessionKey || event.sessionKey === sessionKey)
    && (event.cancelled || ['aborted', 'cancelled'].includes(event.reason))),
  'The stopped task must publish a cancelled terminal state')
  return { taskId: abort.params.taskId, source: abort.params.source, scope: abort.params.scope }
}
