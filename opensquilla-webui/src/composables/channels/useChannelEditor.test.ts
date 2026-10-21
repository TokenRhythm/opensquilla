import { beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope } from 'vue'
import {
  _resetChannelCatalogForTests,
  ensureChannelCatalog,
  suggestChannelName,
  useChannelEditor,
} from './useChannelEditor'
import type { SetupCatalogPort } from '@/modules/setupWorkflow'
import type { ChannelAdministration } from '@/modules/channelAdministration'
import type { ChannelSetup } from '@/modules/channelSetup'

const rpcCall = vi.fn()

const setupCatalog = {
  catalog: () => rpcCall('onboarding.catalog'),
  discoverImageGenerationModels: () => Promise.resolve({ ok: false }),
} satisfies SetupCatalogPort

const channelAdministration = {
  get: (name: string) => rpcCall('channels.get', { name }),
} as unknown as ChannelAdministration

const channelSetup = {
  probeDraft: (entry: Record<string, unknown>) => rpcCall('onboarding.channel.probe', { entry }),
  upsert: async (entry: Record<string, unknown>) => {
    const result = await rpcCall('onboarding.channel.upsert', { entry }) as {
      changed?: boolean
      restartRequired?: boolean
      liveApply?: Record<string, string> | null
    }
    const name = String(entry.name || '')
    return {
      name,
      changed: result.changed !== false,
      restartRequired: result.restartRequired !== false,
      liveApplyFailed: result.liveApply?.[name] === 'failed',
    }
  },
} as ChannelSetup

function createEditor() {
  return useChannelEditor(setupCatalog, channelAdministration, channelSetup)
}

function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function savedChannel(name: string) {
  return { entry: { ...STORED_ENTRY, name }, secretFields: ['token', 'signing_secret'] }
}

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
  it('keeps the initial configuration load alive when edit mode is discarded before it arrives', async () => {
    const initial = deferred()
    mockRpc({ 'channels.get': () => initial.promise })
    const editor = createEditor()
    const loading = editor.open('team-slack')
    editor.discard()
    initial.resolve(savedChannel('team-slack'))
    await loading
    expect(editor.phase.value).toBe('active')
    expect(editor.loadedName.value).toBe('team-slack')
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.form.payload().name).toBe('team-slack')
  })

  it.each(['resolve', 'reject'] as const)('ignores a stale channel load that later %ss', async settle => {
    const first = deferred()
    mockRpc({ 'channels.get': params => params?.name === 'first' ? first.promise : savedChannel('second') })
    const editor = createEditor()
    const oldLoad = editor.open('first')
    editor.reset()
    await editor.open('second')
    editor.updateField('slack_channel_id', 'C-SECOND')
    if (settle === 'resolve') first.resolve(savedChannel('first'))
    else first.reject(new Error('old load failed'))
    await oldLoad
    expect(editor.phase.value).toBe('active')
    expect(editor.loadError.value).toBe('')
    expect(editor.loadedName.value).toBe('second')
    expect(editor.form.payload()).toMatchObject({ name: 'second', slack_channel_id: 'C-SECOND' })
    await editor.save()
    expect(rpcCall).toHaveBeenCalledWith('onboarding.channel.upsert', {
      entry: expect.objectContaining({ name: 'second', slack_channel_id: 'C-SECOND' }),
    })
  })

  it('does not reopen a reset editor when the catalog for compose arrives', async () => {
    const catalog = deferred()
    mockRpc({ 'onboarding.catalog': () => catalog.promise })
    const editor = createEditor()
    const composing = editor.startCompose('slack')
    editor.reset()
    catalog.resolve({ channels: [SLACK_SPEC] })
    await composing
    expect(editor.phase.value).toBe('idle')
    expect(editor.entryType.value).toBe('')
    expect(editor.form.payload()).not.toHaveProperty('name')
  })

  it('keeps a new compose draft when a previous saved channel finishes loading', async () => {
    const first = deferred()
    mockRpc({ 'channels.get': () => first.promise })
    const editor = createEditor()
    const loading = editor.open('first')
    await editor.startCompose('slack', { existingNames: [] })
    editor.updateField('token', 'draft-secret')
    first.resolve(savedChannel('first'))
    await loading
    expect(editor.form.isEditing.value).toBe(false)
    expect(editor.form.payload()).toMatchObject({ name: 'slack', token: 'draft-secret' })
  })

  it('drops pending configuration reads when its scope is disposed', async () => {
    const first = deferred()
    mockRpc({ 'channels.get': () => first.promise })
    const scope = effectScope()
    const editor = scope.run(createEditor)!
    const loading = editor.open('first')
    scope.stop()
    first.resolve(savedChannel('first'))
    await loading
    expect(editor.loadedName.value).toBe('')
    expect(editor.loadedEntry.value).toBeNull()
  })

  it.each(['resolve', 'reject'] as const)('does not restore a stale probe verdict after an edit (%s)', async settle => {
    const pending = deferred()
    mockRpc({ 'onboarding.channel.probe': () => pending.promise })
    const editor = createEditor()
    await editor.open('team-slack')
    const probing = editor.testDraft()
    editor.updateField('slack_channel_id', 'C-NEW')
    if (settle === 'resolve') pending.resolve({ warnings: ['old warning'] })
    else pending.reject(new Error('old draft failed'))
    expect(await probing).toBe(false)
    expect(editor.probe.value).toEqual({ phase: 'idle', rows: [] })
  })

  it('does not commit another draft when selection changes during the save probe', async () => {
    const pending = deferred()
    mockRpc({
      'channels.get': params => savedChannel(String(params?.name)),
      'onboarding.channel.probe': () => pending.promise,
    })
    const editor = createEditor()
    await editor.open('first')
    editor.updateField('slack_channel_id', 'C-FIRST')
    const saving = editor.save()
    editor.reset()
    await editor.open('second')
    editor.updateField('slack_channel_id', 'C-SECOND')
    pending.resolve({ warnings: [] })
    expect(await saving).toEqual({ status: 'superseded' })
    expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)
    expect(editor.form.payload()).toMatchObject({ name: 'second', slack_channel_id: 'C-SECOND' })
    expect(editor.probe.value.phase).toBe('idle')
  })

  it('does not submit unprobed edits made while a save is probing', async () => {
    const pending = deferred()
    mockRpc({ 'onboarding.channel.probe': () => pending.promise })
    const editor = createEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C-OLD')
    const saving = editor.save()
    editor.updateField('slack_channel_id', 'C-NEW')
    pending.resolve({ warnings: [] })
    expect(await saving).toEqual({ status: 'superseded' })
    expect(rpcCall.mock.calls.some(([method]) => method === 'onboarding.channel.upsert')).toBe(false)
    expect(editor.form.payload().slack_channel_id).toBe('C-NEW')
  })

  it('does not reseed or release the new save when an old upsert finishes after reopening the same channel', async () => {
    const first = deferred()
    const second = deferred()
    let commits = 0
    mockRpc({ 'onboarding.channel.upsert': () => ++commits === 1 ? first.promise : second.promise })
    const editor = createEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C-OLD')
    const oldSave = editor.saveAnyway()
    editor.reset()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C-NEW')
    const newSave = editor.saveAnyway()
    first.resolve({ changed: true })
    expect(await oldSave).toMatchObject({ status: 'superseded', outcome: { name: 'team-slack' } })
    expect(editor.saving.value).toBe(true)
    expect(editor.form.payload().slack_channel_id).toBe('C-NEW')
    expect(rpcCall.mock.calls.filter(([method]) => method === 'channels.get')).toHaveLength(2)
    second.resolve({ changed: true })
    expect((await newSave).status).toBe('saved')
    expect(editor.saving.value).toBe(false)
  })

  it('guards the post-save reseed when its response arrives after selecting another channel', async () => {
    const reseed = deferred()
    let reads = 0
    mockRpc({ 'channels.get': params => ++reads === 2 ? reseed.promise : savedChannel(String(params?.name)) })
    const editor = createEditor()
    await editor.open('first')
    const saving = editor.saveAnyway()
    await vi.waitFor(() => expect(reads).toBe(2))
    await editor.open('second')
    editor.updateField('slack_channel_id', 'C-SECOND')
    reseed.resolve(savedChannel('first'))
    expect(await saving).toMatchObject({ status: 'superseded', outcome: { name: 'first' } })
    expect(editor.form.payload()).toMatchObject({ name: 'second', slack_channel_id: 'C-SECOND' })
  })

  it('open() hydrates from channels.get + catalog and starts clean', async () => {
    const editor = createEditor()
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
    const first = createEditor()
    await first.open('team-slack')
    expect(catalogCalls()).toBe(1)

    const second = createEditor()
    await second.open('team-slack')
    expect(catalogCalls()).toBe(1)

    await second.refreshCatalog()
    expect(catalogCalls()).toBe(2)
  })

  it('coalesces concurrent catalog fetches into one request', async () => {
    const [a, b] = await Promise.all([
      ensureChannelCatalog(setupCatalog),
      ensureChannelCatalog(setupCatalog),
    ])
    expect(a).toBe(b)
    expect(catalogCalls()).toBe(1)
  })

  it('tracks edited fields against the loaded baseline, in spec order', async () => {
    const editor = createEditor()
    await editor.open('team-slack')
    editor.updateField('slack_channel_id', 'C42')
    editor.updateField('connection_mode', 'socket')
    expect(editor.editedFields.value).toEqual(['connection_mode', 'slack_channel_id'])
    editor.discard()
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.editedFields.value).toEqual([])
  })

  it('testDraft probes the current draft without any redaction sentinel', async () => {
    const editor = createEditor()
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
    const editor = createEditor()
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
    const editor = createEditor()
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
    const editor = createEditor()
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
    const editor = createEditor()
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
    const editor = createEditor()
    await editor.startCompose('slack', { existingNames: ['feishu', 'ops-bot'] })
    const nameRow = editor.panel.value.channelFields.find(row => row.field.name === 'name')
    expect(nameRow?.value).toBe('slack')
    // The prefill is part of the pristine draft: no dirty flag, no edited tick.
    expect(editor.form.isDirty.value).toBe(false)
    expect(editor.editedFields.value).toEqual([])
  })

  it('startCompose increments the suggestion past existing names of ANY type', async () => {
    const editor = createEditor()
    // A non-slack channel literally named "slack" still collides — the name
    // is the global identity key and upsert would overwrite it.
    await editor.startCompose('slack', { existingNames: ['Slack', 'slack-2', 'feishu'] })
    const nameRow = editor.panel.value.channelFields.find(row => row.field.name === 'name')
    expect(nameRow?.value).toBe('slack-3')
  })

  it('startCompose leaves the name blank when the caller has no name list yet', async () => {
    const editor = createEditor()
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
    const editor = createEditor()
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
    const editor = createEditor()
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
    const editor = createEditor()
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
