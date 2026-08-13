import { describe, expect, it } from 'vitest'

import {
  isLegacySilentSentinelOnly,
  sanitizeAssistantPresentationSegments,
  sanitizeAssistantPresentationText,
} from './silentSentinels'

describe('sanitizeAssistantPresentationText', () => {
  it.each([
    ['NO_REPLY', ''],
    ['  HEARTBEAT_OK  ', ''],
    ['NO_REPLY\nHEARTBEAT_OK', ''],
  ])('globally removes sentinel-only legacy text from %j', (input, expected) => {
    expect(sanitizeAssistantPresentationText(input)).toBe(expected)
    expect(sanitizeAssistantPresentationText(expected)).toBe(expected)
  })

  it.each([
    ['\r\nNO_REPLY\r\n\r\nVisible answer.\r\n', 'Visible answer.\r\n'],
    ['NO_REPLY\nHEARTBEAT_OK\nVisible answer.', 'Visible answer.'],
    ['Visible answer.\n\nHEARTBEAT_OK\n', 'Visible answer.'],
    ['NO_REPLY\nVisible answer.\nHEARTBEAT_OK', 'Visible answer.'],
  ])('removes mixed boundary markers from trusted internal text %j', (input, expected) => {
    const provenance = { runKind: 'goal' }
    expect(sanitizeAssistantPresentationText(input, provenance)).toBe(expected)
    expect(sanitizeAssistantPresentationText(expected, provenance)).toBe(expected)
  })

  it('preserves mixed boundary markers without trusted internal provenance', () => {
    const input = 'NO_REPLY\nVisible answer.\nHEARTBEAT_OK'
    expect(sanitizeAssistantPresentationText(input)).toBe(input)
    expect(sanitizeAssistantPresentationText(input, {
      inputMode: 'user',
      runKind: 'default',
    })).toBe(input)
    expect(sanitizeAssistantPresentationText(input, { inputMode: 'system_event' }))
      .toBe('Visible answer.')
    expect(sanitizeAssistantPresentationText(input, { runKind: 'heartbeat' }))
      .toBe('Visible answer.')
  })

  it.each([
    'Use NO_REPLY when no response is required.',
    'The literal `NO_REPLY` is documented here.',
    '> NO_REPLY',
    'NO_REPLY.md',
    'HEARTBEAT_OKAY',
    'Before\nNO_REPLY\nAfter',
    ['```text', 'NO_REPLY', '```'].join('\n'),
    ['~~~', 'HEARTBEAT_OK', '~~~'].join('\n'),
    ['```text', 'example', '```', 'NO_REPLY in prose'].join('\n'),
  ])('preserves non-marker documentation %j', (input) => {
    expect(sanitizeAssistantPresentationText(input)).toBe(input)
  })

  it('does not remove a trailing marker from an unclosed fenced example', () => {
    const input = ['Example:', '```text', 'NO_REPLY'].join('\n')
    expect(sanitizeAssistantPresentationText(input)).toBe(input)
  })

  it.each([
    '    NO_REPLY',
    '\tHEARTBEAT_OK',
    '   \tNO_REPLY',
    'Visible answer.\n    HEARTBEAT_OK',
    '    NO_REPLY\nVisible answer.',
  ])('preserves Markdown-indented code marker %j even for an internal turn', (input) => {
    expect(sanitizeAssistantPresentationText(input, { runKind: 'goal' })).toBe(input)
    expect(isLegacySilentSentinelOnly(input)).toBe(false)
  })

  it('recognizes only presentation text made entirely of boundary markers', () => {
    expect(isLegacySilentSentinelOnly('\nNO_REPLY\n')).toBe(true)
    expect(isLegacySilentSentinelOnly('NO_REPLY\nHEARTBEAT_OK')).toBe(true)
    expect(isLegacySilentSentinelOnly('NO_REPLY\nVisible')).toBe(false)
    expect(isLegacySilentSentinelOnly('')).toBe(false)
  })
})

describe('sanitizeAssistantPresentationSegments', () => {
  it('cleans only the outer text-segment boundaries', () => {
    expect(sanitizeAssistantPresentationSegments([
      'NO_REPLY',
      'Before',
      'NO_REPLY',
      'After',
      'HEARTBEAT_OK',
    ], { runKind: 'goal' })).toEqual([
      '',
      'Before',
      'NO_REPLY',
      'After',
      '',
    ])
  })

  it('cleans boundary marker lines attached to substantive segments', () => {
    expect(sanitizeAssistantPresentationSegments([
      'NO_REPLY\nFirst',
      'Last\nHEARTBEAT_OK',
    ], { inputMode: 'system_event' })).toEqual(['First', 'Last'])
  })

  it('preserves mixed direct-user segments but globally hides sentinel-only segments', () => {
    expect(sanitizeAssistantPresentationSegments(['NO_REPLY', 'Explanation']))
      .toEqual(['NO_REPLY', 'Explanation'])
    expect(sanitizeAssistantPresentationSegments(['NO_REPLY', 'HEARTBEAT_OK']))
      .toEqual(['', ''])
  })
})
