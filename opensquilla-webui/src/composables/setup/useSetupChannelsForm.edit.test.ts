import { describe, expect, it } from 'vitest'
import { REDACTED_SENTINEL } from './channelRpc'
import { useSetupChannelsForm } from './useSetupChannelsForm'

const SLACK_SPEC = {
  type: 'slack',
  label: 'Slack',
  fields: [
    { name: 'name', label: 'Channel name', required: true },
    { name: 'connection_mode', label: 'Connection mode', default: 'socket' },
    { name: 'token', label: 'Bot token', secret: true, required: true },
    { name: 'app_token', label: 'App token', secret: true, showWhen: { connection_mode: 'socket' } },
    { name: 'signing_secret', label: 'Signing secret', secret: true, showWhen: { connection_mode: 'webhook' } },
    { name: 'reply_in_thread', label: 'Reply in thread', default: false },
  ],
}

const STORED_ENTRY = {
  name: 'team-slack',
  type: 'slack',
  connection_mode: 'socket',
  token: REDACTED_SENTINEL,
  app_token: REDACTED_SENTINEL,
  reply_in_thread: true,
  agent_id: 'main', // not a spec field here → passthrough
}

function editForm() {
  const form = useSetupChannelsForm()
  form.initFromEntry(SLACK_SPEC, { ...STORED_ENTRY }, ['token', 'app_token'])
  return form
}

describe('channel edit mode', () => {
  it('seeds non-secret values from the entry, pristine form is not dirty', () => {
    const form = editForm()
    expect(form.isEditing.value).toBe(true)
    expect(form.isDirty.value).toBe(false)
    expect(form.payload().reply_in_thread).toBe(true)
  })

  it("'***' firewall: untouched stored secrets are absent from the payload, never echoed", () => {
    const form = editForm()
    const payload = form.payload()
    expect(JSON.stringify(payload)).not.toContain(REDACTED_SENTINEL)
    expect('token' in payload).toBe(false)
    expect('app_token' in payload).toBe(false)
    expect(payload.name).toBe('team-slack')
  })

  it('replace flow: opening the box is clean, typing is dirty, cancel restores clean', () => {
    const form = editForm()
    form.replaceSecret('token')
    expect(form.isDirty.value).toBe(false)
    form.updateField('token', 'xoxb-new')
    expect(form.isDirty.value).toBe(true)
    expect(form.payload().token).toBe('xoxb-new')
    form.cancelSecretReplace('token')
    expect(form.isDirty.value).toBe(false)
    expect('token' in form.payload()).toBe(false)
  })

  it('showWhen hides the inactive mode secret and keeps it out of the payload', () => {
    const form = editForm()
    form.replaceSecret('app_token')
    form.updateField('app_token', 'xapp-visible')
    form.updateField('connection_mode', 'webhook')
    const payload = form.payload()
    expect('app_token' in payload).toBe(false)
    expect('signing_secret' in payload).toBe(false)
  })

  it('passthrough keys survive an edit round-trip', () => {
    const form = editForm()
    expect(form.payload().agent_id).toBe('main')
  })

  it('compose mode also scrubs a pasted literal sentinel', () => {
    const form = useSetupChannelsForm()
    form.selectChannelType('slack')
    form.resetForSpec(SLACK_SPEC)
    form.updateField('name', 'x')
    form.updateField('token', REDACTED_SENTINEL)
    expect(JSON.stringify(form.payload())).not.toContain(REDACTED_SENTINEL)
  })
})
