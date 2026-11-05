import { describe, expect, it } from 'vitest'
import { mapSessionReadError } from './sessionReadErrorMapping'

describe('history read failure projection', () => {
  it.each(['RESPONSE_TOO_LARGE', 'response_too_large', 'SNAPSHOT_TOO_LARGE'])(
    'preserves %s as a size failure without retrying the same request', code => {
      expect(mapSessionReadError(Object.assign(new Error('private diagnostic'), {
        code, retryable: true,
      }))).toMatchObject({ kind: 'too-large', retryable: false })
    },
  )

  it.each(['HISTORY_STALE', 'HISTORY_CURSOR_INVALIDATED'])(
    'recovers %s from the latest history window', code => {
      expect(mapSessionReadError(Object.assign(new Error('private cursor'), { code })))
        .toMatchObject({ code: 'history-cursor-rejected', reason: 'stale', recovery: 'reload-latest' })
    },
  )

  it('keeps read-lease recovery separate from history cursor recovery', () => {
    expect(mapSessionReadError(Object.assign(new Error('lease retired'), { code: 'REBASE_REQUIRED' })))
      .toMatchObject({ kind: 'rebase-required', retryable: true })
  })
})
