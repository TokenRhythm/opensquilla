const ISO_OFFSET_RE = /(Z|[+-]\d{2}:\d{2})$/i
const LOCAL_DATE_TIME_RE =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/

export function localDateTimeInputValue(value: string | undefined): string {
  const match = String(value || '').match(
    /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/,
  )
  return match?.[1] || ''
}

function browserOffsetForLocalDateTime(value: string): string {
  const match = value.match(LOCAL_DATE_TIME_RE)
  if (!match) return '+00:00'

  const [, year, month, day, hour, minute] = match
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
  )
  if (Number.isNaN(date.getTime())) return '+00:00'

  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes < 0 ? '-' : '+'
  const absolute = Math.abs(offsetMinutes)
  const offsetHours = String(Math.floor(absolute / 60)).padStart(2, '0')
  const offsetRemainder = String(absolute % 60).padStart(2, '0')
  return `${sign}${offsetHours}:${offsetRemainder}`
}

export function atScheduleValueFromLocalInput(
  localValue: string,
  currentIsoValue: string | undefined,
): string {
  if (!localValue) return ''
  const existingOffset = String(currentIsoValue || '').match(ISO_OFFSET_RE)?.[1]
  const offset = existingOffset || browserOffsetForLocalDateTime(localValue)
  return `${localValue}:00${offset}`
}
