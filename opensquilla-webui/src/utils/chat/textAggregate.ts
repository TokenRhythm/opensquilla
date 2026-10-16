/** The persisted transcript separates adjacent prose blocks only when the
 * provider did not already supply whitespace at that boundary. */
export function readableTextAggregate(chunks: readonly string[]): string {
  let readable = chunks[0] || ''
  for (const chunk of chunks.slice(1)) {
    readable += /\s$/u.test(readable) || /^\s/u.test(chunk) ? chunk : `\n\n${chunk}`
  }
  return readable
}
