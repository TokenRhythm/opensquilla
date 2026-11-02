// Match TokenRhythm's two-decimal ceiling display without losing source precision.
export function formatAccountMoney(value: string | undefined, currency?: string): string {
  if (value === undefined || !/^-?\d+(?:\.\d+)?$/.test(value)) return '—'
  const negative = value.startsWith('-')
  const [whole, fraction = ''] = (negative ? value.slice(1) : value).split('.')
  let cents = BigInt(whole!) * 100n + BigInt(fraction.padEnd(2, '0').slice(0, 2))
  if (!negative && /[1-9]/.test(fraction.slice(2))) cents += 1n
  const amount = `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`
  const prefix = currency === 'CNY' ? '¥' : currency ? `${currency} ` : ''
  return `${negative && cents > 0n ? '-' : ''}${prefix}${amount}`
}
