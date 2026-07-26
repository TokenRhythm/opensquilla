import { describe, it, expect } from 'vitest'
import { computed } from 'vue'
import { useSetupChannelsForm, buildChannelEntry } from './useSetupChannelsForm'

// Regression: channel fields must respect show_when so users see fields for the
// SELECTED connection mode, not every field at once (Slack socket vs webhook,
// Telegram polling vs webhook). The backend ships show_when as field.showWhen.

const slackFields = [
  { name: 'token', label: 'Bot token' },
  { name: 'app_token', label: 'App token', showWhen: { connection_mode: 'socket' } },
  { name: 'signing_secret', label: 'Signing secret', showWhen: { connection_mode: 'webhook' } },
  { name: 'connection_mode', label: 'Connection mode', default: 'webhook' },
]
const slack = { type: 'slack', label: 'Slack', fields: slackFields }

function panelFor(form: ReturnType<typeof useSetupChannelsForm>) {
  return form.createPanel(computed(() => slackFields))
}
const names = (panel: { value: { channelFields: Array<{ field: { name: string } }> } }) =>
  panel.value.channelFields.map((r) => r.field.name)

describe('useSetupChannelsForm — show_when field visibility', () => {
  it('shows only the fields for the default connection mode (webhook)', () => {
    const f = useSetupChannelsForm()
    f.resetForSpec(slack)
    const panel = panelFor(f)
    const shown = names(panel)
    expect(shown).toContain('token') // unconditional
    expect(shown).toContain('connection_mode') // the controller
    expect(shown).toContain('signing_secret') // webhook-only, default is webhook
    expect(shown).not.toContain('app_token') // socket-only, hidden under webhook
  })

  it('flips visible fields when the connection mode changes', () => {
    const f = useSetupChannelsForm()
    f.resetForSpec(slack)
    const panel = panelFor(f)
    f.updateField('connection_mode', 'socket')
    const shown = names(panel)
    expect(shown).toContain('app_token') // socket-only now visible
    expect(shown).not.toContain('signing_secret') // webhook-only now hidden
  })

  it('payload drops a hidden field’s stale value', () => {
    const f = useSetupChannelsForm()
    // The gallery leaves nothing preselected; pick the type explicitly.
    f.selectChannelType('slack')
    f.resetForSpec(slack)
    // user was in webhook, typed a signing secret, then switched to socket
    f.updateField('signing_secret', 'shh-secret')
    f.updateField('connection_mode', 'socket')
    f.updateField('app_token', 'xapp-123')
    const p = f.payload()
    expect(p.type).toBe('slack')
    expect(p.app_token).toBe('xapp-123') // visible in socket → sent
    expect(p.signing_secret).toBeUndefined() // hidden in socket → dropped
    expect(p.connection_mode).toBe('socket')
  })
})

describe('buildChannelEntry', () => {
  it('drops empty values and stamps the type', () => {
    expect(buildChannelEntry('telegram', { token: 't', webhook_url: '' }))
      .toEqual({ type: 'telegram', token: 't' })
  })
})
