/** Server-issued admission window; browser wall time is not authority. */
export function parseTurnReceiptEpoch(value: unknown): { boot: string; issued: number; expires: number } | undefined {
  if (typeof value !== 'string') return undefined
  const match = /^v2\.([0-9a-f]{32})\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(value)
  if (!match) return undefined
  const issued = Number(match[2])
  const expires = Number(match[3])
  if (!Number.isSafeInteger(issued) || !Number.isSafeInteger(expires) || expires !== issued + 25) return undefined
  return { boot: match[1]!, issued, expires }
}
