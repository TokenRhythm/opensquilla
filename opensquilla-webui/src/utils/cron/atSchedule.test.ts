import { describe, expect, it } from 'vitest'
import {
  atScheduleValueFromLocalInput,
  localDateTimeInputValue,
} from './atSchedule'

describe('at schedule form normalization', () => {
  it('adds an explicit offset for a new datetime-local value', () => {
    expect(atScheduleValueFromLocalInput('2026-05-18T09:00', '')).toMatch(
      /^2026-05-18T09:00:00[+-]\d{2}:\d{2}$/,
    )
  })

  it('preserves an existing schedule offset while editing the wall-clock time', () => {
    expect(
      atScheduleValueFromLocalInput(
        '2026-05-18T10:30',
        '2026-05-18T09:00:00+08:00',
      ),
    ).toBe('2026-05-18T10:30:00+08:00')
  })

  it('extracts the datetime-local portion from an offset timestamp', () => {
    expect(localDateTimeInputValue('2026-05-18T09:00:00+08:00')).toBe(
      '2026-05-18T09:00',
    )
  })
})
