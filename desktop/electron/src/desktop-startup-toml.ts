// smol-toml accepts TOML 1.1, while supported Gateway runtimes include tomllib
// 1.0. This lexical filter only excludes syntax whose interpretation may differ;
// smol-toml still validates and parses the document, and date values are checked
// separately. It does not extract settings or replace either TOML parser.
export function mayNeedPythonTomlParser(raw: string): boolean {
  if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(raw)) return true
  const containers: string[] = []
  let inlineDepth = 0
  let quote = ''
  let multiline = false
  let continuation = false
  let comment = false
  let previous = ''
  for (let index = 0; index < raw.length; index++) {
    const character = raw[index]
    // Conservatively defer even newlines inside an inline table's value. This
    // keeps TOML 1.1 multiline inline tables on the original Python path.
    if ((character === '\n' || character === '\r') && inlineDepth > 0) return true
    if (comment) {
      if (character === '\n') comment = false
      continue
    }
    if (quote) {
      // tomllib normalizes multiline CRLF content; smol-toml preserves it.
      if (character === '\r') return true
      if (continuation && character !== ' ' && character !== '\t' && character !== '\n') {
        // smol-toml drops content quotes at this particular continuation end.
        if (raw.startsWith('""""', index)) return true
        continuation = false
      }
      if (quote === '"' && character === '\\') {
        const escaped = raw[++index]
        if (escaped === 'e' || escaped === 'x') return true
        if ((escaped === '\n' || escaped === '\r') && inlineDepth > 0) return true
        continuation = multiline && (escaped === ' ' || escaped === '\t' || escaped === '\r' || escaped === '\n')
        continue
      }
      if (character !== quote) continue
      if (multiline) {
        if (raw[index + 1] !== quote || raw[index + 2] !== quote) continue
        index += 2
        // TOML permits one or two content quotes before the closing triple.
        if (raw[index + 1] === quote) index++
        if (raw[index + 1] === quote) index++
      }
      quote = ''
      continue
    }
    if (character === '#') { comment = true; continue }
    if (character === ' ' || character === '\t' || character === '\r' || character === '\n') continue
    // Non-ASCII text is fine in quoted keys, strings and comments. Keep bare
    // keys and table names outside those contexts on Python's original path.
    if (character.charCodeAt(0) > 0x7f) return true
    if (character === '"' || character === "'") {
      quote = character
      multiline = raw[index + 1] === quote && raw[index + 2] === quote
      if (multiline) index += 2
    } else if (character === '{' || character === '[') {
      containers.push(character)
      if (character === '{') inlineDepth++
    } else if (character === '}' || character === ']') {
      const opening = containers.pop()
      if (opening !== (character === '}' ? '{' : '[')) return true
      if (character === '}') {
        if (previous === ',') return true // Inline trailing comma is TOML 1.1.
        inlineDepth--
      }
    }
    previous = character
  }
  return quote !== '' || containers.length !== 0
}
