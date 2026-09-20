import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { createServer } from 'node:net'
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const repoRoot = resolve(packageRoot, '..', '..')
const desktopOutputDir = join(repoRoot, 'dist', 'desktop-electron')
const sourceRuntimeGatewayDir = join(packageRoot, 'runtime', 'gateway')
const binaryName = process.platform === 'win32' ? 'opensquilla-gateway.exe' : 'opensquilla-gateway'
const deadlineMs = Number.parseInt(process.env.OPENSQUILLA_GATEWAY_SMOKE_TIMEOUT_MS || '90000', 10)
const pollIntervalMs = 250
const killGraceMs = 3_000
const maxTailLines = 80
const caProbeSuccessPattern = /\bopensquilla-desktop-ca-store-ok x509_ca=(\d+)\b/
const documentFixtureText = 'OpenSquilla packaged document fixture'
const strippedTlsEnvironmentKeys = new Set([
  'ALL_PROXY',
  'CURL_CA_BUNDLE',
  'HTTP_PROXY',
  'HTTPS_PROXY',
  'NODE_EXTRA_CA_CERTS',
  'NODE_TLS_REJECT_UNAUTHORIZED',
  'NO_PROXY',
  'REQUESTS_CA_BUNDLE',
  'SSL_CERT_DIR',
  'SSL_CERT_FILE',
])

function appendTail(tail, chunk) {
  const lines = chunk
    .toString()
    .split(/\r?\n/)
    .filter((line) => line.length > 0)
  if (lines.length === 0) return tail
  return [...tail, ...lines].slice(-maxTailLines)
}

function formatTail(stdoutTail, stderrTail) {
  const parts = []
  if (stdoutTail.length > 0) parts.push(`stdout tail:\n${stdoutTail.join('\n')}`)
  if (stderrTail.length > 0) parts.push(`stderr tail:\n${stderrTail.join('\n')}`)
  return parts.length > 0 ? `\n\n${parts.join('\n\n')}` : ''
}

function pathIsFile(path) {
  try {
    return statSync(path).isFile()
  } catch {
    return false
  }
}

function pathIsDirectory(path) {
  try {
    return statSync(path).isDirectory()
  } catch {
    return false
  }
}

function gatewayBinaryCandidates(runtimeGatewayDir) {
  return [join(runtimeGatewayDir, 'opensquilla-gateway', binaryName), join(runtimeGatewayDir, binaryName)]
}

function findGatewayBinary(runtimeGatewayDir) {
  return gatewayBinaryCandidates(runtimeGatewayDir).find(pathIsFile)
}

async function findGeneratedBundleRuntimes(root) {
  const runtimes = []
  const seenResourcesDirs = new Set()
  if (!pathIsDirectory(root)) return runtimes

  function addRuntime(label, resourcesDir, platform) {
    if (platform !== process.platform || seenResourcesDirs.has(resourcesDir)) return
    seenResourcesDirs.add(resourcesDir)
    runtimes.push({
      label,
      runtimeGatewayDir: join(resourcesDir, 'runtime', 'gateway'),
    })
  }

  async function walk(dir, depth) {
    if (depth > 5) return
    const entries = await readdir(dir, { withFileTypes: true }).catch(() => [])
    for (const entry of entries) {
      if (!entry.isDirectory()) continue
      const path = join(dir, entry.name)
      if (entry.name.endsWith('.app')) {
        addRuntime(`generated app bundle ${path}`, join(path, 'Contents', 'Resources'), 'darwin')
      } else if (entry.name === 'win-unpacked' || entry.name === 'linux-unpacked') {
        addRuntime(`generated bundle ${path}`, join(path, 'resources'), entry.name === 'win-unpacked' ? 'win32' : 'linux')
      } else {
        await walk(path, depth + 1)
      }
    }
  }

  await walk(root, 0)
  return runtimes.sort((left, right) => left.runtimeGatewayDir.localeCompare(right.runtimeGatewayDir))
}

async function selectRuntimeGateway() {
  const generatedRuntimes = await findGeneratedBundleRuntimes(desktopOutputDir)
  if (generatedRuntimes.length > 0) {
    const selected = generatedRuntimes[0]
    if (generatedRuntimes.length > 1) {
      console.log(`Found ${generatedRuntimes.length} generated bundle runtimes; selecting first sorted path.`)
      for (const runtime of generatedRuntimes) console.log(`- ${runtime.runtimeGatewayDir}`)
    }
    console.log(`Smoking packaged gateway runtime from ${selected.label}: ${selected.runtimeGatewayDir}`)
    return selected.runtimeGatewayDir
  }

  if (process.env.OPENSQUILLA_REQUIRE_PACKAGED_GATEWAY_SMOKE === '1') {
    throw new Error(`No current-platform generated Electron bundle runtime found under ${desktopOutputDir}.`)
  }

  console.log(`No current-platform generated Electron bundle runtime found under ${desktopOutputDir}; falling back to source runtime ${sourceRuntimeGatewayDir}.`)
  return sourceRuntimeGatewayDir
}

function smokeEnv(tempHome, config, runtimeGatewayDir) {
  const env = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith('OPENSQUILLA_')) continue
    if (strippedTlsEnvironmentKeys.has(key.toUpperCase())) continue
    env[key] = value
  }

  return {
    ...env,
    HOME: tempHome,
    USERPROFILE: tempHome,
    OPENSQUILLA_DESKTOP: '1',
    OPENSQUILLA_INSTALL_METHOD: 'desktop',
    // The Desktop contract treats OPENSQUILLA_STATE_DIR as the profile root H.
    // Runtime databases still live below H/state; config must remain at H/config.toml.
    OPENSQUILLA_STATE_DIR: tempHome,
    OPENSQUILLA_GATEWAY_CONFIG_PATH: config,
    OPENSQUILLA_CONTROL_UI_DIST: join(runtimeGatewayDir, 'control-ui-dist'),
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8:replace',
  }
}

function verifyGatewayCaStore(gatewayBinary, env) {
  const result = spawnSync(gatewayBinary, ['--_desktop-ca-probe'], {
    cwd: dirname(gatewayBinary),
    env,
    encoding: 'utf8',
    windowsHide: true,
  })
  if (result.error) throw result.error
  const match = result.stdout.match(caProbeSuccessPattern)
  const caCertificateCount = match ? Number.parseInt(match[1], 10) : 0
  if (result.status !== 0 || caCertificateCount <= 0) {
    throw new Error(
      `Packaged gateway TLS trust probe failed with exit ${result.status ?? 'null'}.` +
        formatTail(
          result.stdout ? result.stdout.trim().split(/\r?\n/) : [],
          result.stderr ? result.stderr.trim().split(/\r?\n/) : [],
        )
    )
  }
}

function verifyGatewayFilesystemWorker(gatewayBinary, env, targetPath) {
  const payload = JSON.stringify({
    kind: 'read_file',
    path: targetPath,
    displayPath: targetPath,
  })
  const result = spawnSync(gatewayBinary, ['--internal-child', 'filesystem-worker', '-'], {
    cwd: dirname(gatewayBinary),
    env,
    input: payload,
    encoding: 'utf8',
    windowsHide: true,
  })
  if (result.error) throw result.error
  let response = null
  try {
    response = JSON.parse(result.stdout)
  } catch {
    response = null
  }
  if (
    result.status !== 0
    || typeof response?.message !== 'string'
    || !response.message.includes('synthetic packaged gateway smoke')
  ) {
    throw new Error(
      `Packaged gateway filesystem worker probe failed with exit ${result.status ?? 'null'}.`
        + formatTail(
          result.stdout ? result.stdout.trim().split(/\r?\n/) : [],
          result.stderr ? result.stderr.trim().split(/\r?\n/) : [],
        ),
    )
  }
}

function verifyGatewayToolSearch(gatewayBinary, env) {
  const result = spawnSync(gatewayBinary, ['--_desktop-tool-search-probe'], {
    cwd: dirname(gatewayBinary),
    env,
    encoding: 'utf8',
    windowsHide: true,
    timeout: 20_000,
  })
  if (result.error) throw result.error
  if (
    result.status !== 0
    || result.stdout.trim() !== 'opensquilla-desktop-tool-search-ok'
  ) {
    throw new Error(
      `Packaged gateway tool-search resource probe failed with exit ${result.status ?? 'null'}.`
        + formatTail(
          result.stdout ? result.stdout.trim().split(/\r?\n/) : [],
          result.stderr ? result.stderr.trim().split(/\r?\n/) : [],
        ),
    )
  }
}

function verifyGatewayPty(gatewayBinary, env) {
  const result = functionalProbe(gatewayBinary, env, ['--_desktop-pty-probe'])
  if (
    result.probe !== 'opensquilla-desktop-pty'
    || result.available !== true
    || result.ioMode !== 'pty'
    || result.returncode !== 0
  ) {
    throw new Error(`Packaged gateway PTY probe failed: ${JSON.stringify(result)}`)
  }
}

function documentFixture() {
  const stream = `BT /F1 24 Tf 72 720 Td (${documentFixtureText}) Tj ET`
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`,
  ]
  let body = '%PDF-1.4\n'
  const offsets = [0]
  for (const [index, object] of objects.entries()) {
    offsets.push(Buffer.byteLength(body))
    body += `${index + 1} 0 obj\n${object}\nendobj\n`
  }
  const xref = Buffer.byteLength(body)
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`
  for (const offset of offsets.slice(1)) body += `${String(offset).padStart(10, '0')} 00000 n \n`
  body += `trailer\n<< /Root 1 0 R /Size ${objects.length + 1} >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body, 'ascii')
}

function functionalProbe(gatewayBinary, env, args) {
  const result = spawnSync(gatewayBinary, args, {
    cwd: dirname(gatewayBinary), env, encoding: 'utf8', windowsHide: true, timeout: 60_000,
  })
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`Packaged ${args[0]} failed with exit ${result.status ?? 'null'}.`
      + formatTail(result.stdout?.trim().split(/\r?\n/) || [], result.stderr?.trim().split(/\r?\n/) || []))
  }
  return JSON.parse(result.stdout)
}

function verifyGatewayDocument(gatewayBinary, env, path) {
  assert.deepEqual(functionalProbe(gatewayBinary, env, ['--_desktop-document-probe', path]), {
    probe: 'opensquilla-desktop-document', pages: 1, text: documentFixtureText,
    imageMime: 'image/png', imageSize: [1224, 1584],
  })
}

async function verifyGatewayCodeExecution(gatewayBinary, env, tempHome) {
  const code = await readFile(join(scriptDir, 'probe-code-execution.py'), 'utf8')
  const inheritedKeys = new Set([
    'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'HOME', 'USERPROFILE',
    'PYTHONUNBUFFERED', 'PYTHONUTF8', 'PYTHONIOENCODING',
  ])
  const probeEnv = Object.fromEntries(Object.entries(env).filter(([key]) => (
    inheritedKeys.has(key.toUpperCase()) || key.startsWith('OPENSQUILLA_')
  )))
  probeEnv.APPDATA = join(tempHome, 'AppData', 'Roaming')
  probeEnv.LOCALAPPDATA = join(tempHome, 'AppData', 'Local')
  probeEnv.TMP = probeEnv.TEMP = probeEnv.TMPDIR = join(tempHome, 'code-execution-temp')
  await mkdir(probeEnv.APPDATA, { recursive: true })
  await mkdir(probeEnv.LOCALAPPDATA, { recursive: true })
  await mkdir(probeEnv.TMP, { recursive: true })
  assert.deepEqual(functionalProbe(gatewayBinary, probeEnv, [
    '--internal-child', 'python-code', code,
  ]), {
    probe: 'opensquilla-desktop-code-execution', frozen: true,
    pythonExit: 0, errorExit: 7, pages: 1, title: 'Packaged Python tool smoke',
    documents: { csvRows: 2, xlsxValue: 42, pdfText: '中文文件验收 样本 42' },
    published: 4,
  })
}

function verifyGatewayMcp(gatewayBinary, env, port) {
  const { protocolVersion, ...result } = functionalProbe(gatewayBinary, env, [
    '--_desktop-mcp-probe', `ws://127.0.0.1:${port}/ws`,
  ])
  // The Python probe verifies this against the bundled SDK's latest version.
  assert.match(protocolVersion, /^\d{4}-\d{2}-\d{2}$/)
  assert.deepEqual(result, {
    probe: 'opensquilla-desktop-mcp', sessions: 0, resources: ['opensquilla://sessions'],
    tools: ['conversations_list', 'events_wait', 'messages_read', 'messages_send', 'session_resolve', 'transcript_export'],
  })
}

async function findFreePort() {
  return await new Promise((resolvePort, reject) => {
    const server = createServer()
    server.unref()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (!address || typeof address === 'string') {
        server.close(() => reject(new Error('Could not determine an available loopback port.')))
        return
      }
      const { port } = address
      server.close((error) => {
        if (error) reject(error)
        else resolvePort(port)
      })
    })
  })
}

async function sleep(ms) {
  await new Promise((resolveSleep) => setTimeout(resolveSleep, ms))
}

function bodySnippet(body) {
  return body.length > 300 ? `${body.slice(0, 300)}...` : body
}

async function healthCheck(url) {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(1_000) })
    const body = await response.text()
    if (!response.ok) {
      return { ok: false, detail: `${url} returned HTTP ${response.status}: ${bodySnippet(body)}` }
    }

    let payload
    try {
      payload = JSON.parse(body)
    } catch (error) {
      return {
        ok: false,
        detail: `${url} returned HTTP ${response.status} with non-JSON body: ${bodySnippet(body)} (${error instanceof Error ? error.message : String(error)})`,
      }
    }

    if (payload?.ok === true) return { ok: true, detail: '' }
    return { ok: false, detail: `${url} returned JSON without ok=true: ${bodySnippet(body)}` }
  } catch (error) {
    return { ok: false, detail: `${url} request failed: ${error instanceof Error ? error.message : String(error)}` }
  }
}

async function fetchText(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(2_000) })
  const body = await response.text()
  if (!response.ok) {
    throw new Error(`${url} returned HTTP ${response.status}: ${bodySnippet(body)}`)
  }
  return body
}

function controlAssetUrls(html, baseUrl) {
  const urls = []
  for (const match of html.matchAll(/<script\b[^>]*\btype="module"[^>]*\bsrc="([^"]+)"/g)) {
    urls.push(new URL(match[1], baseUrl).toString())
  }
  for (const match of html.matchAll(/<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"/g)) {
    urls.push(new URL(match[1], baseUrl).toString())
  }
  return urls
}

async function verifyControlUi(port, stdoutTail, stderrTail) {
  const controlUrl = `http://127.0.0.1:${port}/control/`
  const html = await fetchText(controlUrl)
  const assetUrls = controlAssetUrls(html, controlUrl)

  const hasModule = assetUrls.some((url) => url.includes('/static/dist/') && url.endsWith('.js'))
  const hasStylesheet = assetUrls.some((url) => url.includes('/static/dist/') && url.endsWith('.css'))
  if (!hasModule || !hasStylesheet) {
    throw new Error(
      `${controlUrl} did not inject Vite JS/CSS assets from /static/dist/. ` +
        `Found assets: ${assetUrls.length > 0 ? assetUrls.join(', ') : '(none)'}.` +
        formatTail(stdoutTail, stderrTail)
    )
  }

  for (const url of assetUrls.filter((assetUrl) => assetUrl.includes('/static/dist/'))) {
    await fetchText(url)
  }
}

async function waitForGateway(port, childExit, stdoutTail, stderrTail) {
  const deadline = Date.now() + deadlineMs
  const healthzUrl = `http://127.0.0.1:${port}/healthz`
  const healthUrl = `http://127.0.0.1:${port}/health`
  let lastHealthFailure = ''

  while (Date.now() < deadline) {
    if (childExit.value) {
      const { code, signal } = childExit.value
      throw new Error(
        `Gateway exited before becoming healthy (code=${code ?? 'null'} signal=${signal ?? 'null'}).` +
          formatTail(stdoutTail, stderrTail)
      )
    }

    const healthz = await healthCheck(healthzUrl)
    if (healthz.ok) return
    const health = await healthCheck(healthUrl)
    if (health.ok) return
    lastHealthFailure = `${healthz.detail}; ${health.detail}`
    await sleep(pollIntervalMs)
  }

  const detail = lastHealthFailure ? ` Last health failure: ${lastHealthFailure}` : ''
  throw new Error(`Timed out after ${deadlineMs / 1000}s waiting for ${healthzUrl} or ${healthUrl}.${detail}` + formatTail(stdoutTail, stderrTail))
}

async function terminateChild(child, childClosed) {
  if (childClosed.value) return

  await new Promise((resolveTerminate) => {
    let settled = false
    let forceTimer = null
    let abandonTimer = null

    function finish() {
      if (settled) return
      settled = true
      if (forceTimer) clearTimeout(forceTimer)
      if (abandonTimer) clearTimeout(abandonTimer)
      resolveTerminate()
    }

    if (process.platform !== 'win32') child.once('close', finish)

    if (child.exitCode === null && child.signalCode === null) {
      if (process.platform === 'win32' && child.pid) {
        const finishAfterTaskkill = () => {
          if (childClosed.value || child.exitCode !== null || child.signalCode !== null) {
            finish()
            return
          }
          abandonTimer = setTimeout(finish, 1_000)
        }
        const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], {
          stdio: 'ignore',
          windowsHide: true,
        })
        const taskkillTimer = setTimeout(() => {
          console.warn(`taskkill timed out while terminating gateway process ${child.pid}.`)
          killer.kill()
          finishAfterTaskkill()
        }, killGraceMs)
        killer.once('error', (error) => {
          clearTimeout(taskkillTimer)
          console.warn(`taskkill failed while terminating gateway process ${child.pid}: ${error.message}`)
          finishAfterTaskkill()
        })
        killer.once('close', (code, signal) => {
          clearTimeout(taskkillTimer)
          if (code !== 0) {
            console.warn(`taskkill exited while terminating gateway process ${child.pid} with code=${code ?? 'null'} signal=${signal ?? 'null'}.`)
          }
          finishAfterTaskkill()
        })
      } else if (process.platform === 'win32') {
        console.warn('Gateway process had no PID for taskkill fallback.')
        child.kill()
      } else {
        child.kill('SIGTERM')
      }
    }

    forceTimer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        if (process.platform !== 'win32') {
          child.kill('SIGKILL')
          abandonTimer = setTimeout(finish, 1_000)
        } else {
          finish()
        }
      } else {
        finish()
      }
    }, killGraceMs)
  })
}

async function main() {
  const runtimeGatewayDir = await selectRuntimeGateway()
  const candidates = gatewayBinaryCandidates(runtimeGatewayDir)
  const gatewayBinary = findGatewayBinary(runtimeGatewayDir)
  if (!gatewayBinary) {
    throw new Error(
      `Packaged gateway binary is missing. Checked: ${candidates.join(', ')}. Run npm run build:gateway first; release CI should run this after electron-builder.`
    )
  }

  const tempHome = await mkdtemp(join(tmpdir(), 'opensquilla-gateway-smoke-'))
  const config = join(tempHome, 'config.toml')
  const stateDir = join(tempHome, 'state')
  const workspaceDir = join(tempHome, 'workspace')
  let child = null
  const stdoutTail = []
  const stderrTail = []
  const childExit = { value: null }
  const childClosed = { value: false }

  try {
    await mkdir(stateDir, { recursive: true })
    await mkdir(workspaceDir, { recursive: true })
    await writeFile(join(workspaceDir, 'SOUL.md'), 'synthetic packaged gateway smoke\n', 'utf8')
    const documentPath = join(workspaceDir, 'document-fixture.pdf')
    await writeFile(documentPath, documentFixture())
    await writeFile(
      config,
      [
        '[auth]',
        'mode = "none"',
        '',
      ].join('\n'),
      'utf8'
    )

    const env = smokeEnv(tempHome, config, runtimeGatewayDir)
    verifyGatewayCaStore(gatewayBinary, env)
    verifyGatewayToolSearch(gatewayBinary, env)
    verifyGatewayPty(gatewayBinary, env)
    verifyGatewayFilesystemWorker(gatewayBinary, env, join(workspaceDir, 'SOUL.md'))
    verifyGatewayDocument(gatewayBinary, env, documentPath)
    await verifyGatewayCodeExecution(gatewayBinary, env, tempHome)

    const port = await findFreePort()
    child = spawn(gatewayBinary, ['gateway', 'run', '--port', String(port), '--bind', '127.0.0.1', '--config', config], {
      cwd: dirname(gatewayBinary),
      env,
      windowsHide: true,
    })

    child.stdout.on('data', (chunk) => {
      stdoutTail.splice(0, stdoutTail.length, ...appendTail(stdoutTail, chunk))
    })
    child.stderr.on('data', (chunk) => {
      stderrTail.splice(0, stderrTail.length, ...appendTail(stderrTail, chunk))
    })
    child.once('close', (code, signal) => {
      childClosed.value = true
      childExit.value = { code, signal }
    })
    child.once('error', (error) => {
      childExit.value = { code: null, signal: `spawn error: ${error.message}` }
    })

    await waitForGateway(port, childExit, stdoutTail, stderrTail)
    await verifyControlUi(port, stdoutTail, stderrTail)
    verifyGatewayMcp(gatewayBinary, env, port)
    console.log('OpenSquilla packaged gateway smoke passed.')
  } finally {
    if (child) await terminateChild(child, childClosed)
    await rm(tempHome, { recursive: true, force: true })
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error))
  process.exit(1)
})
