import { describe, expect, it } from 'vitest'
import {
  absoluteTime,
  fullTime,
  isoTime,
  localizedRelativeTime,
  messageDate,
  relativeTime,
} from './messageTime'

const MS_2026 = Date.UTC(2026, 5, 23, 4, 10, 23) // 1750648223000, well above 1e12

describe('messageDate', () => {
  it('returns null for empty/missing values', () => {
    expect(messageDate(null)).toBeNull()
    expect(messageDate(undefined)).toBeNull()
    expect(messageDate('')).toBeNull()
  })

  it('returns null for an unparseable string instead of an Invalid Date', () => {
    expect(messageDate('not-a-date')).toBeNull()
  })

  it('treats a large number as epoch milliseconds', () => {
    expect(messageDate(MS_2026)?.getTime()).toBe(MS_2026)
  })

  it('promotes a sub-1e12 number from epoch SECONDS to milliseconds', () => {
    const seconds = Math.floor(MS_2026 / 1000)
    // The e2e fixtures seed seconds (Math.floor(Date.now()/1000)); without the
    // promotion this would resolve to ~1970 instead of the same instant.
    expect(messageDate(seconds)?.getTime()).toBe(MS_2026)
  })

  it('parses an ISO-8601 string with a Z designator', () => {
    expect(messageDate('2026-06-23T04:10:23.000Z')?.getTime()).toBe(MS_2026)
  })
})

describe('relativeTime', () => {
  const now = MS_2026

  it('is empty for missing or invalid timestamps', () => {
    expect(relativeTime(null, now)).toBe('')
    expect(relativeTime('garbage', now)).toBe('')
  })

  it('renders coarse buckets relative to the injected now', () => {
    expect(relativeTime(now - 5_000, now)).toBe('just now')
    expect(relativeTime(now - 5 * 60_000, now)).toBe('5m ago')
    expect(relativeTime(now - 2 * 3_600_000, now)).toBe('2h ago')
    expect(relativeTime(now - 3 * 86_400_000, now)).toBe('3d ago')
  })

  it('clamps a future timestamp (clock skew) to "just now"', () => {
    expect(relativeTime(now + 2 * 3_600_000, now)).toBe('just now')
  })

  it('does not render January 1970 for an epoch-SECONDS fixture value', () => {
    // Mirrors the e2e seed: Math.floor(Date.now()/1000) - 120.
    const seedSeconds = Math.floor(now / 1000) - 120
    expect(relativeTime(seedSeconds, now)).toBe('2m ago')
  })
})

describe('localizedRelativeTime', () => {
  const now = MS_2026

  it('is empty for missing or invalid timestamps', () => {
    expect(localizedRelativeTime(null, 'en', now)).toBe('')
    expect(localizedRelativeTime('garbage', 'en', now)).toBe('')
  })

  it('renders locale-aware buckets', () => {
    expect(localizedRelativeTime(now - 5 * 60_000, 'en', now)).toBe('5 minutes ago')
    expect(localizedRelativeTime(now - 5 * 60_000, 'zh-Hans', now)).toBe('5分钟前')
    expect(localizedRelativeTime(now - 2 * 3_600_000, 'en', now)).toBe('2 hours ago')
    expect(localizedRelativeTime(now - 3 * 86_400_000, 'en', now)).toBe('3 days ago')
  })

  it('clamps future timestamps and floors sub-minute ages to seconds', () => {
    expect(localizedRelativeTime(now + 60_000, 'en', now)).toBe('1 second ago')
    expect(localizedRelativeTime(now - 30_000, 'en', now)).toBe('30 seconds ago')
  })

  it('falls back to English for an invalid locale tag', () => {
    expect(localizedRelativeTime(now - 5 * 60_000, '!!bad!!', now)).toBe('5 minutes ago')
  })
})

describe('absoluteTime', () => {
  it('is empty for missing timestamps', () => {
    expect(absoluteTime(null)).toBe('')
  })

  it('formats seconds and milliseconds for the same instant identically', () => {
    const ms = Date.now() - 90_000
    expect(absoluteTime(Math.floor(ms / 1000))).toBe(absoluteTime(ms))
  })

  it('produces a non-empty, digit-bearing local label', () => {
    expect(absoluteTime(Date.now())).toMatch(/\d/)
  })
})

describe('isoTime / fullTime', () => {
  it('round-trips an instant to an ISO string', () => {
    expect(isoTime(MS_2026)).toBe(new Date(MS_2026).toISOString())
    expect(isoTime(Math.floor(MS_2026 / 1000))).toBe(new Date(MS_2026).toISOString())
  })

  it('is empty for missing timestamps', () => {
    expect(isoTime(null)).toBe('')
    expect(fullTime(null)).toBe('')
  })
})
