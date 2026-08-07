import { describe, expect, it } from 'vitest'
import {
  errorBoundaryDetails,
  errorBoundaryMessage,
  redactErrorBoundaryText,
} from './errorBoundaryDetails'

describe('errorBoundaryMessage', () => {
  it('uses the Error message', () => {
    expect(errorBoundaryMessage(new Error('boom'))).toBe('boom')
  })

  it('turns a multiline message into a single line', () => {
    expect(errorBoundaryMessage(new Error('first\nsecond'))).toBe('first second')
  })

  it('passes a string through unchanged', () => {
    expect(errorBoundaryMessage('plain failure')).toBe('plain failure')
  })

  it('serializes a non-Error object', () => {
    expect(errorBoundaryMessage({ code: 42 })).toBe('{"code":42}')
  })

  it('falls back to String() when JSON serialization throws', () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    expect(errorBoundaryMessage(circular)).toBe('[object Object]')
  })

  it('returns an empty message for undefined or an entirely hostile value', () => {
    const hostile = new Proxy({}, {
      get() {
        throw new Error('blocked')
      },
      getPrototypeOf() {
        throw new Error('blocked')
      },
    })
    expect(errorBoundaryMessage(undefined)).toBe('')
    expect(() => errorBoundaryMessage(hostile)).not.toThrow()
    expect(errorBoundaryMessage(hostile)).toBe('')
  })
})

describe('errorBoundaryDetails', () => {
  it('uses a standard stack without duplicating its error heading', () => {
    const err = new Error('kaboom')
    err.stack = 'Error: kaboom\n    at somewhere (app.js:1:1)'
    expect(errorBoundaryDetails(err)).toBe(
      'Error: kaboom\n    at somewhere (app.js:1:1)',
    )
  })

  it('keeps a non-standard stack after the error heading', () => {
    const err = new Error('kaboom')
    err.stack = 'at somewhere (app.js:1:1)'
    expect(errorBoundaryDetails(err)).toBe(
      'Error: kaboom\n\nat somewhere (app.js:1:1)',
    )
  })

  it('handles an Error without a stack', () => {
    const err = new Error('no stack')
    err.stack = undefined
    expect(errorBoundaryDetails(err)).toBe('Error: no stack')
  })

  it('handles a non-Error value', () => {
    expect(errorBoundaryDetails('just a string')).toBe('just a string')
  })

  it('truncates an oversized stack with a locale-neutral marker', () => {
    const err = new Error('big')
    err.stack = 'x'.repeat(20000)
    const details = errorBoundaryDetails(err)
    expect(details.length).toBeLessThanOrEqual(8000)
    expect(details.endsWith('\n…')).toBe(true)
    expect(details).not.toContain('truncated')
  })

  it('does not throw when Error properties have hostile getters', () => {
    const err = new Error('safe')
    Object.defineProperties(err, {
      message: { get: () => { throw new Error('blocked') } },
      name: { get: () => { throw new Error('blocked') } },
      stack: { get: () => { throw new Error('blocked') } },
    })
    expect(() => errorBoundaryDetails(err)).not.toThrow()
    expect(errorBoundaryDetails(err)).toBe('Error')
  })
})

describe('redactErrorBoundaryText', () => {
  it('redacts macOS, Linux, and Windows home directory names', () => {
    const input = [
      '/Users/alice/.opensquilla/config.toml',
      '/home/bob/.opensquilla/state.db',
      String.raw`C:\Users\carol\AppData\Roaming\OpenSquilla`,
      'D:/Users/dave/AppData/Roaming/OpenSquilla',
    ].join('\n')
    expect(redactErrorBoundaryText(input)).toBe([
      '/Users/[user]/.opensquilla/config.toml',
      '/home/[user]/.opensquilla/state.db',
      String.raw`C:\Users\[user]\AppData\Roaming\OpenSquilla`,
      'D:/Users/[user]/AppData/Roaming/OpenSquilla',
    ].join('\n'))
  })

  it('redacts common secret assignments, URL credentials, and bearer tokens', () => {
    const input = [
      'https://alice:password@example.test/path?access_token=url-secret&mode=safe',
      'https://example.test/path?token=plain-token&mode=safe',
      'api_key="sk-example" password: hunter2',
      'Authorization: Bearer abc.def-123',
      'request failed with Bearer standalone-token',
      '--password flag-secret',
      'provider rejected sk-example123456',
    ].join('\n')
    const result = redactErrorBoundaryText(input)
    expect(result).not.toContain('alice:password')
    expect(result).not.toContain('url-secret')
    expect(result).not.toContain('plain-token')
    expect(result).not.toContain('sk-example')
    expect(result).not.toContain('hunter2')
    expect(result).not.toContain('abc.def-123')
    expect(result).not.toContain('standalone-token')
    expect(result).not.toContain('flag-secret')
    expect(result).not.toContain('sk-example123456')
    expect(result).toContain('access_token=[redacted]&mode=safe')
  })
})
