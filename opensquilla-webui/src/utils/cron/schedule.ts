import i18n from '@/i18n'

/**
 * The schedule the friendly picker shows before the user touches anything.
 * The form model has to start here too — the picker reads its own state back
 * out of the expression, so a default that only exists on screen is a schedule
 * the Gateway never receives.
 */
export const DEFAULT_CRON_EXPRESSION = '0 9 * * *'

interface ParsedField {
  all: boolean
  set?: Set<number>
}

export interface ParsedCron {
  minute: ParsedField
  hour: ParsedField
  dom: ParsedField
  month: ParsedField
  dow: ParsedField
  raw: string
}

export function parseCron(expr: string): ParsedCron | null {
  if (!expr) return null
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return null
  const monthNames: Record<string, number> = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 }
  const dowNames: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 }
  try {
    const minute = parseField(parts[0], 0, 59)
    const hour = parseField(parts[1], 0, 23)
    const dom = parseField(parts[2], 1, 31)
    const month = parseField(parts[3], 1, 12, monthNames)
    const dow = parseField(parts[4], 0, 7, dowNames)
    if (!dow.all && dow.set?.has(7)) {
      dow.set.delete(7)
      dow.set.add(0)
    }
    return { minute, hour, dom, month, dow, raw: expr }
  } catch {
    return null
  }
}

export function nextRuns(parsed: ParsedCron, count: number, fromTs = Date.now()): Date[] {
  const results: Date[] = []
  const start = new Date(fromTs)
  start.setSeconds(0, 0)
  start.setMinutes(start.getMinutes() + 1)
  let d = new Date(start)
  const endLimit = fromTs + 365 * 24 * 3600 * 1000
  while (results.length < count && d.getTime() < endLimit) {
    const m = d.getMinutes()
    const h = d.getHours()
    const dom = d.getDate()
    const mon = d.getMonth() + 1
    const dow = d.getDay()
    const dayOk = parsed.dom.all && parsed.dow.all
      ? true
      : parsed.dom.all
        ? matches(parsed.dow, dow)
        : parsed.dow.all
          ? matches(parsed.dom, dom)
          : matches(parsed.dom, dom) || matches(parsed.dow, dow)
    if (
      matches(parsed.minute, m) &&
      matches(parsed.hour, h) &&
      matches(parsed.month, mon) &&
      dayOk
    ) {
      results.push(new Date(d))
    }
    d = new Date(d.getTime() + 60_000)
  }
  return results
}

export function explainCron(expr: string): string {
  const t = i18n.global.t
  const p = parseCron(expr)
  if (!p) return ''
  // Canonical English day/month tokens — used only for the structural Mon–Fri /
  // Sat+Sun detection below; the display labels come from i18n keys.
  const dowNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
  const dowLabel = (v: number) => t(`cronSkills.explain.dow.${dowNames[v].toLowerCase()}`)
  const monKeys = ['', 'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
  const monLabel = (v: number) => t(`cronSkills.explain.mon.${monKeys[v]}`)

  if (p.minute.all && p.hour.all) return t('cronSkills.explain.everyMinute')
  if (!p.minute.all && p.minute.set!.size === 1 && p.hour.all) {
    return t('cronSkills.explain.everyHourAt', { minute: String([...p.minute.set!][0]).padStart(2, '0') })
  }
  if (!p.minute.all && p.minute.set!.size === 1 && !p.hour.all && p.hour.set!.size === 1) {
    const m = [...p.minute.set!][0]
    const h = [...p.hour.set!][0]
    const time = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
    if (p.dom.all && p.dow.all && p.month.all) return t('cronSkills.explain.everyDayAt', { time })
    if (!p.dow.all && p.dom.all && p.month.all) {
      const dowVals = [...p.dow.set!].sort((a, b) => a - b)
      const tokens = dowVals.map(v => dowNames[v])
      if (tokens.length === 5 && tokens[0] === 'Mon' && tokens[4] === 'Fri') return t('cronSkills.explain.weekdaysAt', { time })
      if (tokens.length === 2 && tokens.includes('Sat') && tokens.includes('Sun')) return t('cronSkills.explain.weekendsAt', { time })
      return t('cronSkills.explain.daysAt', { days: dowVals.map(dowLabel).join(', '), time })
    }
    if (!p.dom.all && p.dow.all && p.month.all) {
      return t('cronSkills.explain.dayOfMonthAt', { days: [...p.dom.set!].sort((a, b) => a - b).join(', '), time })
    }
    if (!p.dom.all && p.dow.all && !p.month.all) {
      const months = [...p.month.set!].sort((a, b) => a - b).map(monLabel).join(', ')
      const days = [...p.dom.set!].sort((a, b) => a - b).join(', ')
      return t('cronSkills.explain.monthDaysAt', { months, days, time })
    }
  }
  if (!p.minute.all && p.minute.set!.size > 1 && p.hour.all) {
    const arr = [...p.minute.set!].sort((a, b) => a - b)
    const diffs = arr.slice(1).map((v, i) => v - arr[i])
    if (diffs.length && diffs.every(d => d === diffs[0]) && arr[0] % diffs[0] === 0) return t('cronSkills.explain.everyNMinutes', { n: diffs[0] })
  }
  return t('cronSkills.explain.fallback', {
    minute: humanizeFieldList(p.minute, t('cronSkills.explain.everyMinuteField')),
    hour: humanizeFieldList(p.hour, t('cronSkills.explain.everyHourField')),
  })
}

function parseField(field: string, min: number, max: number, names?: Record<string, number>): ParsedField {
  if (field === '*' || field === '?') return { all: true }
  const out = new Set<number>()
  field.split(',').forEach(part => {
    let stepStr = '1'
    let core = part
    const slash = part.indexOf('/')
    if (slash >= 0) {
      core = part.slice(0, slash)
      stepStr = part.slice(slash + 1)
    }
    const step = Math.max(1, parseInt(stepStr, 10) || 1)
    let lo: number | null = min
    let hi: number | null = max
    if (core === '*' || core === '') {
      lo = min
      hi = max
    } else if (core.includes('-')) {
      const [a, b] = core.split('-')
      lo = toNum(a, names)
      hi = toNum(b, names)
    } else {
      const n = toNum(core, names)
      lo = n
      hi = n
    }
    if (lo === null || hi === null || lo > max || hi < min) return
    lo = Math.max(min, lo)
    hi = Math.min(max, hi)
    for (let v = lo; v <= hi; v += step) out.add(v)
  })
  return { all: false, set: out }
}

function toNum(token: string | null, names?: Record<string, number>): number | null {
  if (token == null) return null
  const t = String(token).trim().toLowerCase()
  if (!t) return null
  if (names && names[t] !== undefined) return names[t]
  const n = parseInt(t, 10)
  return Number.isNaN(n) ? null : n
}

function matches(field: ParsedField, v: number): boolean {
  return field.all || field.set!.has(v)
}

function humanizeFieldList(field: ParsedField, allLabel: string): string {
  if (field.all) return allLabel
  const arr = [...field.set!].sort((a, b) => a - b)
  if (arr.length === 0) return '—'
  const display = arr.map(v => String(v).padStart(2, '0'))
  if (display.length === 1) return display[0]
  if (display.length <= 4) return display.join(', ')
  return display.slice(0, 3).join(', ') + i18n.global.t('cronSkills.explain.andMore', { n: display.length - 3 })
}
