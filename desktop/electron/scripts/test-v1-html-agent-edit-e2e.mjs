import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createServer } from 'node:http'
import { createServer as createTcpServer } from 'node:net'
import { cp, mkdtemp, mkdir, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

import { _electron as electron } from 'playwright'

import {
  environmentWithoutProviderSecrets,
  waitFor,
} from './packaged-smoke-helpers.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const electronRoot = join(scriptDir, '..')
const repoRoot = join(electronRoot, '..', '..')
// Use a catalogued tool-capable model id so the artifact writer's verified-tool
// gate admits the exact document surface. Requests still terminate at the
// loopback fixture below; this never contacts or impersonates a live provider.
const SYNTHETIC_MODEL = 'gpt-5.4-mini'
const INITIAL_HEADING = 'Synthetic draft heading'
const APPLIED_HEADING = 'Verified V1 heading'
const ANNOTATION_BODY = 'Use a shorter synthetic heading.'
const ANSWER_ONLY_ANNOTATION_BODY = 'Explain this synthetic paragraph without editing it.'
const INITIAL_MESSAGE = 'Keep this synthetic fixture unchanged.'
const APPLY_MESSAGE = 'Apply the selected synthetic instruction once.'
const ANSWER_ONLY_MESSAGE = 'Explain the selected synthetic paragraph without changing it.'
const EXPECTED_DOCUMENT_TOOLS = [
  'document_apply',
  'document_inspect',
  'document_locate',
  'document_read',
]
const TIMEOUT_MS = 60_000
const STARTUP_TIMEOUT_MS = 120_000
const execFileAsync = promisify(execFile)
const uvExecutable = process.platform === 'win32' ? 'uv.exe' : 'uv'

function jsonFromToolContent(content) {
  const text = String(content || '')
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start < 0 || end < start) {
    throw new Error('Synthetic provider received a tool result without JSON.')
  }
  return JSON.parse(text.slice(start, end + 1))
}

function messageText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map(part => typeof part?.text === 'string' ? part.text : '')
    .join('\n')
}

function isMutationFinalizationRequest(payload) {
  const messages = Array.isArray(payload?.messages) ? payload.messages : []
  return messages.some(message => (
    message?.role === 'user'
    && messageText(message?.content).includes('documentMutationOutcome')
  ))
}

function documentToolNames(payload) {
  return (Array.isArray(payload?.tools) ? payload.tools : [])
    .map(item => item?.function?.name)
    .filter(name => EXPECTED_DOCUMENT_TOOLS.includes(name))
    .sort()
}

function openAiTextChunks(model, text) {
  return [
    {
      model,
      choices: [{
        index: 0,
        delta: { role: 'assistant', content: text },
        finish_reason: null,
      }],
    },
    {
      model,
      choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
      usage: { prompt_tokens: 12, completion_tokens: 5 },
    },
  ]
}

function openAiToolChunks(model, callId, name, args) {
  return [
    {
      model,
      choices: [{
        index: 0,
        delta: {
          role: 'assistant',
          tool_calls: [{
            index: 0,
            id: callId,
            type: 'function',
            function: {
              name,
              arguments: JSON.stringify(args),
            },
          }],
        },
        finish_reason: null,
      }],
    },
    {
      model,
      choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }],
      usage: { prompt_tokens: 12, completion_tokens: 5 },
    },
  ]
}

async function startDeterministicProvider() {
  const requests = []
  let documentApplyCalls = 0
  const server = createServer((request, response) => {
    const url = new URL(request.url || '/', 'http://127.0.0.1')
    if (request.method === 'GET' && url.pathname === '/v1/models') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({
        object: 'list',
        data: [{ id: SYNTHETIC_MODEL, object: 'model', owned_by: 'opensquilla-test' }],
      }))
      return
    }
    if (request.method !== 'POST' || url.pathname !== '/v1/chat/completions') {
      response.writeHead(404, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ error: { message: 'unsupported synthetic endpoint' } }))
      return
    }

    const chunks = []
    request.on('data', chunk => chunks.push(chunk))
    request.on('end', () => {
      const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      requests.push(payload)
      const model = String(payload.model || SYNTHETIC_MODEL)
      const tools = Array.isArray(payload.tools) ? payload.tools : []
      const toolNames = new Set(tools.map(item => item?.function?.name).filter(Boolean))
      const messages = Array.isArray(payload.messages) ? payload.messages : []
      const toolMessages = messages.filter(message => message?.role === 'tool')
      const currentUserMessage = [...messages]
        .reverse()
        .find(message => message?.role === 'user')
      const answerOnlyAnnotatedTurn = messageText(currentUserMessage?.content)
        .includes(ANSWER_ONLY_MESSAGE)
      const hasDocumentTools = toolNames.has('document_inspect')
        || toolNames.has('document_apply')
      let bodyChunks

      if (hasDocumentTools && answerOnlyAnnotatedTurn) {
        bodyChunks = openAiTextChunks(
          model,
          'The selected synthetic paragraph explains preserved fixture content.',
        )
      } else if (!hasDocumentTools) {
        const finalizedMutation = toolMessages.some(message => (
          message?.name === 'document_apply'
          || String(message?.tool_call_id || '').includes('document_apply')
        ))
        bodyChunks = openAiTextChunks(
          model,
          finalizedMutation
            ? 'The selected synthetic heading was updated.'
            : 'Synthetic fixture acknowledged without a document edit.',
        )
      } else if (toolMessages.length === 0) {
        bodyChunks = openAiToolChunks(
          model,
          'call_document_inspect_v1_e2e',
          'document_inspect',
          {},
        )
      } else {
        const latest = toolMessages.at(-1)
        const latestName = String(latest?.name || '')
        if (latestName === 'document_apply') {
          bodyChunks = openAiTextChunks(model, 'The selected synthetic heading was updated.')
        } else {
          const inspected = jsonFromToolContent(latest?.content)
          const annotations = Array.isArray(inspected.annotations) ? inspected.annotations : []
          assert.equal(annotations.length, 1, 'the fixture turn must expose exactly one annotation')
          const locations = Array.isArray(annotations[0]?.initialLocations)
            ? annotations[0].initialLocations
            : []
          const location = locations.find(candidate => candidate?.operation === 'replace_text')
          assert.ok(location?.grantToken, 'document_inspect must return a replace_text grant')
          documentApplyCalls += 1
          bodyChunks = openAiToolChunks(
            model,
            'call_document_apply_v1_e2e',
            'document_apply',
            {
              mutations: [{
                grant_token: location.grantToken,
                input: APPLIED_HEADING,
              }],
            },
          )
        }
      }

      if (payload.stream === false) {
        const text = bodyChunks
          .map(chunk => chunk?.choices?.[0]?.delta?.content || '')
          .join('')
        const body = Buffer.from(JSON.stringify({
          id: 'synthetic-non-stream-response',
          object: 'chat.completion',
          model,
          choices: [{
            index: 0,
            message: { role: 'assistant', content: text },
            finish_reason: 'stop',
          }],
          usage: { prompt_tokens: 12, completion_tokens: 5 },
        }))
        response.writeHead(200, {
          'content-type': 'application/json',
          'cache-control': 'no-store',
          'content-length': String(body.length),
        })
        response.end(body)
        return
      }

      const body = Buffer.from(
        bodyChunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('')
          + 'data: [DONE]\n\n',
      )
      response.writeHead(200, {
        'content-type': 'text/event-stream',
        'cache-control': 'no-store',
        'content-length': String(body.length),
      })
      response.end(body)
    })
  })

  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert.ok(address && typeof address === 'object')
  return {
    baseUrl: `http://127.0.0.1:${address.port}/v1`,
    requests,
    documentApplyCalls: () => documentApplyCalls,
    close: () => new Promise((resolveClose, rejectClose) => {
      server.closeIdleConnections?.()
      server.close(error => error ? rejectClose(error) : resolveClose())
    }),
  }
}

async function reserveLoopbackPort() {
  const server = createTcpServer()
  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert.ok(address && typeof address === 'object')
  const port = address.port
  await new Promise(resolveClose => server.close(resolveClose))
  return port
}

async function seedDesktopCredential(userDataDir, providerBaseUrl) {
  await mkdir(userDataDir, { recursive: true })
  const now = '2026-08-16T00:00:00.000Z'
  const credential = {
    provider: 'openai',
    model: SYNTHETIC_MODEL,
    baseUrl: providerBaseUrl,
    apiKeyEnv: 'OPENAI_API_KEY',
    encryptedApiKey: Buffer.from('synthetic-loopback-key', 'utf8').toString('base64'),
    modelRoutingMode: 'direct',
    routerMode: 'disabled',
    routerDefaultTier: 'c1',
    routerTiers: {},
    searchProvider: 'duckduckgo',
    searchApiKeyEnv: '',
    encryptedSearchApiKey: '',
    encryption: 'plain',
    configAuthority: 'generated',
    importTransactionId: '',
    disableNetworkObservability: true,
    createdAt: now,
    updatedAt: now,
  }
  await writeFile(
    join(userDataDir, 'desktop-credential.json'),
    `${JSON.stringify(credential, null, 2)}\n`,
    { mode: 0o600 },
  )
}

async function createDevelopmentElectronRoot(isolationRoot) {
  // The repository may contain a previously built bundled Gateway under
  // desktop/electron/runtime. Source Electron deliberately prefers that binary,
  // which would test stale Python/static bytes. A minimal copied shell without
  // runtime/ forces the documented dev path (`uv run opensquilla`) and therefore
  // exercises this checkout's Gateway and freshly built Vue bundle.
  const root = join(isolationRoot, 'electron-source-shell')
  await mkdir(join(root, 'src'), { recursive: true })
  await Promise.all([
    cp(join(electronRoot, 'dist'), join(root, 'dist'), { recursive: true }),
    cp(join(electronRoot, 'assets'), join(root, 'assets'), { recursive: true }),
    cp(join(electronRoot, 'package.json'), join(root, 'package.json')),
    cp(join(electronRoot, 'src', 'boot.html'), join(root, 'src', 'boot.html')),
  ])
  await symlink(
    join(electronRoot, 'node_modules'),
    join(root, 'node_modules'),
    process.platform === 'win32' ? 'junction' : 'dir',
  )
  return root
}

async function readDurableMutationEvidence(isolationRoot) {
  const databasePath = join(
    isolationRoot,
    'electron-user-data',
    'opensquilla',
    'state',
    'sessions.db',
  )
  const program = `
import json
import sqlite3
import sys

database_path = sys.argv[1]
connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row

def scalar(query):
    return int(connection.execute(query).fetchone()[0])

row = connection.execute(
    """
    SELECT status, change_set_id, revision_id
    FROM artifact_mutation_attempts
    """
).fetchone()
evidence = {
    "documents": scalar("SELECT COUNT(*) FROM artifact_documents"),
    "revisions": scalar("SELECT COUNT(*) FROM artifact_revisions"),
    "changeSets": scalar("SELECT COUNT(*) FROM artifact_change_sets"),
    "mutationAttempts": scalar("SELECT COUNT(*) FROM artifact_mutation_attempts"),
    "annotations": scalar("SELECT COUNT(*) FROM artifact_prompt_annotations"),
    "sentAnnotations": scalar(
        "SELECT COUNT(*) FROM artifact_prompt_annotations WHERE status = 'sent'"
    ),
    "appliedAttempts": scalar(
        "SELECT COUNT(*) FROM artifact_mutation_attempts WHERE status = 'applied'"
    ),
    "attemptLinksCommittedObjects": scalar(
        """
        SELECT COUNT(*)
        FROM artifact_mutation_attempts AS attempt
        JOIN artifact_change_sets AS change_set
          ON change_set.change_set_id = attempt.change_set_id
        JOIN artifact_revisions AS revision
          ON revision.revision_id = attempt.revision_id
        WHERE attempt.status = 'applied'
          AND change_set.applied_revision_id = revision.revision_id
          AND revision.change_set_id = change_set.change_set_id
        """
    ),
    "attemptStatus": row["status"] if row is not None else None,
    "attemptHasChangeSet": bool(row and row["change_set_id"]),
    "attemptHasRevision": bool(row and row["revision_id"]),
}
connection.close()
print(json.dumps(evidence, sort_keys=True))
`
  const env = environmentWithoutProviderSecrets(process.env)
  const { stdout } = await execFileAsync(
    uvExecutable,
    ['run', 'python', '-c', program, databasePath],
    {
      cwd: repoRoot,
      env,
      timeout: TIMEOUT_MS,
      maxBuffer: 1024 * 1024,
    },
  )
  return JSON.parse(stdout.trim())
}

function launchEnvironment(isolationRoot, gatewayPort) {
  const inherited = environmentWithoutProviderSecrets(process.env)
  for (const name of Object.keys(inherited)) {
    if (name.startsWith('OPENSQUILLA_')) delete inherited[name]
  }
  const isolatedHome = join(isolationRoot, 'home')
  return {
    ...inherited,
    HOME: isolatedHome,
    USERPROFILE: isolatedHome,
    LOCALAPPDATA: join(isolatedHome, 'LocalAppData'),
    TEMP: join(isolatedHome, 'Temp'),
    TMP: join(isolatedHome, 'Temp'),
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true',
    OPENSQUILLA_DESKTOP_REPO_ROOT: repoRoot,
    OPENSQUILLA_DESKTOP_SECRET_STORAGE: 'plain',
    OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE: '1',
    OPENSQUILLA_DESKTOP_GATEWAY_PORT: String(gatewayPort),
    OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
    OPENSQUILLA_TEST_PROFILE_LOCK_ROOT: '1',
    OPENSQUILLA_USER_STATE_DIR: join(isolatedHome, 'user-state'),
    // Gateway source launch uses `uv run`. Keep only the package cache shared;
    // all OpenSquilla config, credentials, state, and workspace remain isolated.
    UV_CACHE_DIR: process.env.UV_CACHE_DIR
      || join(tmpdir(), 'opensquilla-v1-html-agent-edit-uv-cache'),
    NO_PROXY: '127.0.0.1,localhost,.localhost,::1',
    no_proxy: '127.0.0.1,localhost,.localhost,::1',
  }
}

function isAllowedLoopbackUrl(value) {
  try {
    const url = new URL(value)
    return ['127.0.0.1', 'localhost', '::1'].includes(url.hostname)
      || url.hostname.endsWith('.localhost')
      || url.protocol === 'data:'
  } catch {
    return false
  }
}

async function waitForSettledTurn(page) {
  await waitFor(
    async () => {
      const button = page.locator('.chat-send-btn.btn--primary')
      return await button.count() === 1 && !await button.isDisabled()
    },
    'terminal chat turn',
    TIMEOUT_MS,
  )
}

async function armAnnotationPicker(page, annotationButton) {
  await annotationButton.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  assert.equal(await annotationButton.isDisabled(), false)
  if (await annotationButton.getAttribute('aria-pressed') !== 'true') {
    await annotationButton.click()
  }
  await page.getByTestId('workbench-annotation-mode-status').waitFor({
    state: 'visible',
    timeout: TIMEOUT_MS,
  })
}

async function previewWebContentsSnapshot(electronApp) {
  return await electronApp.evaluate(async ({ webContents }) => {
    const result = []
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      const url = contents.getURL()
      let heading = null
      try {
        heading = await contents.executeJavaScript(
          "document.querySelector('#editable-heading')?.textContent || null",
          true,
        )
      } catch {}
      result.push({ id: contents.id, type: contents.getType(), url, heading })
    }
    return result
  })
}

async function selectElementInNativePreview(electronApp, selector) {
  return await electronApp.evaluate(async ({ webContents }, targetSelector) => {
    const contents = webContents.getAllWebContents().find(candidate => {
      try {
        const url = new URL(candidate.getURL())
        return url.hostname.endsWith('.localhost')
      } catch {
        return false
      }
    })
    if (!contents) throw new Error('Native HTML preview WebContents was not found.')
    const rect = await contents.executeJavaScript(`(() => {
      const element = document.querySelector(${JSON.stringify(targetSelector)})
      if (!element) return null
      const value = element.getBoundingClientRect()
      return {
        x: value.x,
        y: value.y,
        width: value.width,
        height: value.height,
        tagName: element.tagName.toLowerCase(),
      }
    })()`, true)
    if (!rect) throw new Error('Synthetic target was not found in the native preview.')
    const x = Math.floor(rect.x + Math.max(1, rect.width / 2))
    const y = Math.floor(rect.y + Math.max(1, rect.height / 2))
    contents.focus()
    await contents.debugger.sendCommand('Input.dispatchMouseEvent', {
      type: 'mouseMoved', x, y, button: 'none',
    })
    await contents.debugger.sendCommand('Input.dispatchMouseEvent', {
      type: 'mousePressed', x, y, button: 'left', clickCount: 1,
    })
    await contents.debugger.sendCommand('Input.dispatchMouseEvent', {
      type: 'mouseReleased', x, y, button: 'left', clickCount: 1,
    })
    return { x, y, url: contents.getURL(), tagName: rect.tagName }
  }, selector)
}

async function annotationOverlayState(electronApp) {
  return await electronApp.evaluate(async ({ webContents }) => {
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      try {
        const state = await contents.executeJavaScript(`(() => {
          const body = document.getElementById('annotation-body')
          if (!body) return null
          return {
            body: body.value,
            focused: document.activeElement === body,
            target: document.getElementById('annotation-target')?.textContent || '',
          }
        })()`, true)
        if (state) return { id: contents.id, ...state }
      } catch {}
    }
    return null
  })
}

async function typeAndSubmitAnnotation(electronApp, body) {
  return await electronApp.evaluate(async ({ webContents }, syntheticBody) => {
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      let hasEditor = false
      try {
        hasEditor = await contents.executeJavaScript(
          "Boolean(document.getElementById('annotation-body'))",
          true,
        )
      } catch {}
      if (!hasEditor) continue
      contents.focus()
      await contents.executeJavaScript(
        "document.getElementById('annotation-body').select()",
        true,
      )
      contents.insertText(syntheticBody)
      await contents.executeJavaScript(`(() => {
        const body = document.getElementById('annotation-body')
        body.dispatchEvent(new InputEvent('input', {
          bubbles: true,
          data: body.value,
          inputType: 'insertText',
        }))
      })()`, true)
      const accepted = await contents.executeJavaScript(
        "document.getElementById('annotation-body').value",
        true,
      )
      if (accepted !== syntheticBody) {
        throw new Error('Trusted annotation editor did not accept synthetic keyboard input.')
      }
      contents.sendInputEvent({
        type: 'keyDown',
        keyCode: 'Enter',
        modifiers: process.platform === 'darwin' ? ['meta'] : ['control'],
      })
      contents.sendInputEvent({
        type: 'keyUp',
        keyCode: 'Enter',
        modifiers: process.platform === 'darwin' ? ['meta'] : ['control'],
      })
      return { accepted }
    }
    throw new Error('Trusted annotation overlay WebContents was not found.')
  }, body)
}

const isolationRoot = await mkdtemp(join(tmpdir(), 'opensquilla-v1-html-agent-edit-'))
const userDataDir = join(isolationRoot, 'electron-user-data')
const provider = await startDeterministicProvider()
const gatewayPort = await reserveLoopbackPort()
await seedDesktopCredential(userDataDir, provider.baseUrl)
const developmentElectronRoot = await createDevelopmentElectronRoot(isolationRoot)

let app
let runError
const pageErrors = []
const consoleErrors = []
const externalRequests = []
const evidence = {
  sessionUrl: '',
  selectedPreviewUrl: '',
  versionsAfterApply: '',
  changesAfterApply: '',
  previewHeading: '',
  answerOnlyPreservedCounts: false,
  answerOnlyAnnotatedRequests: 0,
  answerOnlyMutationFinalizations: 0,
  exactDocumentToolRequests: 0,
  toolFreeMutationFinalizations: 0,
  durableMutation: null,
}

try {
  app = await electron.launch({
    args: [
      '--use-mock-keychain',
      `--user-data-dir=${userDataDir}`,
      developmentElectronRoot,
    ],
    env: launchEnvironment(isolationRoot, gatewayPort),
  })
  await app.context().route(url => (
    (url.protocol === 'http:' || url.protocol === 'https:')
    && !isAllowedLoopbackUrl(url.toString())
  ), async route => {
    externalRequests.push(route.request().url())
    await route.abort('blockedbyclient')
  })

  const page = await app.firstWindow({ timeout: STARTUP_TIMEOUT_MS })
  page.on('pageerror', error => pageErrors.push(String(error?.message || error)))
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await waitFor(
    () => page.url().includes('/control/chat'),
    'owned-Gateway Control UI',
    STARTUP_TIMEOUT_MS,
  )
  await page.locator('.conn-pill.connected').waitFor({
    state: 'visible',
    timeout: STARTUP_TIMEOUT_MS,
  })
  await delay(500)
  // Source startup can complete optional session-recovery probes just after the
  // connection indicator appears. They are outside this feature journey; start
  // the renderer-error assertion at the first user interaction boundary.
  pageErrors.length = 0
  consoleErrors.length = 0

  const syntheticHtml = Buffer.from(`<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Synthetic V1 fixture</title></head>
  <body>
    <main>
      <h1 id="editable-heading">${INITIAL_HEADING}</h1>
      <p id="preserved-copy">This byte range must remain unchanged.</p>
    </main>
  </body>
</html>`, 'utf8')
  await page.locator('input[type="file"]').setInputFiles({
    name: 'synthetic-v1-fixture.html',
    mimeType: 'text/html',
    buffer: syntheticHtml,
  })
  await page.locator('.attachment-chip__name').filter({
    hasText: 'synthetic-v1-fixture.html',
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await page.locator('.chat-textarea').fill(INITIAL_MESSAGE)
  await page.locator('.chat-send-btn.btn--primary').click()
  await waitFor(() => /\/control\/chat\?session=/.test(page.url()), 'materialized V1 session', TIMEOUT_MS)
  evidence.sessionUrl = page.url()
  await waitForSettledTurn(page)
  await page.locator('.msg-file-chip__name').filter({
    hasText: 'synthetic-v1-fixture.html',
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })

  const editCopy = page.locator('button[aria-label^="Edit a copy of synthetic-v1-fixture.html"]')
  await waitFor(
    async () => await editCopy.count() === 1 && !await editCopy.isDisabled(),
    'enabled Edit a copy action',
    TIMEOUT_MS,
  )
  await editCopy.click()
  await page.locator('.artifact-document').waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await page.locator('[data-document-section="preview"]').waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await waitFor(async () => {
    const snapshot = await previewWebContentsSnapshot(app)
    return snapshot.some(item => item.heading === INITIAL_HEADING)
  }, 'native HTML preview heading', TIMEOUT_MS)

  const versionsTab = page.getByRole('tab', { name: /Versions/ })
  const changesTab = page.getByRole('tab', { name: /Changes/ })
  assert.match(await versionsTab.innerText(), /1/)
  assert.match(await changesTab.innerText(), /0/)

  const annotationButton = page.getByRole('button', { name: 'Annotate preview' })
  await armAnnotationPicker(page, annotationButton)
  const selected = await selectElementInNativePreview(app, '#editable-heading')
  evidence.selectedPreviewUrl = selected.url
  assert.equal(selected.tagName, 'h1')
  const overlay = await waitFor(
    async () => {
      const state = await annotationOverlayState(app)
      return state?.target === '<h1>' && state.focused ? state : null
    },
    'trusted annotation overlay',
    TIMEOUT_MS,
  )
  assert.equal(overlay.target, '<h1>')
  await typeAndSubmitAnnotation(app, ANNOTATION_BODY)
  await page.locator('.chat-prompt-annotation-chip').filter({
    hasText: ANNOTATION_BODY,
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })

  await page.locator('.chat-textarea').fill(APPLY_MESSAGE)
  await page.locator('.chat-send-btn.btn--primary').click()
  await waitForSettledTurn(page)
  await waitFor(async () => {
    const snapshot = await previewWebContentsSnapshot(app)
    return snapshot.some(item => item.heading === APPLIED_HEADING)
  }, 'updated native preview heading', TIMEOUT_MS)
  evidence.previewHeading = APPLIED_HEADING
  await waitFor(async () => /2/.test(await versionsTab.innerText()), 'Versions = 2', TIMEOUT_MS)
  await waitFor(async () => /1/.test(await changesTab.innerText()), 'Changes = 1', TIMEOUT_MS)
  evidence.versionsAfterApply = await versionsTab.innerText()
  evidence.changesAfterApply = await changesTab.innerText()
  assert.equal(provider.documentApplyCalls(), 1, 'the V1 edit turn must propose one document_apply')

  const mutationFinalizationsBeforeAnswer = provider.requests
    .filter(isMutationFinalizationRequest)
  assert.equal(
    mutationFinalizationsBeforeAnswer.length,
    1,
    'the mutating turn must finish before the answer-only selection starts',
  )

  // Prove that a selected-context turn may answer without entering the durable
  // mutation lifecycle: create a second real annotation against the new head,
  // then let the deterministic provider answer directly while exact4 is visible.
  await armAnnotationPicker(page, annotationButton)
  const answerOnlySelected = await selectElementInNativePreview(app, '#preserved-copy')
  assert.equal(answerOnlySelected.tagName, 'p')
  const answerOnlyOverlay = await waitFor(
    async () => {
      const state = await annotationOverlayState(app)
      return state?.target === '<p>' && state.focused ? state : null
    },
    'trusted answer-only annotation overlay',
    TIMEOUT_MS,
  )
  assert.equal(answerOnlyOverlay.target, '<p>')
  await typeAndSubmitAnnotation(app, ANSWER_ONLY_ANNOTATION_BODY)
  await page.locator('.chat-prompt-annotation-chip').filter({
    hasText: ANSWER_ONLY_ANNOTATION_BODY,
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })

  const answerOnlyRequestStart = provider.requests.length
  await page.locator('.chat-textarea').fill(ANSWER_ONLY_MESSAGE)
  await page.locator('.chat-send-btn.btn--primary').click()
  await waitFor(
    () => provider.requests.length > answerOnlyRequestStart,
    'answer-only provider request',
    TIMEOUT_MS,
  )
  await waitForSettledTurn(page)
  const answerOnlyRequests = provider.requests.slice(answerOnlyRequestStart)
  assert.equal(
    answerOnlyRequests.length,
    1,
    'selected-context answer must complete in one ordinary Agent call',
  )
  assert.deepEqual(
    documentToolNames(answerOnlyRequests[0]),
    EXPECTED_DOCUMENT_TOOLS,
    'the answer-only turn must receive the same exact4 document surface',
  )
  const answerOnlyMutationFinalizations = answerOnlyRequests
    .filter(isMutationFinalizationRequest)
  assert.equal(
    answerOnlyMutationFinalizations.length,
    0,
    'answer-only selected context must not create a mutation outcome finalizer',
  )
  evidence.answerOnlyAnnotatedRequests = answerOnlyRequests.length
  evidence.answerOnlyMutationFinalizations = answerOnlyMutationFinalizations.length
  assert.match(await versionsTab.innerText(), /2/)
  assert.match(await changesTab.innerText(), /1/)
  assert.equal(provider.documentApplyCalls(), 1, 'answer-only follow-up must not commit')
  evidence.answerOnlyPreservedCounts = true

  const documentToolRequests = provider.requests.filter(payload => {
    const names = documentToolNames(payload)
    if (names.length === 0) return false
    assert.deepEqual(names, EXPECTED_DOCUMENT_TOOLS)
    return true
  })
  assert.equal(
    documentToolRequests.length,
    3,
    'inspect, apply, and answer-only legs must each expose exact4',
  )
  evidence.exactDocumentToolRequests = documentToolRequests.length

  const mutationFinalizations = provider.requests.filter(isMutationFinalizationRequest)
  assert.equal(mutationFinalizations.length, 1, 'apply must end with one mutation finalization')
  assert.equal(
    Array.isArray(mutationFinalizations[0].tools)
      ? mutationFinalizations[0].tools.length
      : 0,
    0,
    'mutation finalization must be tool-free',
  )
  evidence.toolFreeMutationFinalizations = mutationFinalizations.length

  await versionsTab.click()
  assert.equal(await page.locator('.artifact-document__versions > li').count(), 2)
  await changesTab.click()
  assert.equal(await page.locator('[data-document-section="changes"] li').count(), 1)

  assert.equal(
    await page.locator('[data-testid="chat-session-action-workbench"]').count(),
    0,
    'V1 must not expose the internal Workbench resource count as a top-level action',
  )
  assert.equal(
    await page.locator('[data-artifact-action="publish-head"]').count(),
    0,
    'V1 must not expose publication workflow in the HTML editing surface',
  )
  assert.equal(externalRequests.length, 0, 'the offline V1 journey must not use external network')
  assert.equal(pageErrors.length, 0, `renderer page errors: ${pageErrors.join(' | ')}`)
  assert.equal(consoleErrors.length, 0, `renderer console errors: ${consoleErrors.join(' | ')}`)
} catch (error) {
  runError = error
} finally {
  await app?.close().catch(() => {})
  if (!runError) {
    try {
      evidence.durableMutation = await readDurableMutationEvidence(isolationRoot)
      assert.deepEqual(evidence.durableMutation, {
        appliedAttempts: 1,
        annotations: 2,
        attemptHasChangeSet: true,
        attemptHasRevision: true,
        attemptLinksCommittedObjects: 1,
        attemptStatus: 'applied',
        changeSets: 1,
        documents: 1,
        mutationAttempts: 1,
        revisions: 2,
        sentAnnotations: 2,
      })
    } catch (error) {
      runError = error
    }
  }
  await provider.close().catch(() => {})
  await delay(100)
  if (runError) {
    console.error(JSON.stringify({
      ok: false,
      error: String(runError?.stack || runError),
      isolationRoot,
      pageErrors,
      consoleErrors,
      externalRequests,
    }, null, 2))
  }
  if (!runError && process.env.OPENSQUILLA_KEEP_V1_E2E_PROFILE !== '1') {
    await rm(isolationRoot, { recursive: true, force: true }).catch(() => {})
  }
}

if (runError) throw runError

console.log(JSON.stringify({
  ok: true,
  fixture: 'synthetic-v1-html-agent-edit',
  providerRequests: provider.requests.length,
  documentApplyCalls: provider.documentApplyCalls(),
  evidence,
}, null, 2))
