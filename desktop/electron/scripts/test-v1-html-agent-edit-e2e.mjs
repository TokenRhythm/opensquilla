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
const GENERATED_FILENAME = 'synthetic-v1-generated.html'
const INITIAL_HEADING = 'Synthetic draft heading'
const MANUAL_HEADING = 'Manual V1 heading'
const APPLIED_HEADING = 'Agent-patched V1 heading'
const PATCHED_TITLE = 'Patched V1 fixture'
const PRESERVED_COPY = 'This byte range must remain unchanged.'
const GENERATED_HTML = `<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Synthetic V1 fixture</title></head>
  <body>
    <main>
      <h1 id="editable-heading">${INITIAL_HEADING}</h1>
      <p id="preserved-copy">This byte range must remain unchanged.</p>
    </main>
  </body>
</html>`
const MANUAL_HTML = GENERATED_HTML.replace(INITIAL_HEADING, MANUAL_HEADING)
const UNRELATED_HTML = MANUAL_HTML.replace(
  '    </main>',
  '      <!-- unrelated source change -->\n    </main>',
)
const DUPLICATED_TARGET_OPENING = '<p id="preserved-copy">'
const DUPLICATED_TARGET_HTML = UNRELATED_HTML.replace(
  '      <p id="preserved-copy">This byte range must remain unchanged.</p>',
  `      <section>${DUPLICATED_TARGET_OPENING}This byte range must remain unchanged.</p></section>
      <aside>${DUPLICATED_TARGET_OPENING}This byte range must remain unchanged.</p></aside>`,
)
const ANNOTATION_BODY = 'Explain this synthetic paragraph without editing it.'
const SECOND_ANNOTATION_BODY = 'Keep this heading concise and accessible.'
const FOLLOW_UP_ANNOTATION_BODY = 'Confirm the heading remains readable after the first answer.'
const AMBIGUOUS_ANNOTATION_BODY = 'Remove only the paragraph I selected.'
const GENERATE_MESSAGE = 'Create and publish the requested synthetic single-file HTML page.'
const ANNOTATION_MESSAGE = 'Answer the selected annotations without changing the document.'
const AMBIGUOUS_MESSAGE = 'Try the selected page update, but do not guess between duplicate targets.'
const PATCH_MESSAGE = 'Update the current document heading and page style, then save it.'
const EXPECTED_ANNOTATION_TOOLS = [
  'document_apply',
  'document_inspect',
  'document_locate',
  'document_patch',
  'document_read',
]
const EXPECTED_CURRENT_DOCUMENT_TOOLS = [
  'document_patch',
  'document_read',
]
const TIMEOUT_MS = 60_000
// A cold desktop profile performs recovery discovery before it starts the
// source Gateway. Keep functional assertions at 60 seconds, but allow this
// one-time startup phase to complete on slower CI and developer machines.
const STARTUP_TIMEOUT_MS = 180_000
const MANUAL_SETUP_TIMEOUT_MS = 30 * 60_000
const MANUAL_MODE = process.env.OPENSQUILLA_MANUAL_V1_HTML_EDIT === '1'
const MANUAL_REAL_PROVIDER = process.env.OPENSQUILLA_MANUAL_REAL_PROVIDER === '1'
const MANUAL_REUSE_PROFILE = process.env.OPENSQUILLA_MANUAL_V1_PROFILE_ROOT?.trim() || ''
const TEST_WINDOW_WIDTH = 1_440
const TEST_WINDOW_HEIGHT = 900
const MANUAL_TEST_WINDOW_WIDTH = 1_440
const MANUAL_TEST_WINDOW_HEIGHT = 900
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
  const currentUserMessage = [...messages]
    .reverse()
    .find(message => message?.role === 'user')
  try {
    const finalization = JSON.parse(messageText(currentUserMessage?.content))
    return !Array.isArray(payload?.tools)
      && typeof finalization?.language === 'string'
      && typeof finalization?.status === 'string'
      && Object.keys(finalization).every(key => key === 'language' || key === 'status')
  } catch {
    return false
  }
}

function matchingToolNames(payload, expected) {
  return (Array.isArray(payload?.tools) ? payload.tools : [])
    .map(item => item?.function?.name)
    .filter(name => expected.includes(name))
    .sort()
}

function annotationToolNames(payload) {
  return matchingToolNames(payload, EXPECTED_ANNOTATION_TOOLS)
}

function currentDocumentToolNames(payload) {
  return matchingToolNames(payload, EXPECTED_CURRENT_DOCUMENT_TOOLS)
}

function currentTurn(payload) {
  const messages = Array.isArray(payload?.messages) ? payload.messages : []
  let userIndex = -1
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'user') {
      userIndex = index
      break
    }
  }
  return {
    userText: userIndex >= 0 ? messageText(messages[userIndex]?.content) : '',
    toolMessages: messages.slice(userIndex + 1).filter(message => message?.role === 'tool'),
  }
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
  let documentPatchCalls = 0
  let contextualLocateCalls = 0
  let contextualCandidateErrors = 0
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
      const turn = currentTurn(payload)
      const toolMessages = turn.toolMessages
      const latestToolMessage = toolMessages.at(-1)
      const latestToolCallId = String(latestToolMessage?.tool_call_id || '')
      const latestToolName = String(latestToolMessage?.name || '')
        || (latestToolCallId.includes('write_generated_html') ? 'write_file' : '')
        || (latestToolCallId.includes('publish_generated_html') ? 'publish_artifact' : '')
        || (latestToolCallId.includes('document_read') ? 'document_read' : '')
        || (latestToolCallId.includes('document_patch') ? 'document_patch' : '')
        || (latestToolCallId.includes('document_locate') ? 'document_locate' : '')
      const isGenerationTurn = turn.userText.includes(GENERATE_MESSAGE)
      const isAmbiguousTurn = turn.userText.includes(AMBIGUOUS_MESSAGE)
      const hasAnnotationTools = toolNames.has('document_inspect')
        || toolNames.has('document_apply')
      const hasCurrentDocumentTools = toolNames.has('document_patch')
      const isMetadataRequest = payload.stream === false && toolNames.size === 0
      let bodyChunks

      if (isMetadataRequest) {
        bodyChunks = openAiTextChunks(model, 'Synthetic HTML document')
      } else if (isMutationFinalizationRequest(payload)) {
        bodyChunks = openAiTextChunks(
          model,
          'TheUserInstructions {"documentMutationOutcome":{"status":"applied"}} '
            + 'User(synthetic internal control text)',
        )
      } else if (isGenerationTurn && toolMessages.length === 0) {
        assert.ok(
          toolNames.has('write_file'),
          `generation must expose write_file; received: ${[...toolNames].sort().join(', ')}`,
        )
        bodyChunks = openAiToolChunks(
          model,
          'call_write_generated_html_v1_e2e',
          'write_file',
          { path: GENERATED_FILENAME, content: GENERATED_HTML },
        )
      } else if (isGenerationTurn && latestToolName === 'write_file') {
        assert.ok(toolNames.has('publish_artifact'), 'generation must expose publish_artifact')
        bodyChunks = openAiToolChunks(
          model,
          'call_publish_generated_html_v1_e2e',
          'publish_artifact',
          {
            path: GENERATED_FILENAME,
            name: GENERATED_FILENAME,
            mime: 'text/html',
            bundle: 'none',
          },
        )
      } else if (isGenerationTurn) {
        bodyChunks = openAiTextChunks(model, 'The generated HTML file is ready.')
      } else if (isAmbiguousTurn && toolMessages.length === 0) {
        bodyChunks = openAiToolChunks(
          model,
          'call_contextual_document_read_v1_e2e',
          'document_read',
          { view: 'source', max_chars: 16_384 },
        )
      } else if (isAmbiguousTurn && latestToolName === 'document_read') {
        const read = jsonFromToolContent(latestToolMessage?.content)
        assert.equal(read.status, 'ok')
        assert.equal(read.hasMore, false)
        assert.equal(
          String(read.chunk?.text || '').split(DUPLICATED_TARGET_OPENING).length - 1,
          2,
          'the contextual fixture must contain two indistinguishable candidates',
        )
        contextualLocateCalls += 1
        bodyChunks = openAiToolChunks(
          model,
          `call_contextual_document_locate_${contextualLocateCalls}_v1_e2e`,
          'document_locate',
          {
            annotation_order: 0,
            operation: 'remove_node',
            candidateSource: DUPLICATED_TARGET_OPENING,
          },
        )
      } else if (isAmbiguousTurn && latestToolName === 'document_locate') {
        assert.match(
          String(latestToolMessage?.content || ''),
          /DOCUMENT_CANDIDATE_INVALID/,
          'a duplicated contextual candidate must be rejected without a grant',
        )
        contextualCandidateErrors += 1
        if (contextualLocateCalls < 2) {
          contextualLocateCalls += 1
          bodyChunks = openAiToolChunks(
            model,
            `call_contextual_document_locate_${contextualLocateCalls}_v1_e2e`,
            'document_locate',
            {
              annotation_order: 0,
              operation: 'remove_node',
              candidateSource: DUPLICATED_TARGET_OPENING,
            },
          )
        } else {
          bodyChunks = openAiTextChunks(
            model,
            'The selected page area could not be located uniquely, so no update was made.',
          )
        }
      } else if (hasAnnotationTools) {
        bodyChunks = openAiTextChunks(
          model,
          'The selected synthetic paragraph explains preserved fixture content.',
        )
      } else if (hasCurrentDocumentTools && toolMessages.length === 0) {
        bodyChunks = openAiToolChunks(
          model,
          'call_document_read_v1_e2e',
          'document_read',
          { view: 'source', max_chars: 16_384 },
        )
      } else if (hasCurrentDocumentTools && latestToolName === 'document_read') {
        const read = jsonFromToolContent(latestToolMessage?.content)
        assert.equal(read.status, 'ok')
        assert.equal(read.hasMore, false, 'the single-file fixture must fit in one source page')
        assert.match(String(read.chunk?.text || ''), new RegExp(MANUAL_HEADING))
        documentPatchCalls += 1
        bodyChunks = openAiToolChunks(
          model,
          'call_document_patch_v1_e2e',
          'document_patch',
          {
            expectedSha256: read.sha256,
            edits: [
              { expectedText: MANUAL_HEADING, replacement: APPLIED_HEADING },
              {
                expectedText: '<title>Synthetic V1 fixture</title>',
                replacement: `<title>${PATCHED_TITLE}</title>`,
              },
              {
                expectedText: '<body>',
                replacement: '<body style="background: #f6f7fb;">',
              },
            ],
          },
        )
      } else if (hasCurrentDocumentTools && latestToolName === 'document_patch') {
        bodyChunks = openAiTextChunks(model, 'The current HTML document was updated.')
      } else {
        bodyChunks = openAiTextChunks(model, 'Synthetic fixture acknowledged.')
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
    documentPatchCalls: () => documentPatchCalls,
    contextualLocateCalls: () => contextualLocateCalls,
    contextualCandidateErrors: () => contextualCandidateErrors,
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
  const root = join(isolationRoot, `electron-source-shell-${process.pid}`)
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
import hashlib
from pathlib import Path
import sqlite3
import sys

database_path = sys.argv[1]
connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row

def scalar(query):
    return int(connection.execute(query).fetchone()[0])

document = connection.execute(
    """
    SELECT document_id, head_revision_id, generation
    FROM artifact_documents
    """
).fetchone()
binding = connection.execute(
    """
    SELECT document_id, source_type, source_resource_id, source_sha256
    FROM document_source_bindings
    """
).fetchone()
revision_one = connection.execute(
    """
    SELECT artifact_id, artifact_sha256
    FROM artifact_revisions
    WHERE generation = 1
    """
).fetchone()
head = connection.execute(
    """
    SELECT artifact_sha256
    FROM artifact_revisions
    WHERE revision_id = (SELECT head_revision_id FROM artifact_documents LIMIT 1)
    """
).fetchone()
original_payload_sha256 = None
if binding is not None:
    artifact_root = Path(database_path).parent.parent / "media" / "artifacts"
    for marker in artifact_root.rglob(".artifact-id"):
        if marker.read_text(encoding="utf-8").strip() == binding["source_resource_id"]:
            original_payload_sha256 = hashlib.sha256(
                (marker.parent / "data").read_bytes()
            ).hexdigest()
            break
evidence = {
    "documents": scalar("SELECT COUNT(*) FROM artifact_documents"),
    "revisions": scalar("SELECT COUNT(*) FROM artifact_revisions"),
    "changeSets": scalar("SELECT COUNT(*) FROM artifact_change_sets"),
    "mutationAttempts": scalar("SELECT COUNT(*) FROM artifact_mutation_attempts"),
    "sourceBindings": scalar("SELECT COUNT(*) FROM document_source_bindings"),
    "deliverableBindings": scalar(
        "SELECT COUNT(*) FROM document_source_bindings WHERE source_type = 'deliverable'"
    ),
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
    "allAttemptsLinked": scalar(
        """
        SELECT COUNT(*)
        FROM artifact_mutation_attempts
        WHERE change_set_id IS NOT NULL AND revision_id IS NOT NULL
        """
    ),
    "documentGeneration": int(document["generation"]) if document is not None else 0,
    "bindingTargetsDocument": bool(
        binding is not None
        and document is not None
        and binding["document_id"] == document["document_id"]
    ),
    "revisionOneReusesDeliverable": bool(
        binding is not None
        and revision_one is not None
        and binding["source_resource_id"] == revision_one["artifact_id"]
        and binding["source_sha256"] == revision_one["artifact_sha256"]
    ),
    "originalDeliverableUnchanged": bool(
        binding is not None
        and revision_one is not None
        and binding["source_sha256"] == revision_one["artifact_sha256"]
    ),
    "originalDeliverableBytesMatch": bool(
        binding is not None
        and original_payload_sha256 == binding["source_sha256"]
    ),
    "headDiffersFromOriginal": bool(
        binding is not None
        and head is not None
        and binding["source_sha256"] != head["artifact_sha256"]
    ),
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
  const environment = {
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
  if (MANUAL_REAL_PROVIDER) {
    delete environment.OPENSQUILLA_DESKTOP_SECRET_STORAGE
  }
  return environment
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

async function submitChatComposer(page) {
  const button = page.locator('.chat-send-btn.btn--primary')
  await waitFor(
    async () => {
      if (await button.count() !== 1 || !await button.isVisible() || await button.isDisabled()) {
        return false
      }
      return (await button.getAttribute('class'))?.includes('is-ready') === true
    },
    'ready chat send action',
    TIMEOUT_MS,
  )
  await button.evaluate(element => element.click())
}

function generatedArtifactCard(page) {
  return page.locator('.msg-artifact-chip').filter({ hasText: GENERATED_FILENAME })
}

async function openDeliverablesFromHeader(page) {
  const wideAction = page.getByTestId('chat-session-action-deliverables')
  const compactAction = page.locator(
    '[data-testid="chat-header-primary-action"][data-action="deliverables"]',
  )
  const menuTrigger = page.getByTestId('chat-session-actions-trigger')

  await waitFor(
    async () => await wideAction.isVisible()
      || await compactAction.isVisible()
      || await menuTrigger.isVisible(),
    'responsive deliverables header action',
    TIMEOUT_MS,
  )
  if (await wideAction.isVisible()) {
    await wideAction.click()
    return
  }
  if (await compactAction.isVisible()) {
    await compactAction.click()
    return
  }

  await menuTrigger.click()
  await wideAction.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await wideAction.click()
}

async function openGeneratedArtifact(page) {
  const card = generatedArtifactCard(page)
  await card.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await card.locator('.msg-artifact-body').click()
  await page.locator('.artifact-document').waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  const previewTab = page.getByRole('tab', { name: /^Preview/ })
  await waitFor(
    async () => await previewTab.getAttribute('aria-selected') === 'true',
    'Preview selected on artifact open',
    TIMEOUT_MS,
  )
  await waitForReadyArtifactPreview(page)
}

async function waitForReadyArtifactPreview(page) {
  await page.locator('[data-document-section="preview"]').waitFor({
    state: 'visible',
    timeout: TIMEOUT_MS,
  })
  await page.locator('.artifact-preview[data-preview-state="ready"]').waitFor({
    state: 'visible',
    timeout: TIMEOUT_MS,
  })
}

function normalizeRenderedSource(value) {
  return String(value || '')
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .trim()
}

async function sourceEditorSnapshot(page) {
  const studio = page.locator('.artifact-html-studio')
  if (await studio.count() === 0) {
    return {
      editorPresent: false,
      inputPresent: false,
      renderedSource: '',
    }
  }
  return await studio.evaluate(section => {
    const editor = section.querySelector('.monaco-editor')
    const inputs = editor?.querySelectorAll('textarea.inputarea, .native-edit-context') || []
    const input = inputs.length ? inputs[inputs.length - 1] : null
    const inputKind = input instanceof HTMLTextAreaElement
      ? 'textarea'
      : input?.classList.contains('native-edit-context')
        ? 'native-edit-context'
        : null
    const inputReadOnly = input instanceof HTMLTextAreaElement ? input.readOnly : null
    const ariaAutocomplete = input?.getAttribute('aria-autocomplete')
    const inputEditable = inputKind === 'textarea'
      ? inputReadOnly === false
      : inputKind === 'native-edit-context'
        ? ariaAutocomplete === 'both' || ariaAutocomplete === 'list'
        : false
    const active = document.activeElement
    const save = section.querySelector('.artifact-html-studio__action')
    const status = section.querySelector('.artifact-html-studio__status')
    const renderedSource = editor?.querySelector('.view-lines')?.innerText || ''
    return {
      editorPresent: Boolean(editor),
      inputPresent: Boolean(input),
      inputKind,
      inputEditable,
      inputReadOnly,
      ariaReadOnly: input?.getAttribute('aria-readonly'),
      ariaAutocomplete,
      focused: Boolean(input && active === input),
      activeElement: active instanceof HTMLElement
        ? {
            tag: active.tagName.toLowerCase(),
            className: String(active.className || ''),
            role: active.getAttribute('role'),
            ariaLabel: active.getAttribute('aria-label'),
          }
        : null,
      status: status?.getAttribute('data-state'),
      statusText: status?.textContent?.trim() || '',
      saveDisabled: save instanceof HTMLButtonElement ? save.disabled : null,
      error: section.querySelector('.artifact-html-studio__error')?.textContent?.trim() || '',
      renderedSource,
      renderedSourceLength: renderedSource.length,
    }
  })
}

async function waitForEditableSourceEditor(page, expectedSourceText) {
  const editor = page.locator('.artifact-html-studio .monaco-editor')
  await editor.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  let lastSnapshot = {}
  try {
    await waitFor(async () => {
      lastSnapshot = await sourceEditorSnapshot(page)
      const sourceLoaded = normalizeRenderedSource(lastSnapshot.renderedSource)
        .includes(normalizeRenderedSource(expectedSourceText))
      return lastSnapshot.editorPresent
        && lastSnapshot.inputPresent
        && lastSnapshot.inputEditable
        && lastSnapshot.ariaReadOnly !== 'true'
        && sourceLoaded
        && !lastSnapshot.error
    }, 'loaded and editable source editor', TIMEOUT_MS)

    await editor.click()
    const input = editor.locator('textarea.inputarea, .native-edit-context').last()
    await input.focus()
    await waitFor(async () => {
      lastSnapshot = await sourceEditorSnapshot(page)
      return lastSnapshot.focused
        && lastSnapshot.inputEditable
        && lastSnapshot.ariaReadOnly !== 'true'
    }, 'focused editable Monaco input', 5_000)
    return lastSnapshot
  } catch (error) {
    throw new Error(`${error.message}; editor snapshot: ${JSON.stringify(lastSnapshot)}`)
  }
}

function changedSourceLines(previousSource, nextSource) {
  const previousLines = new Set(
    previousSource.split('\n').map(line => normalizeRenderedSource(line)).filter(Boolean),
  )
  const changed = nextSource
    .split('\n')
    .map(line => normalizeRenderedSource(line))
    .filter(line => line && !previousLines.has(line))
  assert.ok(changed.length > 0, 'replacement source must change at least one rendered model line')
  return changed
}

async function openGeneratedArtifactSource(page, expectedSourceText = INITIAL_HEADING) {
  await openGeneratedArtifact(page)
  const sourceTab = page.getByRole('tab', { name: /^Source/ })
  await sourceTab.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await sourceTab.click()
  await waitFor(
    async () => await sourceTab.getAttribute('aria-selected') === 'true',
    'Source selected after explicit user action',
    TIMEOUT_MS,
  )
  await waitForEditableSourceEditor(page, expectedSourceText)
}

async function replaceSourceInEditor(page, source, previousSource, expectedCurrentText) {
  const editor = page.locator('.artifact-html-studio .monaco-editor')
  const before = await waitForEditableSourceEditor(page, expectedCurrentText)
  const input = editor.locator('textarea.inputarea, .native-edit-context').last()
  await input.focus()
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A')
  await page.keyboard.insertText(source)
  const save = page.locator('.artifact-html-studio__action')
  const expectedChangedLines = changedSourceLines(previousSource, source)
  let changedSnapshot = {}
  try {
    await waitFor(async () => {
      changedSnapshot = await sourceEditorSnapshot(page)
      const rendered = normalizeRenderedSource(changedSnapshot.renderedSource)
      return changedSnapshot.status === 'dirty'
        && changedSnapshot.saveDisabled === false
        && expectedChangedLines.every(line => rendered.includes(line))
        && normalizeRenderedSource(changedSnapshot.renderedSource)
          !== normalizeRenderedSource(before.renderedSource)
    }, 'changed Monaco model and dirty source editor', 10_000)
  } catch (error) {
    throw new Error(`${error.message}; editor snapshot: ${JSON.stringify(changedSnapshot)}`)
  }
  assert.equal(await save.isDisabled(), false, 'changed Monaco model must enable Save')
  await save.click()
  await waitFor(
    async () => await page.locator('.artifact-html-studio__status').getAttribute('data-state') === 'saved',
    'saved source editor',
    TIMEOUT_MS,
  )
}

async function gatewayHealthSnapshot(port) {
  const url = `http://127.0.0.1:${port}/health`
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(2_000) })
    const body = await response.text()
    return {
      url,
      reachable: true,
      status: response.status,
      body: body.slice(0, 2_000),
    }
  } catch (error) {
    return {
      url,
      reachable: false,
      error: String(error?.message || error),
    }
  }
}

async function diagnosticCall(label, operation, timeoutMs = 3_000) {
  const controller = new AbortController()
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      delay(timeoutMs, undefined, { signal: controller.signal }).then(() => {
        throw new Error(`${label} timed out after ${timeoutMs}ms`)
      }),
    ])
  } finally {
    controller.abort()
  }
}

async function captureFailureEvidence({
  app,
  page,
  error,
  gatewayPort,
  isolationRoot,
  userDataDir,
  pageErrors,
  consoleErrors,
}) {
  const reportRoot = process.env.CI_REPORT_DIR?.trim()
    || join(isolationRoot, 'failure-evidence')
  await mkdir(reportRoot, { recursive: true })

  const rawAttempt = process.env.OPENSQUILLA_DESKTOP_E2E_ATTEMPT
    || process.env.GITHUB_RUN_ATTEMPT
    || '1'
  const attempt = rawAttempt.replace(/[^a-zA-Z0-9._-]/g, '_')
  const reportStem = `v1-html-agent-edit-failure-attempt-${attempt}-${Date.now()}`

  // Capture process, Gateway, and on-disk logs first. Renderer inspection is
  // best-effort because a frozen renderer is one of the failures diagnosed by
  // this report and must not prevent the durable evidence from being written.
  let electronProcess = null
  try {
    const child = app?.process()
    if (child) {
      electronProcess = {
        pid: child.pid,
        exitCode: child.exitCode,
        signalCode: child.signalCode,
        killed: child.killed,
      }
    }
  } catch (caught) {
    electronProcess = { error: String(caught?.message || caught) }
  }

  const gateway = await gatewayHealthSnapshot(gatewayPort)
  const copiedLogs = {}
  for (const name of ['desktop.log', 'gateway.log']) {
    const source = join(userDataDir, 'logs', name)
    const destination = join(reportRoot, `${reportStem}-${name}`)
    try {
      await cp(source, destination)
      copiedLogs[name] = destination
    } catch (caught) {
      copiedLogs[name] = { error: String(caught?.message || caught), source }
    }
  }

  let renderer
  let screenshot = null
  if (page && !page.isClosed()) {
    try {
      const shell = await diagnosticCall('renderer shell snapshot', () => page.evaluate(() => {
        const connection = document.querySelector('.conn-pill')
        const active = document.activeElement
        return {
          url: location.href,
          connection: connection
            ? {
                className: String(connection.className || ''),
                text: connection.textContent?.trim() || '',
              }
            : null,
          activeElement: active instanceof HTMLElement
            ? {
                tag: active.tagName.toLowerCase(),
                className: String(active.className || ''),
                role: active.getAttribute('role'),
                ariaLabel: active.getAttribute('aria-label'),
              }
            : null,
        }
      }))
      renderer = {
        available: true,
        ...shell,
      }
    } catch (caught) {
      renderer = {
        available: false,
        shellError: String(caught?.message || caught),
      }
    }
    try {
      renderer.sourceEditor = await diagnosticCall(
        'source editor snapshot',
        () => sourceEditorSnapshot(page),
      )
    } catch (caught) {
      renderer.sourceEditorError = String(caught?.message || caught)
    }
    try {
      const screenshotPath = join(reportRoot, `${reportStem}.png`)
      await diagnosticCall(
        'failure screenshot',
        () => page.screenshot({ path: screenshotPath, fullPage: true, timeout: 3_000 }),
      )
      screenshot = screenshotPath
    } catch (caught) {
      renderer.screenshotError = String(caught?.message || caught)
    }
  } else {
    renderer = {
      available: false,
      error: 'renderer page is unavailable or already closed',
    }
  }

  const captured = {
    capturedAt: new Date().toISOString(),
    beforeElectronClose: true,
    error: String(error?.stack || error),
    isolationRoot,
    renderer,
    electronProcess,
    gateway,
    pageErrors: [...pageErrors],
    consoleErrors: [...consoleErrors],
    screenshot,
    copiedLogs,
  }
  const reportPath = join(reportRoot, `${reportStem}.json`)
  await writeFile(reportPath, `${JSON.stringify(captured, null, 2)}\n`, 'utf8')
  return { ...captured, reportPath }
}

async function verifyPatchedSourceRemainsEditable(page, versionsTab, changesTab) {
  const sourceTab = page.getByRole('tab', { name: /^Source/ })
  await sourceTab.click()
  const editor = page.locator('.artifact-html-studio .monaco-editor')
  await editor.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  const status = page.locator('.artifact-html-studio__status')
  let lastSourceSnapshot = {}
  try {
    await waitFor(async () => {
      const state = await status.getAttribute('data-state')
      const renderedSource = await page.locator('.artifact-html-studio .view-lines').innerText()
      // Monaco renders indentation with non-breaking spaces and may leave
      // spacing between separately tokenized spans. Normalize only that
      // presentation layer; the source must still prove all three R3 edits.
      const source = renderedSource.replace(/\u00a0/g, ' ')
      const error = await page.locator('.artifact-html-studio__error').allInnerTexts()
      lastSourceSnapshot = { state, source, error }
      if (state !== 'ready' && state !== 'saved') return false
      return source.includes(APPLIED_HEADING)
        && source.includes(PATCHED_TITLE)
        && /background:\s*#f6f7fb;/.test(source)
    }, 'latest source loaded without a conflict', TIMEOUT_MS)
  } catch (error) {
    throw new Error(`${error.message}; source snapshot: ${JSON.stringify(lastSourceSnapshot)}`)
  }

  assert.equal(await page.locator('.artifact-html-studio__error').count(), 0)
  assert.equal(await page.getByTestId('copy-unsaved-source').count(), 0)
  assert.equal(await page.getByTestId('discard-and-load-latest').count(), 0)
  const save = page.locator('.artifact-html-studio__action')
  assert.equal(await save.isDisabled(), true, 'latest source must start clean')

  // Prove that the model-advanced head did not strand Monaco in read-only
  // mode. Undo the probe before autosave, then let the pending timer drain;
  // the durable revision/change counts must remain unchanged.
  await editor.click()
  await page.keyboard.insertText('__opensquilla_edit_probe__')
  await waitFor(
    async () => await status.getAttribute('data-state') === 'dirty',
    'latest source remains editable',
    TIMEOUT_MS,
  )
  assert.equal(await save.isDisabled(), false)
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Z' : 'Control+Z')
  await waitFor(async () => {
    const state = await status.getAttribute('data-state')
    return (state === 'ready' || state === 'saved') && await save.isDisabled()
  }, 'latest edit probe restored a clean buffer', TIMEOUT_MS)
  await delay(1_500)
  assert.match(await versionsTab.innerText(), /5/)
  assert.match(await changesTab.innerText(), /4/)
  assert.equal(await page.locator('.artifact-html-studio__error').count(), 0)
}

async function installOfflineRequestGuard(electronApp, externalRequests) {
  await electronApp.context().route(url => (
    (url.protocol === 'http:' || url.protocol === 'https:')
    && !isAllowedLoopbackUrl(url.toString())
  ), async route => {
    externalRequests.push(route.request().url())
    await route.abort('blockedbyclient')
  })
}

async function armAnnotationPicker(page, annotationButton) {
  await annotationButton.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  assert.equal(await annotationButton.isDisabled(), false)
  if (await annotationButton.getAttribute('aria-pressed') !== 'true') {
    await annotationButton.click()
  }
  await assertAnnotationPickerArmed(
    page,
    page.getByRole('button', { name: 'Stop annotating' }),
  )
}

async function assertAnnotationPickerArmed(page, annotationButton) {
  await waitFor(
    async () => await annotationButton.getAttribute('aria-pressed') === 'true',
    'annotation picker pressed',
    TIMEOUT_MS,
  )
  const status = page.getByTestId('workbench-annotation-mode-status')
  await status.waitFor({ state: 'attached', timeout: TIMEOUT_MS })
  const layout = await status.evaluate(element => {
    const host = element.closest('.workbench-host')
    return {
      hostWidth: host?.getBoundingClientRect().width ?? Number.POSITIVE_INFINITY,
      statusDisplay: getComputedStyle(element).display,
    }
  })
  if (layout.hostWidth <= 520.5) {
    assert.equal(layout.statusDisplay, 'none')
  } else {
    assert.notEqual(layout.statusDisplay, 'none')
  }
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
      result.push({
        id: contents.id,
        type: contents.getType(),
        url: url.startsWith('data:') ? 'data:[redacted]' : url,
        heading,
      })
    }
    return result
  })
}

async function selectElementInNativePreview(electronApp, selector, expectedSourceMarker) {
  return await electronApp.evaluate(async ({ webContents }, input) => {
    const { targetSelector, expectedMarker } = input
    let contents = null
    const candidates = []
    for (const candidate of webContents.getAllWebContents()) {
      try {
        const url = new URL(candidate.getURL())
        const owner = candidate.getOwnerBrowserWindow()
        const view = owner?.contentView.children.find(item => (
          item.webContents?.id === candidate.id
        ))
        const snapshot = {
          id: candidate.id,
          url: candidate.getURL().startsWith('data:')
            ? `${candidate.getURL().slice(0, 32)}…`
            : candidate.getURL(),
          host: url.hostname,
          visible: view?.getVisible?.() === true,
          bounds: view?.getBounds?.() || null,
          ownerDestroyed: owner?.isDestroyed?.() ?? null,
        }
        if (!url.hostname.endsWith('.localhost')) {
          candidates.push(snapshot)
          continue
        }
        if (view?.getVisible?.() !== true) {
          candidates.push(snapshot)
          continue
        }
        if (expectedMarker) {
          const source = await candidate.executeJavaScript(
            'document.documentElement?.outerHTML || ""',
            true,
          )
          snapshot.markerMatched = source.includes(expectedMarker)
          if (!snapshot.markerMatched) {
            candidates.push(snapshot)
            continue
          }
        }
        contents = candidate
        break
      } catch {
        continue
      }
    }
    if (!contents) {
      throw new Error(`Native HTML preview WebContents was not found: ${JSON.stringify(candidates)}`)
    }
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
  }, { targetSelector: selector, expectedMarker: expectedSourceMarker })
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
            newlineHint: document.getElementById('annotation-newline-hint')?.textContent || '',
          }
        })()`, true)
        if (state) {
          const owner = contents.getOwnerBrowserWindow()
          const view = owner?.contentView.children.find(candidate => (
            candidate.webContents?.id === contents.id
          ))
          const visible = view?.getVisible?.() === true
          if (!visible) continue
          return {
            id: contents.id,
            visible,
            ...state,
          }
        }
      } catch {}
    }
    return null
  })
}

async function waitForFreshAnnotationOverlay(electronApp, expectedTarget, label) {
  return await waitFor(
    async () => {
      const state = await annotationOverlayState(electronApp)
      return state?.visible
        && state.focused
        && state.body === ''
        && state.target.includes(expectedTarget)
        ? state
        : null
    },
    label,
    TIMEOUT_MS,
  )
}

async function typeAndSubmitAnnotation(electronApp, body) {
  return await electronApp.evaluate(async ({ webContents }, syntheticBody) => {
    for (const contents of webContents.getAllWebContents()) {
      if (contents.isDestroyed()) continue
      let hasEditor = false
      try {
        const owner = contents.getOwnerBrowserWindow()
        const view = owner?.contentView.children.find(candidate => (
          candidate.webContents?.id === contents.id
        ))
        if (view?.getVisible?.() !== true) continue
        hasEditor = await contents.executeJavaScript(
          "document.activeElement?.id === 'annotation-body'",
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
      })
      contents.sendInputEvent({
        type: 'keyUp',
        keyCode: 'Enter',
      })
      return { accepted }
    }
    throw new Error('Trusted annotation overlay WebContents was not found.')
  }, body)
}

async function dropNextChatSend(page) {
  await page.evaluate(() => {
    window.__opensquillaV1DroppedChatSend = false
    const originalSend = WebSocket.prototype.send
    WebSocket.prototype.send = function dropSyntheticSessionsSend(data) {
      if (
        typeof data === 'string'
        && data.includes('"method":"chat.send"')
      ) {
        WebSocket.prototype.send = originalSend
        window.__opensquillaV1DroppedChatSend = true
        this.close(4000, 'synthetic sessions.send disconnect')
        return
      }
      return originalSend.call(this, data)
    }
  })
}

const isolationRoot = MANUAL_REUSE_PROFILE
  || await mkdtemp(join(tmpdir(), 'opensquilla-v1-html-agent-edit-'))
const userDataDir = join(isolationRoot, 'electron-user-data')
const provider = MANUAL_REAL_PROVIDER ? null : await startDeterministicProvider()
const gatewayPort = await reserveLoopbackPort()
const manualDebugPort = MANUAL_MODE ? await reserveLoopbackPort() : null
if (provider) await seedDesktopCredential(userDataDir, provider.baseUrl)
const developmentElectronRoot = await createDevelopmentElectronRoot(isolationRoot)

let app
let activePage
let runError
let failureEvidence
const pageErrors = []
const consoleErrors = []
const externalRequests = []
const evidence = {
  sessionUrl: '',
  selectedPreviewUrl: '',
  versionsAfterManualSave: '',
  versionsAfterUnrelatedSave: '',
  versionsAfterAgentPatch: '',
  changesAfterAgentPatch: '',
  previewHeading: '',
  annotationModeExitedAfterAcceptance: false,
  annotationSendFailureRetained: false,
  annotationAcceptanceEvents: [],
  annotationModeAfterAcceptance: null,
  annotationRequests: 0,
  followUpAnnotationRequests: 0,
  followUpAnnotationModeExited: false,
  annotationsPrepared: 0,
  contextualLocateCalls: 0,
  contextualCandidateErrors: 0,
  ambiguousRevisionStayedPut: false,
  currentDocumentToolRequests: 0,
  toolFreeMutationFinalizations: 0,
  recoveredRevisionCount: 0,
  logicalResourceCount: 0,
  originalDownloadEndpointVerified: false,
  patchedSourceRemainedEditable: false,
  manualPreviewSnapshot: [],
  nativeAnnotationSurfaceEvents: [],
  durableMutation: null,
}

try {
  desktopJourney: {
  app = await electron.launch({
    args: [
      ...(MANUAL_REAL_PROVIDER ? [] : ['--use-mock-keychain']),
      ...(manualDebugPort ? [`--remote-debugging-port=${manualDebugPort}`] : []),
      `--user-data-dir=${userDataDir}`,
      developmentElectronRoot,
    ],
    env: launchEnvironment(isolationRoot, gatewayPort),
  })
  if (!MANUAL_REAL_PROVIDER) {
    await installOfflineRequestGuard(app, externalRequests)
  }

  const page = await app.firstWindow({ timeout: STARTUP_TIMEOUT_MS })
  activePage = page
  const testWindowWidth = MANUAL_MODE
    ? MANUAL_TEST_WINDOW_WIDTH
    : TEST_WINDOW_WIDTH
  const testWindowHeight = MANUAL_MODE
    ? MANUAL_TEST_WINDOW_HEIGHT
    : TEST_WINDOW_HEIGHT
  await app.evaluate(({ BrowserWindow }, bounds) => {
    BrowserWindow.getAllWindows()[0]?.setSize(bounds.width, bounds.height)
  }, {
    width: testWindowWidth,
    height: testWindowHeight,
  })
  await page.waitForFunction(
    width => window.innerWidth <= width,
    testWindowWidth,
  )
  page.on('pageerror', error => pageErrors.push(String(error?.message || error)))
  page.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  // A fresh real-provider profile intentionally opens native setup before the
  // Gateway-backed Control UI exists. Report that setup is interactive, but do
  // not claim the feature client is ready until the Gateway and renderer are
  // both connected.
  if (MANUAL_MODE && MANUAL_REAL_PROVIDER) {
    console.log(JSON.stringify({
      ready: false,
      phase: 'provider-setup',
      isolationRoot,
      gatewayPort,
      debugPort: manualDebugPort,
      credentials: MANUAL_REUSE_PROFILE
        ? 'existing isolated Desktop credential retained'
        : 'none preconfigured; enter the API key in Desktop settings',
      next: 'The harness will report ready only after the Control UI is connected.',
    }, null, 2))
  }
  await waitFor(
    () => page.url().includes('/control/chat'),
    'owned-Gateway Control UI',
    MANUAL_MODE && MANUAL_REAL_PROVIDER ? MANUAL_SETUP_TIMEOUT_MS : STARTUP_TIMEOUT_MS,
  )
  await page.locator('.conn-pill.connected').waitFor({
    state: 'visible',
    timeout: MANUAL_MODE && MANUAL_REAL_PROVIDER ? MANUAL_SETUP_TIMEOUT_MS : STARTUP_TIMEOUT_MS,
  })
  await delay(500)
  // Source startup can complete optional session-recovery probes just after the
  // connection indicator appears. They are outside this feature journey; start
  // the renderer-error assertion at the first user interaction boundary.
  pageErrors.length = 0
  consoleErrors.length = 0
  await page.evaluate(() => {
    window.__opensquillaV1NativeAnnotationSurfaceEvents = []
    window.opensquillaDesktop?.onWorkbenchSurfaceEvent?.((payload) => {
      if (!payload || typeof payload !== 'object') return
      const event = payload
      if (!String(event.type || '').startsWith('annotation-')) return
      window.__opensquillaV1NativeAnnotationSurfaceEvents.push({
        type: event.type,
        detail: event.detail,
      })
    })
  })

  if (MANUAL_MODE && MANUAL_REAL_PROVIDER) {
    console.log(JSON.stringify({
      ready: true,
      mode: 'manual-real-provider',
      branch: 'feature/artifact-prompt-annotations',
      isolationRoot,
      gatewayPort,
      debugPort: manualDebugPort,
      instruction: 'The real-provider HTML workflow is ready for manual testing.',
      shutdown: 'Close the Electron window.',
    }, null, 2))
    const electronProcess = app.process()
    if (electronProcess.exitCode === null) {
      await new Promise(resolveExit => electronProcess.once('exit', resolveExit))
    }
    break desktopJourney
  }

  if (MANUAL_MODE && MANUAL_REUSE_PROFILE) {
    console.log(JSON.stringify({
      ready: true,
      mode: 'manual-v1-html-agent-edit-recovery',
      branch: 'feature/artifact-prompt-annotations',
      isolationRoot,
      providerBaseUrl: provider.baseUrl,
      gatewayPort,
      debugPort: manualDebugPort,
      instruction: 'Open the existing session and verify its recovered document history.',
      shutdown: 'Close the Electron window.',
    }, null, 2))
    const electronProcess = app.process()
    if (electronProcess.exitCode === null) {
      await new Promise(resolveExit => electronProcess.once('exit', resolveExit))
    }
    break desktopJourney
  }

  await page.locator('.chat-textarea').fill(GENERATE_MESSAGE)
  await submitChatComposer(page)
  await waitFor(() => /\/control\/chat\?session=/.test(page.url()), 'materialized V1 session', TIMEOUT_MS)
  evidence.sessionUrl = page.url()
  await waitForSettledTurn(page)
  await generatedArtifactCard(page).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  assert.equal(await generatedArtifactCard(page).count(), 1, 'generation must show one HTML card')
  assert.equal(
    await page.locator('button[aria-label^="Edit a copy of "]').count(),
    0,
    'generated HTML must not expose the old editable-copy action',
  )
  assert.equal(
    await page.getByText(/Create (an )?editable copy/i).count(),
    0,
    'generated HTML must not expose an editable-copy CTA',
  )
  await openGeneratedArtifactSource(page)

  if (MANUAL_MODE) {
    console.log(JSON.stringify({
      ready: true,
      mode: 'manual-v1-html-agent-edit',
      branch: 'feature/artifact-prompt-annotations',
      fixture: GENERATED_FILENAME,
      isolationRoot,
      providerBaseUrl: provider.baseUrl,
      gatewayPort,
      debugPort: manualDebugPort,
      exactPrompts: {
        annotation: ANNOTATION_MESSAGE,
        patch: PATCH_MESSAGE,
      },
      exactAnnotationBody: ANNOTATION_BODY,
      shutdown: 'Close the Electron window.',
    }, null, 2))
    const electronProcess = app.process()
    if (electronProcess.exitCode === null) {
      await new Promise(resolveExit => electronProcess.once('exit', resolveExit))
    }
    break desktopJourney
  }

  const versionsTab = page.getByRole('tab', { name: /Versions/ })
  const changesTab = page.getByRole('tab', { name: /Changes/ })
  assert.match(await versionsTab.innerText(), /1/)
  assert.match(await changesTab.innerText(), /0/)

  await replaceSourceInEditor(page, MANUAL_HTML, GENERATED_HTML, INITIAL_HEADING)
  await waitFor(async () => /2/.test(await versionsTab.innerText()), 'Versions = 2', TIMEOUT_MS)
  await waitFor(async () => /1/.test(await changesTab.innerText()), 'Changes = 1', TIMEOUT_MS)
  evidence.versionsAfterManualSave = await versionsTab.innerText()

  const previewTab = page.getByRole('tab', { name: /^Preview/ })
  await previewTab.click()
  await waitForReadyArtifactPreview(page)
  await waitFor(async () => {
    const snapshot = await previewWebContentsSnapshot(app)
    evidence.manualPreviewSnapshot = snapshot
    return snapshot.some(item => item.heading === MANUAL_HEADING)
  }, 'manually saved native HTML preview heading', TIMEOUT_MS)

  const annotationButton = page.getByRole('button', { name: 'Annotate preview' })
  await armAnnotationPicker(page, annotationButton)
  const selected = await selectElementInNativePreview(
    app,
    '#preserved-copy',
    MANUAL_HEADING,
  )
  evidence.selectedPreviewUrl = selected.url
  assert.equal(selected.tagName, 'p')
  const overlay = await waitForFreshAnnotationOverlay(
    app,
    PRESERVED_COPY,
    'trusted annotation overlay',
  )
  assert.doesNotMatch(overlay.target, /^<[^>]+>$/)
  assert.equal(
    overlay.newlineHint,
    process.platform === 'darwin'
      ? '⇧ Return for a new line'
      : 'Shift + Enter for a new line',
  )
  await typeAndSubmitAnnotation(app, ANNOTATION_BODY)
  await page.locator('.chat-prompt-annotation-chip').filter({
    hasText: ANNOTATION_BODY,
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await waitFor(async () => {
    const state = await annotationOverlayState(app)
    return !state?.visible
  }, 'first annotation editor acknowledged and closed', TIMEOUT_MS)
  const activeAnnotationButton = page.getByRole('button', { name: 'Stop annotating' })
  await activeAnnotationButton.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  assert.equal(
    await activeAnnotationButton.getAttribute('aria-pressed'),
    'true',
    'adding a draft must keep element selection active until the chat send is accepted',
  )
  await assertAnnotationPickerArmed(page, activeAnnotationButton)

  // Move the head without touching the selected paragraph. Draft targets must
  // follow the current page automatically, and the continuous picker intent
  // must survive the native preview rebuild.
  const sourceTab = page.getByRole('tab', { name: /^Source/ })
  await sourceTab.click()
  await replaceSourceInEditor(page, UNRELATED_HTML, MANUAL_HTML, MANUAL_HEADING)
  await waitFor(async () => /3/.test(await versionsTab.innerText()), 'Versions = 3', TIMEOUT_MS)
  await waitFor(async () => /2/.test(await changesTab.innerText()), 'Changes = 2', TIMEOUT_MS)
  evidence.versionsAfterUnrelatedSave = await versionsTab.innerText()
  await previewTab.click()
  await waitForReadyArtifactPreview(page)
  const restoredAnnotationButton = page.getByRole('button', { name: 'Stop annotating' })
  await assertAnnotationPickerArmed(page, restoredAnnotationButton)
  assert.equal(
    await restoredAnnotationButton.getAttribute('aria-pressed'),
    'true',
    'an unrelated source change must not make the user restart annotation mode',
  )

  const secondSelected = await selectElementInNativePreview(
    app,
    '#editable-heading',
    'unrelated source change',
  )
  assert.equal(secondSelected.tagName, 'h1')
  const secondOverlay = await waitForFreshAnnotationOverlay(
    app,
    MANUAL_HEADING,
    'second trusted annotation overlay',
  )
  assert.doesNotMatch(secondOverlay.target, /^<[^>]+>$/)
  await typeAndSubmitAnnotation(app, SECOND_ANNOTATION_BODY)
  try {
    await page.locator('.chat-prompt-annotation-chip').filter({
      hasText: SECOND_ANNOTATION_BODY,
    }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  } catch (error) {
    evidence.nativeAnnotationSurfaceEvents = await page.evaluate(
      () => window.__opensquillaV1NativeAnnotationSurfaceEvents || [],
    )
    throw error
  }
  evidence.nativeAnnotationSurfaceEvents = await page.evaluate(
    () => window.__opensquillaV1NativeAnnotationSurfaceEvents || [],
  )
  evidence.annotationsPrepared = await page.locator('.chat-prompt-annotation-chip').count()
  assert.equal(evidence.annotationsPrepared, 2, 'continuous selection must collect two drafts')
  assert.equal(
    await page.getByRole('button', { name: 'Stop annotating' }).getAttribute('aria-pressed'),
    'true',
    'adding another draft must keep annotation mode active before send acceptance',
  )

  await page.evaluate(() => {
    window.__opensquillaV1AnnotationAcceptanceEvents = []
    window.addEventListener('opensquilla:artifact-prompt-annotations-accepted', (event) => {
      window.__opensquillaV1AnnotationAcceptanceEvents.push(event.detail)
    })
  })
  const annotationRequestStart = provider.requests.length
  await page.locator('.chat-textarea').fill(ANNOTATION_MESSAGE)
  await dropNextChatSend(page)
  await submitChatComposer(page)
  await page.waitForFunction(
    () => window.__opensquillaV1DroppedChatSend === true,
    undefined,
    { timeout: TIMEOUT_MS },
  )
  await page.locator('.conn-pill.connected').waitFor({
    state: 'visible',
    timeout: TIMEOUT_MS,
  })
  await waitForSettledTurn(page)
  assert.equal(
    await page.locator('.chat-textarea').inputValue(),
    ANNOTATION_MESSAGE,
    'a disconnected send must retain the user message for retry',
  )
  assert.equal(
    await page.locator('.chat-prompt-annotation-chip').count(),
    2,
    'a disconnected send must retain every prepared annotation',
  )
  assert.equal(
    await page.getByRole('button', { name: 'Stop annotating' }).getAttribute('aria-pressed'),
    'true',
    'a disconnected send must keep the continuous picker pressed',
  )
  assert.equal(
    await page.evaluate(() => (
      window.__opensquillaV1AnnotationAcceptanceEvents || []
    ).length),
    0,
    'a disconnected send must not publish an acceptance event',
  )
  evidence.annotationSendFailureRetained = true

  await submitChatComposer(page)
  await waitFor(
    () => provider.requests.slice(annotationRequestStart)
      .some(payload => annotationToolNames(payload).length > 0),
    'annotation document-tool provider request',
    TIMEOUT_MS,
  )
  await waitForSettledTurn(page)
  await waitFor(
    async () => {
      const state = await page.evaluate(() => ({
        acceptedEvents: window.__opensquillaV1AnnotationAcceptanceEvents || [],
        statusCount: document.querySelectorAll(
          '[data-testid="workbench-annotation-mode-status"]',
        ).length,
        stopButtonCount: [...document.querySelectorAll('button')]
          .filter(button => button.getAttribute('aria-label') === 'Stop annotating')
          .length,
      }))
      evidence.annotationAcceptanceEvents = state.acceptedEvents
      evidence.annotationModeAfterAcceptance = {
        statusCount: state.statusCount,
        stopButtonCount: state.stopButtonCount,
      }
      return state.stopButtonCount === 0 && state.statusCount === 0
    },
    'annotation mode exit after accepted send',
    TIMEOUT_MS,
  )
  await page.getByTestId('workbench-annotation-mode-status').waitFor({
    state: 'detached',
    timeout: TIMEOUT_MS,
  })
  assert.equal(
    await page.locator('.chat-prompt-annotation-chip').filter({ hasText: ANNOTATION_BODY }).count(),
    0,
    'accepted annotation must leave the composer',
  )
  assert.equal(
    await page.locator('.chat-prompt-annotation-chip').count(),
    0,
    'all accepted annotations must leave the composer together',
  )
  evidence.annotationModeExitedAfterAcceptance = true
  assert.match(await versionsTab.innerText(), /3/)
  assert.match(await changesTab.innerText(), /2/)

  const annotationRequests = provider.requests.slice(annotationRequestStart)
    .filter(payload => annotationToolNames(payload).length > 0)
  assert.equal(annotationRequests.length, 1, 'annotation answer must use one document-tool request')
  assert.deepEqual(annotationToolNames(annotationRequests[0]), EXPECTED_ANNOTATION_TOOLS)
  evidence.annotationRequests = annotationRequests.length

  // Regression for the reported flow: after one accepted selection/question,
  // arm the picker again, create a new selection, and send it as a second
  // independent turn. The acceptance handoff must not leave the native picker
  // pressed or consume the second annotation as ordinary chat text.
  await armAnnotationPicker(page, page.getByRole('button', { name: 'Annotate preview' }))
  await waitForReadyArtifactPreview(page)
  const followUpSelected = await selectElementInNativePreview(
    app,
    '#editable-heading',
    MANUAL_HEADING,
  )
  assert.equal(followUpSelected.tagName, 'h1')
  await waitForFreshAnnotationOverlay(
    app,
    MANUAL_HEADING,
    'follow-up trusted annotation overlay',
  )
  await typeAndSubmitAnnotation(app, FOLLOW_UP_ANNOTATION_BODY)
  await page.locator('.chat-prompt-annotation-chip').filter({
    hasText: FOLLOW_UP_ANNOTATION_BODY,
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await waitFor(async () => !await annotationOverlayState(app),
    'follow-up annotation editor acknowledged and closed', TIMEOUT_MS)
  const followUpRequestStart = provider.requests.length
  await page.locator('.chat-textarea').fill(ANNOTATION_MESSAGE)
  await submitChatComposer(page)
  await waitFor(
    () => provider.requests.slice(followUpRequestStart)
      .some(payload => annotationToolNames(payload).length > 0),
    'follow-up annotation document-tool provider request',
    TIMEOUT_MS,
  )
  await waitForSettledTurn(page)
  await waitFor(
    async () => {
      const state = await page.evaluate(() => ({
        statusCount: document.querySelectorAll(
          '[data-testid="workbench-annotation-mode-status"]',
        ).length,
        stopButtonCount: [...document.querySelectorAll('button')]
          .filter(button => button.getAttribute('aria-label') === 'Stop annotating')
          .length,
      }))
      return state.stopButtonCount === 0 && state.statusCount === 0
    },
    'follow-up annotation mode exit after accepted send',
    TIMEOUT_MS,
  )
  assert.equal(
    await page.locator('.chat-prompt-annotation-chip').count(),
    0,
    'the second accepted annotation must leave the composer',
  )
  const followUpAnnotationRequests = provider.requests.slice(followUpRequestStart)
    .filter(payload => annotationToolNames(payload).length > 0)
  assert.equal(followUpAnnotationRequests.length, 1)
  assert.deepEqual(annotationToolNames(followUpAnnotationRequests[0]), EXPECTED_ANNOTATION_TOOLS)
  evidence.followUpAnnotationRequests = followUpAnnotationRequests.length
  evidence.followUpAnnotationModeExited = true

  // Create a new selection, then move the original element and introduce two
  // indistinguishable candidates. The accepted turn may ask AI to help, but a
  // repeated ambiguous candidate must never mint a grant or advance history.
  await armAnnotationPicker(page, page.getByRole('button', { name: 'Annotate preview' }))
  const ambiguousSelected = await selectElementInNativePreview(
    app,
    '#preserved-copy',
    MANUAL_HEADING,
  )
  assert.equal(ambiguousSelected.tagName, 'p')
  await waitForFreshAnnotationOverlay(
    app,
    PRESERVED_COPY,
    'contextual annotation overlay',
  )
  await typeAndSubmitAnnotation(app, AMBIGUOUS_ANNOTATION_BODY)
  await page.locator('.chat-prompt-annotation-chip').filter({
    hasText: AMBIGUOUS_ANNOTATION_BODY,
  }).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  await waitFor(async () => !await annotationOverlayState(app),
    'contextual annotation editor acknowledged and closed', TIMEOUT_MS)

  await sourceTab.click()
  await replaceSourceInEditor(
    page,
    DUPLICATED_TARGET_HTML,
    UNRELATED_HTML,
    'unrelated source change',
  )
  await waitFor(async () => /4/.test(await versionsTab.innerText()), 'Versions = 4', TIMEOUT_MS)
  await waitFor(async () => /3/.test(await changesTab.innerText()), 'Changes = 3', TIMEOUT_MS)
  await previewTab.click()
  await waitForReadyArtifactPreview(page)
  await assertAnnotationPickerArmed(
    page,
    page.getByRole('button', { name: 'Stop annotating' }),
  )

  const ambiguousRequestStart = provider.requests.length
  await page.locator('.chat-textarea').fill(AMBIGUOUS_MESSAGE)
  await submitChatComposer(page)
  await waitFor(
    () => provider.contextualCandidateErrors() === 2,
    'two rejected contextual candidates',
    TIMEOUT_MS,
  )
  await waitForSettledTurn(page)
  evidence.contextualLocateCalls = provider.contextualLocateCalls()
  evidence.contextualCandidateErrors = provider.contextualCandidateErrors()
  assert.equal(evidence.contextualLocateCalls, 2)
  assert.equal(evidence.contextualCandidateErrors, 2)
  assert.equal(
    provider.requests.slice(ambiguousRequestStart)
      .filter(isMutationFinalizationRequest).length,
    0,
    'ambiguous candidates must not reach mutation finalization',
  )
  assert.match(await versionsTab.innerText(), /4/)
  assert.match(await changesTab.innerText(), /3/)
  assert.equal(
    await page.locator('.chat-prompt-annotation-chip').count(),
    0,
    'the accepted contextual annotation leaves the composer without guessing a target',
  )
  await page.getByTestId('workbench-annotation-mode-status').waitFor({
    state: 'detached',
    timeout: TIMEOUT_MS,
  })
  evidence.ambiguousRevisionStayedPut = true

  const patchRequestStart = provider.requests.length
  await page.locator('.chat-textarea').fill(PATCH_MESSAGE)
  await submitChatComposer(page)
  await waitFor(() => provider.documentPatchCalls() === 1, 'document_patch proposal', TIMEOUT_MS)
  await waitForSettledTurn(page)
  const mutationTurnText = await page.locator('.msg-ai').last().innerText()
  assert.doesNotMatch(
    mutationTurnText,
    /TheUserInstructions|documentMutationOutcome|synthetic internal control/i,
    'model echoes of the mutation finalizer must never cross the user-visible boundary',
  )
  assert.match(mutationTurnText, /Page updated|document changes were applied/i)
  await waitFor(async () => /5/.test(await versionsTab.innerText()), 'Versions = 5', TIMEOUT_MS)
  await waitFor(async () => /4/.test(await changesTab.innerText()), 'Changes = 4', TIMEOUT_MS)
  evidence.versionsAfterAgentPatch = await versionsTab.innerText()
  evidence.changesAfterAgentPatch = await changesTab.innerText()
  await verifyPatchedSourceRemainsEditable(page, versionsTab, changesTab)
  evidence.patchedSourceRemainedEditable = true
  await previewTab.click()
  await waitForReadyArtifactPreview(page)
  await waitFor(async () => {
    const snapshot = await previewWebContentsSnapshot(app)
    return snapshot.some(item => item.heading === APPLIED_HEADING)
  }, 'agent-patched native preview heading', TIMEOUT_MS)
  evidence.previewHeading = APPLIED_HEADING

  const currentDocumentToolRequests = provider.requests.slice(patchRequestStart).filter(payload => {
    const names = currentDocumentToolNames(payload)
    if (names.length === 0) return false
    assert.deepEqual(names, EXPECTED_CURRENT_DOCUMENT_TOOLS)
    return true
  })
  assert.equal(
    currentDocumentToolRequests.length,
    2,
    'document_read and document_patch legs must expose the current-document tools',
  )
  evidence.currentDocumentToolRequests = currentDocumentToolRequests.length

  const mutationFinalizations = provider.requests.slice(patchRequestStart)
    .filter(isMutationFinalizationRequest)
  assert.equal(mutationFinalizations.length, 1, 'document_patch must finalize once')
  assert.equal(
    Array.isArray(mutationFinalizations[0].tools)
      ? mutationFinalizations[0].tools.length
      : 0,
    0,
    'mutation finalization must be tool-free',
  )
  evidence.toolFreeMutationFinalizations = mutationFinalizations.length

  // Electron owns download UX, so assert the authenticated fetch before the
  // shell receives the generated Blob. Its URL must remain the immutable chat
  // artifact endpoint rather than the current Document head endpoint.
  const originalDownloadPromise = page.waitForResponse(response => {
    try {
      return response.request().method() === 'GET'
        && new URL(response.url()).pathname.startsWith('/api/v1/artifacts/')
    } catch {
      return false
    }
  })
  const originalDownloadButton = generatedArtifactCard(page).locator('.msg-artifact-download')
  await originalDownloadButton.waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  assert.equal(await originalDownloadButton.isDisabled(), false)
  await originalDownloadButton.evaluate(element => element.click())
  const originalDownload = await originalDownloadPromise
  assert.equal(originalDownload.status(), 200)
  const originalDownloadPath = new URL(originalDownload.url()).pathname
  assert.match(
    originalDownloadPath,
    /^\/api\/v1\/artifacts\/[^/]+$/,
    'chat-card download must keep using the immutable artifact endpoint',
  )
  evidence.originalDownloadEndpointVerified = true

  await versionsTab.click()
  assert.equal(await page.locator('.artifact-document__versions > li').count(), 5)
  await changesTab.click()
  assert.equal(await page.locator('[data-document-section="changes"] li').count(), 4)

  assert.equal(
    await page.getByTestId('workbench-artifact-switcher').count(),
    0,
    'one logical generated HTML resource must not create a duplicate switcher entry',
  )
  await openDeliverablesFromHeader(page)
  const logicalResources = page.locator('.resource-collection__item')
  await logicalResources.first().waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  evidence.logicalResourceCount = await logicalResources.count()
  assert.equal(
    evidence.logicalResourceCount,
    1,
    'resource navigation must fold the generated deliverable into its bound Document',
  )
  assert.match(await logicalResources.first().innerText(), new RegExp(GENERATED_FILENAME))
  await logicalResources.first().locator('.resource-collection__open').click()
  await waitFor(
    async () => await previewTab.getAttribute('aria-selected') === 'true',
    'Preview selected from resource navigation',
    TIMEOUT_MS,
  )
  await waitForReadyArtifactPreview(page)

  assert.equal(
    await page.locator('[data-testid="chat-session-action-workbench"]').count(),
    0,
    'the internal Workbench resource count must stay out of top-level actions',
  )
  assert.equal(
    await page.locator('[data-artifact-action="publish-head"]').count(),
    0,
    'V1 must not expose publication workflow in the HTML editing surface',
  )
  assert.equal(externalRequests.length, 0, 'the offline V1 journey must not use external network')
  assert.equal(pageErrors.length, 0, `renderer page errors: ${pageErrors.join(' | ')}`)
  assert.equal(consoleErrors.length, 0, `renderer console errors: ${consoleErrors.join(' | ')}`)

  await app.close()
  app = undefined
  activePage = undefined
  await delay(1_000)
  pageErrors.length = 0
  consoleErrors.length = 0

  app = await electron.launch({
    args: ['--use-mock-keychain', `--user-data-dir=${userDataDir}`, developmentElectronRoot],
    env: launchEnvironment(isolationRoot, gatewayPort),
  })
  await installOfflineRequestGuard(app, externalRequests)
  const recoveredPage = await app.firstWindow({ timeout: STARTUP_TIMEOUT_MS })
  activePage = recoveredPage
  recoveredPage.on('pageerror', error => pageErrors.push(String(error?.message || error)))
  recoveredPage.on('console', message => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  await waitFor(
    () => recoveredPage.url().includes('/control/chat'),
    'restarted owned-Gateway Control UI',
    STARTUP_TIMEOUT_MS,
  )
  await recoveredPage.locator('.conn-pill.connected').waitFor({
    state: 'visible',
    timeout: STARTUP_TIMEOUT_MS,
  })
  const recoveredSessionKey = new URL(evidence.sessionUrl).searchParams.get('session')
  assert.ok(recoveredSessionKey, 'generated session key must survive restart')
  const recoveredSessionRow = recoveredPage.locator(
    `.sidebar-history-row[data-session-key="${recoveredSessionKey}"]`,
  )
  await recoveredSessionRow.waitFor({ state: 'visible', timeout: STARTUP_TIMEOUT_MS })
  await delay(500)
  // Start recovery assertions after optional startup probes have settled, then
  // reopen through the user's persisted Recents entry instead of racing the
  // router with a synthetic page navigation.
  pageErrors.length = 0
  consoleErrors.length = 0
  await recoveredSessionRow.locator('.sidebar-history-item').click()
  await waitFor(
    () => new URL(recoveredPage.url()).searchParams.get('session') === recoveredSessionKey,
    'recovered session selected from Recents',
    TIMEOUT_MS,
  )
  await generatedArtifactCard(recoveredPage).waitFor({ state: 'visible', timeout: TIMEOUT_MS })
  assert.equal(await generatedArtifactCard(recoveredPage).count(), 1)
  await openGeneratedArtifactSource(recoveredPage, APPLIED_HEADING)
  const recoveredVersionsTab = recoveredPage.getByRole('tab', { name: /Versions/ })
  await waitFor(
    async () => /5/.test(await recoveredVersionsTab.innerText()),
    'five recovered versions',
    TIMEOUT_MS,
  )
  evidence.recoveredRevisionCount = 5
  await recoveredPage.getByRole('tab', { name: /^Preview/ }).click()
  await waitForReadyArtifactPreview(recoveredPage)
  await waitFor(async () => {
    const snapshot = await previewWebContentsSnapshot(app)
    return snapshot.some(item => item.heading === APPLIED_HEADING)
  }, 'recovered patched preview', TIMEOUT_MS)
  assert.equal(pageErrors.length, 0, `restart renderer page errors: ${pageErrors.join(' | ')}`)
  assert.equal(consoleErrors.length, 0, `restart renderer console errors: ${consoleErrors.join(' | ')}`)
  }
} catch (error) {
  runError = error
} finally {
  if (!runError && !MANUAL_MODE) {
    try {
      evidence.durableMutation = await readDurableMutationEvidence(isolationRoot)
      assert.deepEqual(evidence.durableMutation, {
        allAttemptsLinked: 4,
        annotations: 4,
        appliedAttempts: 4,
        attemptLinksCommittedObjects: 4,
        bindingTargetsDocument: true,
        changeSets: 4,
        deliverableBindings: 1,
        documentGeneration: 5,
        documents: 1,
        headDiffersFromOriginal: true,
        mutationAttempts: 4,
        originalDeliverableBytesMatch: true,
        originalDeliverableUnchanged: true,
        revisionOneReusesDeliverable: true,
        revisions: 5,
        sentAnnotations: 4,
        sourceBindings: 1,
      })
    } catch (error) {
      runError = error
    }
  }
  if (runError) {
    try {
      failureEvidence = await captureFailureEvidence({
        app,
        page: activePage,
        error: runError,
        gatewayPort,
        isolationRoot,
        userDataDir,
        pageErrors,
        consoleErrors,
      })
    } catch (error) {
      failureEvidence = {
        capturedAt: new Date().toISOString(),
        beforeElectronClose: true,
        captureError: String(error?.stack || error),
      }
    }
  }
  try {
    if (app) {
      await diagnosticCall('Electron shutdown', () => app.close(), 15_000)
    }
  } catch (error) {
    const shutdownError = new Error(
      `Electron did not shut down cleanly: ${String(error?.message || error)}`,
    )
    if (!runError) {
      runError = shutdownError
      try {
        failureEvidence = await captureFailureEvidence({
          app,
          page: activePage,
          error: runError,
          gatewayPort,
          isolationRoot,
          userDataDir,
          pageErrors,
          consoleErrors,
        })
      } catch (captureError) {
        failureEvidence = {
          capturedAt: new Date().toISOString(),
          beforeElectronClose: true,
          captureError: String(captureError?.stack || captureError),
        }
      }
    } else if (failureEvidence) {
      failureEvidence.shutdownError = String(shutdownError.stack || shutdownError)
    }
    try {
      app?.process()?.kill()
    } catch {}
  }
  await provider?.close().catch(() => {})
  await delay(100)
  if (runError) {
    console.error(JSON.stringify({
      ok: false,
      error: String(runError?.stack || runError),
      isolationRoot,
      pageErrors,
      consoleErrors,
      externalRequests,
      failureEvidence,
      evidence,
    }, null, 2))
  }
  if (!runError && process.env.OPENSQUILLA_KEEP_V1_E2E_PROFILE !== '1') {
    await rm(isolationRoot, { recursive: true, force: true }).catch(() => {})
  }
}

if (runError) throw runError

console.log(JSON.stringify({
  ok: true,
  fixture: MANUAL_REAL_PROVIDER ? 'manual-real-provider' : 'synthetic-v1-html-agent-edit',
  providerRequests: provider?.requests.length ?? null,
  documentPatchCalls: provider?.documentPatchCalls() ?? null,
  evidence,
}, null, 2))
