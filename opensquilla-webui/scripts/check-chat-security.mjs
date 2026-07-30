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
  'src/utils/chat/artifacts.ts',
  /\btoken\??:\s*string|searchParams\.set\(['"]token['"]|includeSessionKey\s*!==\s*false/,
  'artifact URLs must not carry bearer tokens or default session keys in query params.',
)

assertPresent(
  'src/utils/chat/artifacts.ts',
  /searchParams\.delete\(['"]token['"]\)[\s\S]+searchParams\.delete\(['"]sessionKey['"]\)[\s\S]+searchParams\.delete\(['"]session_key['"]\)/,
  'artifact URL sanitizer must strip sensitive same-origin query params.',
)

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
  'src/utils/chat/attachmentAccess.ts',
  /url\.protocol !== 'http:'[\s\S]+url\.protocol !== 'https:'[\s\S]+url\.origin !== base\.origin/,
  'attachment downloads must reject non-HTTP(S) and cross-origin staged URLs.',
)

assertPresent(
  'src/utils/chat/attachmentAccess.ts',
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
