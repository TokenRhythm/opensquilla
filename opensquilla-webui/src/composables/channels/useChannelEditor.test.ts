import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  _resetChannelCatalogForTests,
  ensureChannelCatalog,
  suggestChannelName,
  useChannelEditor,
} from './useChannelEditor'

// Module-level rpc mock: every editor instance in this file talks to this fake.
const rpcCall = vi.fn()
vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: rpcCall, waitForConnection: async () => {} }),
}))

const SLACK_SPEC = {
  type: 'slack',
  label: 'Slack',
  fields: [
    { name: 'name', label: 'Channel name', type: 'text', required: true },
    { name: 'connection_mode', label: 'Connection mode', type: 'select', default: 'socket', choices: ['socket', 'webhook'] },
    { name: 'slack_channel_id', label: 'Default channel id', type: 'text', default: '' },
    { name: 'token', label: 'Bot token', type: 'password', required: true, secret: true, group: 'credentials' },
    { name: 'signing_secret', label: 'Signing secret', type: 'password', secret: true, group: 'credentials', showWhen: { connection_mode: 'webhook' } },
  ],
}

const STORED_ENTRY = {
  name: 'team-slack',
  type: 'slack',
  connection_mode: 'webhook',
  token: '***',
  signing_secret: '***',
  agent_id: 'main', // not a spec field → read-only passthrough
}

function mockRpc(overrides: Record<string, (params?: Record<string, unknown>) => unknown> = {}) {
  rpcCall.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
    if (method in overrides) return overrides[method](params)
    if (method === 'onboarding.catalog') return { channels: [SLACK_SPEC] }
    if (method === 'channels.get') {
      return { entry: { ...STORED_ENTRY }, secretFields: ['token', 'signing_secret'] }
    }
    if (method === 'onboarding.channel.probe') {
      return { status: 'validated', connected: false, restartRequired: true, warnings: [] }
    }
    if (method === 'onboarding.channel.upsert') {
      const entry = params?.entry as Record<string, unknown> | undefined
      return { changed: true, restartRequired: true, entry: { name: entry?.name } }
    }
    throw new Error(`unexpected rpc method: ${method}`)
  })
}

function catalogCalls(): number {
  return rpcCall.mock.calls.filter(([method]) => method === 'onboarding.catalog').length
}

beforeEach(() => {
  rpcCall.mockReset()
  _resetChannelCatalogForTests()
  mockRpc()
})

describe('useChannelEditor', () => {
  it('open() hydrates from channels.get + catalog and starts clean', async () => {
    const editor = useChannelEditor()
    await editor.open('team-slack')
    expect(editor.phase.value).toBe('active')
    expect(editor.canEdit.value).toBe(true)
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.editedFields.value).toEqual([])
    // Passthrough keys surface read-only; the type key never does.
    expect(editor.extraRows.value).toEqual([
      { key: 'agent_id', secret: false, value: 'main' },
    ])
  })

  it('caches the catalog at module scope and refetches only on refresh', async () => {
    const first = useChannelEditor()
    await first.open('team-slack')
    expect(catalogCalls()).toBe(1)

    const second = useChannelEditor()
    await second.open('team-slack')
    expect(catalogCalls()).toBe(1)

    await second.refreshCatalog()
    expect(catalogCalls()).toBe(2)
  })

  it('coalesces concurrent catalog fetches into one request', async () => {
    const [a, b] = await Promise.all([
      ensureChannelCatalog({ call: rpcCall }),
      ensureChannelCatalog({ call: rpcCall }),
    ])
    expect(a).toBe(b)
    expect(catalogCalls()).toBe(1)
  })

  it('tracks edited fields against the loaded baseline, in spec order', async () => {
    const editor = useChannelEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C42')
    editor.updateField('connection_mode', 'socket')
    expect(editor.editedFields.value).toEqual(['connection_mode', 'slack_channel_id'])
    editor.discard()
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.editedFields.value).toEqual([])
  })

  it('testDraft probes the current draft without any redaction sentinel', async () => {
    const editor = useChannelEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C42')
    const ok = await editor.testDraft()
    expect(ok).toBe(true)
    expect(editor.probe.value.ok).toBe(true)
    expect(editor.probe.value.rows[0]?.tone).toBe('ok')
    const call = rpcCall.mock.calls.find(([method]) => method === 'onboarding.channel.probe')!
    const entry = (call[1] as { entry: Record<string, unknown> }).entry
    expect(entry).toMatchObject({ type: 'slack', name: 'team-slack', slack_channel_id: 'C42' })
    expect('token' in entry).toBe(false)
    expect(JSON.stringify(entry)).not.toContain('***')
  })

  it('save() probes then upserts and resets the baseline from the reseed', async () => {
    const editor = useChannelEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C42')
    expect(editor.form.isDirty.value).toBe(true)

    const result = await editor.save()
    expect(result.status).toBe('saved')
    expect(result.outcome).toMatchObject({
      name: 'team-slack', changed: true, restartRequired: true, liveApplyFailed: false,
    })
    const methods = rpcCall.mock.calls.map(([method]) => method)
    expect(methods.indexOf('onboarding.channel.probe'))
      .toBeLessThan(methods.indexOf('onboarding.channel.upsert'))
    // Reseeded from the server: clean again.
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.probe.value.phase).toBe('idle')
  })

  it('save() blocks on a failed probe; saveAnyway() commits regardless', async () => {
    mockRpc({
      'onboarding.channel.probe': () => {
        throw new Error('invalid channel entry: token: Field required')
      },
    })
    const editor = useChannelEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C42')

    const blocked = await editor.save()
    expect(blocked.status).toBe('probe-failed')
    expect(editor.probe.value.ok).toBe(false)
    expect(editor.probe.value.fromSave).toBe(true)
    expect(editor.probe.value.rows[0]?.text).toContain('token: Field required')
    expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)

    const forced = await editor.saveAnyway()
    expect(forced.status).toBe('saved')
    expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(true)
  })

  it('secret replace → cancel round-trip keeps the payload sentinel-free', async () => {
    const editor = useChannelEditor()
    await editor.open('team-slack')

    editor.replaceSecret('token')
    editor.updateField('token', 'xoxb-typed-then-cancelled')
    expect(editor.form.isDirty.value).toBe(true)
    editor.cancelSecretReplace('token')
    expect(editor.form.isDirty.value).toBe(false)

    editor.replaceSecret('signing_secret')
    editor.updateField('signing_secret', 'fresh-secret')
    const result = await editor.save()
    expect(result.status).toBe('saved')
    const call = rpcCall.mock.calls.find(([method]) => method === 'onboarding.channel.upsert')!
    const entry = (call[1] as { entry: Record<string, unknown> }).entry
    expect(entry.signing_secret).toBe('fresh-secret')
    expect('token' in entry).toBe(false)
    expect(JSON.stringify(entry)).not.toContain('***')
  })

  it('startCompose seeds an empty draft from spec defaults and saves without reseeding', async () => {
    const editor = useChannelEditor()
    await editor.startCompose('slack')
    expect(editor.phase.value).toBe('active')
    expect(editor.canEdit.value).toBe(true)
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.form.isEditing.value).toBe(false)
    expect(editor.extraRows.value).toEqual([])

    editor.updateField('name', 'fresh-slack')
    editor.updateField('token', 'xoxb-new')
    expect(editor.form.isDirty.value).toBe(true)

    const result = await editor.save()
    expect(result.status).toBe('saved')
    expect(result.outcome?.name).toBe('fresh-slack')
    const upsert = rpcCall.mock.calls.find(([method]) => method === 'onboarding.channel.upsert')!
    const entry = (upsert[1] as { entry: Record<string, unknown> }).entry
    expect(entry).toMatchObject({ type: 'slack', name: 'fresh-slack', token: 'xoxb-new' })
    expect(JSON.stringify(entry)).not.toContain('***')
    // Compose commits never reseed from the server (the caller dismisses the
    // takeover and selects the new channel instead).
    expect(rpcCall.mock.calls.some(([method]) => method === 'channels.get')).toBe(false)
  })

  it('startCompose seeds a unique suggested name that does not read dirty', async () => {
    const editor = useChannelEditor()
    await editor.startCompose('slack', { existingNames: ['feishu', 'ops-bot'] })
    const nameRow = editor.panel.value.channelFields.find(row => row.field.name === 'name')
    expect(nameRow?.value).toBe('slack')
    // The prefill is part of the pristine draft: no dirty flag, no edited tick.
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.editedFields.value).toEqual([])
  })

  it('startCompose increments the suggestion past existing names of ANY type', async () => {
    const editor = useChannelEditor()
    // A non-slack channel literally named "slack" still collides — the name
    // is the global identity key and upsert would overwrite it.
    await editor.startCompose('slack', { existingNames: ['Slack', 'slack-2', 'feishu'] })
    const nameRow = editor.panel.value.channelFields.find(row => row.field.name === 'name')
    expect(nameRow?.value).toBe('slack-3')
  })

  it('startCompose leaves the name blank when the caller has no name list yet', async () => {
    const editor = useChannelEditor()
    await editor.startCompose('slack')
    const nameRow = editor.panel.value.channelFields.find(row => row.field.name === 'name')
    expect(nameRow?.value).toBe('')
  })

  it('loadCatalog tracks a failure and recovers on retry', async () => {
    mockRpc({
      'onboarding.catalog': () => {
        throw new Error('catalog down')
      },
    })
    const editor = useChannelEditor()
    await editor.loadCatalog()
    expect(editor.catalogError.value).toContain('catalog down')
    expect(editor.catalog.value).toEqual([])

    mockRpc()
    await editor.loadCatalog()
    expect(editor.catalogError.value).toBe('')
    expect(editor.catalog.value.map(spec => spec.type)).toEqual(['slack'])
  })

  it('an unknown catalog type stays readable but not editable', async () => {
    mockRpc({
      'channels.get': () => ({
        entry: { name: 'mystery', type: 'carrier-pigeon', roost: 'north' },
        secretFields: [],
      }),
    })
    const editor = useChannelEditor()
    await editor.open('mystery')
    expect(editor.phase.value).toBe('active')
    expect(editor.canEdit.value).toBe(false)
    expect(editor.extraRows.value).toEqual([
      { key: 'name', secret: false, value: 'mystery' },
      { key: 'roost', secret: false, value: 'north' },
    ])
  })

  it('surfaces a load failure and recovers on the next open', async () => {
    mockRpc({
      'channels.get': () => {
        throw new Error('boom')
      },
    })
    const editor = useChannelEditor()
    await editor.open('team-slack')
    expect(editor.phase.value).toBe('error')
    expect(editor.loadError.value).toContain('boom')

    mockRpc()
    await editor.open('team-slack')
    expect(editor.phase.value).toBe('active')
  })
})

describe('suggestChannelName', () => {
  it('returns the bare type when free', () => {
    expect(suggestChannelName('feishu', [])).toBe('feishu')
    expect(suggestChannelName('feishu', ['slack', 'tg-alerts'])).toBe('feishu')
  })

  it('increments until free, case-insensitively', () => {
    expect(suggestChannelName('feishu', ['feishu'])).toBe('feishu-2')
    expect(suggestChannelName('feishu', ['Feishu', 'FEISHU-2'])).toBe('feishu-3')
  })

  it('never suggests a taken name even against a dense list', () => {
    const taken = ['qq', ...Array.from({ length: 40 }, (_, i) => `qq-${i + 2}`)]
    const suggestion = suggestChannelName('qq', taken)
    expect(taken).not.toContain(suggestion)
  })
})
