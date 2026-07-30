import { describe, expect, it, vi } from 'vitest'
import {
  REDACTED_SENTINEL,
  parseUpsertOutcome,
  probeChannelEntry,
  stripRedactionSentinels,
  upsertChannelEntry,
  type ChannelRpcCaller,
} from './channelRpc'

function fakeCaller(result: unknown) {
  const call = vi.fn(async () => result)
  return { call: call as unknown as ChannelRpcCaller['call'], mock: call }
}

describe('stripRedactionSentinels', () => {
  it('drops exactly the redaction placeholder values', () => {
    const out = stripRedactionSentinels({
      name: 'ops', token: REDACTED_SENTINEL, note: '***x', keep: '', flag: false,
    })
    expect(out).toEqual({ name: 'ops', note: '***x', keep: '', flag: false })
  })
})

describe('probe/upsert wrappers', () => {
  it('probeChannelEntry sends onboarding.channel.probe with a sentinel-free entry', async () => {
    const caller = fakeCaller({ status: 'validated' })
    await probeChannelEntry(caller, { type: 'slack', name: 'ops', token: REDACTED_SENTINEL })
    expect(caller.mock).toHaveBeenCalledWith('onboarding.channel.probe', {
      entry: { type: 'slack', name: 'ops' },
    })
  })

  it('upsertChannelEntry sends onboarding.channel.upsert with a sentinel-free entry', async () => {
    const caller = fakeCaller({ changed: true })
    await upsertChannelEntry(caller, { type: 'slack', name: 'ops', app_secret: REDACTED_SENTINEL })
    expect(caller.mock).toHaveBeenCalledWith('onboarding.channel.upsert', {
      entry: { type: 'slack', name: 'ops' },
    })
  })
})

describe('parseUpsertOutcome', () => {
  it('treats absent flags as restart-required (older gateways)', () => {
    const outcome = parseUpsertOutcome('ops', {})
    expect(outcome).toEqual({
      name: 'ops', changed: true, restartRequired: true, liveApplyFailed: false,
    })
  })

  it('honors explicit live-apply flags', () => {
    const outcome = parseUpsertOutcome('ops', { changed: true, restartRequired: false })
    expect(outcome.restartRequired).toBe(false)
    expect(outcome.changed).toBe(true)
  })

  it('flags a failed live apply for the saved channel only', () => {
    expect(parseUpsertOutcome('ops', { liveApply: { ops: 'failed' } }).liveApplyFailed).toBe(true)
    expect(parseUpsertOutcome('ops', { liveApply: { other: 'failed' } }).liveApplyFailed).toBe(false)
  })

  it('falls back to the response echo for the entry name', () => {
    expect(parseUpsertOutcome('', { entry: { name: 'from-server' } }).name).toBe('from-server')
    expect(parseUpsertOutcome(undefined, undefined).name).toBe('')
  })
})
