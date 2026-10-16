/** Source formats rendered as escaped text, never as executable HTML. */
const SOURCE_EXTENSIONS = new Set([
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'json', 'jsonc', 'jsonl', 'ndjson',
  'py', 'pyi', 'sql', 'yaml', 'yml', 'css',
])
const SOURCE_MIMES = new Set([
  'application/javascript', 'text/javascript', 'application/ecmascript', 'text/ecmascript',
  'application/typescript', 'text/typescript', 'application/json', 'application/ld+json',
  'application/jsonl', 'application/ndjson', 'application/x-ndjson',
  'text/x-python', 'text/x-python-script', 'application/sql', 'text/x-sql',
  'application/yaml', 'application/x-yaml', 'text/yaml', 'text/x-yaml', 'text/css',
])

export function isSourceTextMime(value: string): boolean {
  return SOURCE_MIMES.has(value.split(';', 1)[0].trim().toLowerCase())
}

export function isSourceTextFile(name: string, declaredMime = ''): boolean {
  const mime = declaredMime.split(';', 1)[0].trim().toLowerCase()
  if (isSourceTextMime(mime)) return true
  // A name alone cannot override a concrete binary MIME (e.g. video/mp2t).
  if (mime && mime !== 'application/octet-stream' && mime !== 'text/plain') return false
  const extension = name.trim().toLowerCase().split('.').pop() || ''
  return SOURCE_EXTENSIONS.has(extension) && name.includes('.')
}
