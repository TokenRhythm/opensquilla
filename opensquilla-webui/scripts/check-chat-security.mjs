import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))

function read(rel) {
  return readFileSync(join(root, rel), 'utf8')
}

const failures = []

function assertAbsent(rel, pattern, message) {
  const body = read(rel)
  if (pattern.test(body)) failures.push(`${rel}: ${message}`)
}

function assertPresent(rel, pattern, message) {
  const body = read(rel)
  if (!pattern.test(body)) failures.push(`${rel}: ${message}`)
}

assertAbsent(
  'src/adapters/gateway/artifactAccessV4.ts',
  /\btoken\??:\s*string|searchParams\.set\(['"]token['"]|includeSessionKey\s*!==\s*false/,
  'artifact URLs must not carry bearer tokens or default session keys in query params.',
)

assertPresent(
  'src/adapters/gateway/privateArtifactHttpTransport.ts',
  /searchParams\.delete\(['"]token['"]\)[\s\S]+searchParams\.delete\(['"]sessionKey['"]\)[\s\S]+searchParams\.delete\(['"]session_key['"]\)/,
  'artifact URL sanitizer must strip sensitive same-origin query params.',
)

for (const rel of [
  'src/views/ChatView.vue',
  'src/components/workbench/AppWorkbench.vue',
  'src/components/workbench/ArtifactDocumentPanel.vue',
  'src/components/workbench/ArtifactPreviewPanel.vue',
  'src/components/workbench/artifactWorkbenchProvider.ts',
  'src/components/chat/ArtifactImageLightbox.vue',
  'src/components/chat/AssistantMessage.vue',
  'src/components/chat/AudioArtifactCard.vue',
  'src/components/chat/ChatArtifactList.vue',
  'src/components/chat/ChatMessageList.vue',
  'src/components/chat/DeliverablesDrawer.vue',
  'src/components/chat/VideoArtifactCard.vue',
]) {
  assertAbsent(
    rel,
    /\bauthToken\b|opensquilla\.wsToken|artifact(?:Download|Preview|Thumbnail)Url|artifactAccessHeaders/,
    'artifact consumers must use semantic artifact/session requests without HTTP credentials.',
  )
}

for (const rel of [
  'src/workbench/workbenchResourceProvider.ts',
  'src/workbench/artifactDocumentProvider.ts',
  'src/workbench/artifactPromptAnnotationProvider.ts',
]) {
  assertAbsent(
    rel,
    /\b[A-Z_]+_RPC_METHODS\b|\bcreateRpc[A-Z]/,
    'business provider barrels must not re-export Gateway wire helpers.',
  )
}

assertAbsent(
  'src/composables/chat/useChatMarkdownExport.ts',
  /\bsessionKey\b|\bauthToken\b|\btoken\b|artifactDownloadUrl/,
  'Markdown export must not persist raw sessions, bearer tokens, or signed artifact URLs.',
)

assertAbsent(
  'src/components/chat/ChatArtifactList.vue',
  /artifactPreviewUrl\(|:href="artifactUrl\(|:src="artifactUrl\(/,
  'artifact previews must not render credential-bearing artifact URLs directly into the DOM.',
)

assertPresent(
  'src/components/chat/ChatArtifactList.vue',
  /URL\.createObjectURL\(blob\)/,
  'artifact previews must render fetched blob object URLs.',
)

assertPresent(
  'src/adapters/gateway/privateArtifactHttpTransport.ts',
  /url\.protocol !== 'http:'[\s\S]+url\.protocol !== 'https:'[\s\S]+url\.origin !== base\.origin/,
  'attachment downloads must reject non-HTTP(S) and cross-origin staged URLs.',
)

assertPresent(
  'src/adapters/gateway/privateArtifactHttpTransport.ts',
  /CREDENTIAL_QUERY_KEYS[\s\S]+url\.searchParams\.delete\(key\)/,
  'attachment downloads must strip token and session query credentials.',
)

assertAbsent(
  'src/components/chat/UserMessage.vue',
  /:src="(?:attachment\.)?(?:downloadData|download_url|localFile)/,
  'download-only attachment sources must never be rendered directly into the DOM.',
)

assertPresent(
  'src/utils/chat/attachments.ts',
  /essence !== 'image\/svg\+xml'/,
  'SVG user attachments must remain download-only active documents.',
)

// Assistant markdown is sanitized before it reaches the DOM: the renderer must
// not bypass DOMPurify, and must never let assistant text render arbitrary form
// controls. The only <input> markdown produces is a disabled task-list checkbox.
assertAbsent(
  'src/composables/chat/useChatTextRendering.ts',
  /forceKeepAttr/,
  'markdown sanitization must not bypass DOMPurify via forceKeepAttr.',
)

assertPresent(
  'src/composables/chat/useChatTextRendering.ts',
  /addHook\(\s*['"]uponSanitizeElement['"][\s\S]*?removeChild/,
  'markdown sanitizer must drop non-task-list <input> elements (uponSanitizeElement + removeChild).',
)

if (failures.length > 0) {
  console.error(failures.join('\n'))
  process.exit(1)
}

console.log('Chat security guard passed.')
