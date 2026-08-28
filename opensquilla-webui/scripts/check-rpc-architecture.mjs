import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { walkFiles } from './lib/fs-walk.mjs'
import {
  callMemberReceiverText,
  callMemberReferenceReceiverText,
  destructuredCallSourceText,
  generatedContractImportViolation,
  isDirectCallMemberReference,
  isKnownNonRpcCallReceiver,
  isRpcCapabilityReceiverText,
  moduleReferenceSpecifier,
} from './lib/rpc-architecture-imports.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const srcRoot = join(root, 'src')
const require = createRequire(import.meta.url)
const ts = require('typescript')

// Exact debt ledger for direct `.call(...)` invocations that predate the
// Contract migration. Receiver names are deliberately irrelevant, element
// access is included, and destructuring `call` is forbidden. New calls belong
// behind an Adapter. When a legacy caller moves, lowering or removing its
// count is mandatory: stale entries fail this gate.
const legacyRawRpcCalls = new Map([
  ['src/App.vue', 5],
  ['src/components/CommandPalette.vue', 1],
  ['src/components/ProjectWorkspacePickerDialog.vue', 3],
  ['src/components/SupportDiagnosticsMenu.vue', 1],
  ['src/components/chat/PromptCacheKeepaliveDialog.vue', 2],
  ['src/components/settings/DataMigrationPanel.vue', 2],
  ['src/components/settings/SettingsMemoryPanel.vue', 9],
  ['src/composables/channels/useChannelEditor.ts', 2],
  ['src/composables/channels/useChannelMembers.ts', 6],
  ['src/composables/chat/useChatApprovals.ts', 3],
  ['src/composables/chat/useChatFeatureToggles.ts', 7],
  ['src/composables/chat/useChatGoals.ts', 5],
  ['src/composables/chat/useChatHistory.ts', 1],
  ['src/composables/chat/useChatMetaDraftRecovery.ts', 1],
  ['src/composables/chat/useChatPendingQueue.ts', 6],
  ['src/composables/chat/useChatPlans.ts', 4],
  ['src/composables/chat/useChatRouteFeedback.ts', 1],
  ['src/composables/chat/useChatRunModePreference.ts', 5],
  ['src/composables/chat/useChatSend.ts', 9],
  ['src/composables/chat/useChatSessionRouting.ts', 2],
  ['src/composables/chat/useChatSessionSubscription.ts', 7],
  ['src/composables/chat/useChatSlashCommands.ts', 6],
  ['src/composables/chat/useChatUsageWidget.ts', 2],
  ['src/composables/chat/useMetaRuns.ts', 5],
  ['src/composables/chat/useMetaSkillSetup.ts', 1],
  ['src/composables/chat/useSandboxSetupRecovery.ts', 1],
  ['src/composables/chat/useSessionArtifacts.ts', 1],
  ['src/composables/cron/useCronForm.ts', 1],
  ['src/composables/cron/useCronJobs.ts', 3],
  ['src/composables/cron/useCronRuns.ts', 1],
  ['src/composables/sessions/useSessionInspect.ts', 3],
  ['src/composables/settings/useSandboxSettings.ts', 10],
  ['src/composables/setup/channelRpc.ts', 2],
  ['src/composables/setup/useMemoryLearningSettings.ts', 4],
  ['src/composables/setup/useSetupCatalog.ts', 30],
  ['src/composables/setup/useSetupProviderForm.ts', 2],
  ['src/composables/skills/useSkillDetailController.ts', 1],
  ['src/composables/skills/useSkillProposals.ts', 10],
  ['src/composables/skills/useSkillRegistry.ts', 5],
  ['src/composables/skills/useSkillsCatalog.ts', 1],
  ['src/composables/usage/useUsageQuery.ts', 3],
  ['src/composables/useAgentOptions.ts', 2],
  ['src/composables/useProjectWorkspaces.ts', 6],
  ['src/composables/useRequest.ts', 1],
  ['src/composables/useRpc.ts', 2],
  ['src/composables/useSessionListSubscription.ts', 4],
  ['src/stores/app.ts', 1],
  ['src/stores/sandboxSetup.ts', 2],
  ['src/views/AgentsView.vue', 3],
  ['src/views/ChannelsView.vue', 8],
  ['src/views/ChatView.vue', 8],
  ['src/views/LogsView.vue', 2],
  ['src/views/OverviewView.vue', 3],
  ['src/views/SessionsView.vue', 2],
  ['src/views/SkillsView.vue', 1],
  ['src/workbench/artifactDocumentProvider.ts', 1],
  ['src/workbench/artifactPromptAnnotationProvider.ts', 1],
  ['src/workbench/workbenchResourceProvider.ts', 1],
])

const legacySessionsListLiterals = new Map()
const legacyRawRpcCallCapabilities = new Map([
  ['src/composables/chat/useSandboxSetupRecovery.ts', 1],
])

const failures = []
const actualRawRpcCalls = new Map()
const actualLegacySessionsListLiterals = new Map()
const actualRawRpcCallCapabilities = new Map()

function normalized(path) {
  return path.replace(/\\/g, '/')
}

function isTestFile(rel) {
  return /\.(test|spec)\.(ts|tsx)$/.test(rel)
}

function isAdapter(rel) {
  return rel.startsWith('src/adapters/')
}

function isGeneratedContract(rel) {
  return rel.startsWith('src/contracts/generated/')
}

function isRpcTransport(rel) {
  return rel === 'src/stores/rpc.ts'
}

function scriptBody(rel, body) {
  if (!rel.endsWith('.vue')) return body
  return [...body.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
    .map(match => match[1])
    .join('\n')
}

function sourceFile(rel, body) {
  const kind = rel.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  return ts.createSourceFile(
    rel,
    scriptBody(rel, body),
    ts.ScriptTarget.Latest,
    true,
    kind,
  )
}

function increment(map, key) {
  map.set(key, (map.get(key) ?? 0) + 1)
}

for (const file of walkFiles(srcRoot, /\.(ts|tsx|vue)$/)) {
  const rel = normalized(relative(root, file))
  const body = readFileSync(file, 'utf8')
  const source = sourceFile(rel, body)

  function visit(node) {
    const specifier = moduleReferenceSpecifier(ts, node)
    const generatedImportFailure = specifier
      ? generatedContractImportViolation({ root, importer: rel, specifier })
      : null
    if (generatedImportFailure) failures.push(generatedImportFailure)

    const receiver = callMemberReceiverText(ts, node, source)
    if (
      receiver
      && !isKnownNonRpcCallReceiver(receiver)
      && !isTestFile(rel)
      && !isAdapter(rel)
      && !isRpcTransport(rel)
    ) {
      increment(actualRawRpcCalls, rel)
    }

    const capabilityReceiver = callMemberReferenceReceiverText(ts, node, source)
    if (
      capabilityReceiver
      && !isDirectCallMemberReference(ts, node)
      && !ts.isTypeOfExpression(node.parent)
      && isRpcCapabilityReceiverText(capabilityReceiver)
      && !isTestFile(rel)
      && !isAdapter(rel)
      && !isRpcTransport(rel)
    ) {
      increment(actualRawRpcCallCapabilities, rel)
    }

    const destructuredSource = destructuredCallSourceText(ts, node, source)
    if (
      destructuredSource
      && isRpcCapabilityReceiverText(destructuredSource)
      && !isTestFile(rel)
      && !isAdapter(rel)
      && !isRpcTransport(rel)
    ) {
      failures.push(`${rel}: destructuring an RPC call capability can bypass the raw RPC ledger.`)
    }

    if (
      ts.isStringLiteralLike(node)
      && node.text === 'sessions.list'
      && !isAdapter(rel)
      && !isTestFile(rel)
      && !isGeneratedContract(rel)
    ) {
      increment(actualLegacySessionsListLiterals, rel)
    }

    ts.forEachChild(node, visit)
  }

  visit(source)
}

function compareExactLedger(name, expected, actual) {
  for (const [rel, count] of actual) {
    const approved = expected.get(rel)
    if (approved === undefined) {
      failures.push(`${rel}: unexpected ${name} (${count}); add an Adapter instead.`)
    } else if (approved !== count) {
      failures.push(`${rel}: ${name} count is ${count}, allowlist requires exactly ${approved}.`)
    }
  }
  for (const [rel, count] of expected) {
    const observed = actual.get(rel)
    if (observed === undefined) {
      failures.push(`${rel}: stale ${name} allowlist entry (${count}); remove it.`)
    }
  }
}

compareExactLedger('raw RpcClient.call', legacyRawRpcCalls, actualRawRpcCalls)
compareExactLedger(
  'extracted RpcClient.call capability',
  legacyRawRpcCallCapabilities,
  actualRawRpcCallCapabilities,
)
compareExactLedger(
  'sessions.list literal',
  legacySessionsListLiterals,
  actualLegacySessionsListLiterals,
)

if (failures.length > 0) {
  console.error(failures.join('\n'))
  process.exit(1)
}

const rawCount = [...actualRawRpcCalls.values()].reduce((sum, count) => sum + count, 0)
console.log(
  `RPC architecture guard passed (${rawCount} legacy calls in ${actualRawRpcCalls.size} files).`,
)
