import { describe, expect, it } from 'vitest'
import {
  adapterLoaded,
  lastErrorClass,
  statusPresentation,
} from './channelStatus'

describe('statusPresentation', () => {
  it('maps every wire status the gateway emits', () => {
    expect(statusPresentation({ status: 'connected' })).toMatchObject({ key: 'connected', tone: 'ok' })
    expect(statusPresentation({ status: 'stopped' })).toMatchObject({ key: 'notRunning', tone: 'muted' })
    expect(statusPresentation({ status: 'disabled' })).toMatchObject({ key: 'disabled', tone: 'muted' })
    expect(statusPresentation({ status: 'restarting' })).toMatchObject({ key: 'restarting', tone: 'info' })
    expect(statusPresentation({ status: 'dead' })).toMatchObject({ key: 'failed', tone: 'danger' })
    expect(statusPresentation({ status: 'exhausted' })).toMatchObject({ key: 'exhausted', tone: 'danger' })
  })

  it('never leaks a raw enum: unrecognized values become the unknown presentation', () => {
    const pres = statusPresentation({ status: 'running' })
    expect(pres.key).toBe('unknown')
    expect(pres.labelKey).toBe('channelStatus.unknown')
    expect(pres.raw).toBe('running')
    expect(statusPresentation({}).key).toBe('unknown')
  })

  it('treats enabled === false as disabled regardless of wire status, never warn-toned', () => {
    const pres = statusPresentation({ status: 'connected', enabled: false })
    expect(pres.key).toBe('disabled')
    expect(pres.tone).toBe('muted')
  })

  it('pending restart overlays every underlying status', () => {
    for (const status of ['connected', 'stopped', 'dead', 'disabled']) {
      const pres = statusPresentation({ status, pendingRestart: true })
      expect(pres.key).toBe('pendingRestart')
      expect(pres.tone).toBe('info')
    }
  })

  it('attaches a cause key only for known error classes on failure states', () => {
    expect(statusPresentation({ status: 'dead', errorClass: 'auth_invalid' }).causeKey)
      .toBe('channelStatus.cause.auth_invalid')
    expect(statusPresentation({ status: 'dead', errorClass: 'not_a_class' }).causeKey)
      .toBeUndefined()
    expect(statusPresentation({ status: 'connected', errorClass: 'auth_invalid' }).causeKey)
      .toBeUndefined()
  })
})

describe('lastErrorClass', () => {
  it('extracts error_class from diagnostics.last_error', () => {
    expect(lastErrorClass({ last_error: { error_class: 'rate_limited' } })).toBe('rate_limited')
    expect(lastErrorClass({ last_error: {} })).toBeUndefined()
    expect(lastErrorClass({})).toBeUndefined()
    expect(lastErrorClass(null)).toBeUndefined()
    expect(lastErrorClass('nope')).toBeUndefined()
  })
})

describe('adapterLoaded', () => {
  it('is true when a capability profile exists or the dispatch state implies a live adapter', () => {
    expect(adapterLoaded({ status: 'stopped', capability_profile: { transports: [] } })).toBe(true)
    expect(adapterLoaded({ status: 'connected' })).toBe(true)
    expect(adapterLoaded({ status: 'dead' })).toBe(true)
    expect(adapterLoaded({ status: 'restarting' })).toBe(true)
    expect(adapterLoaded({ status: 'exhausted' })).toBe(true)
  })

  it('is false for a config-only entry (stopped, no capability profile)', () => {
    expect(adapterLoaded({ status: 'stopped' })).toBe(false)
    expect(adapterLoaded({ status: 'stopped', capability_profile: null })).toBe(false)
    expect(adapterLoaded({ status: 'disabled' })).toBe(false)
  })
})
