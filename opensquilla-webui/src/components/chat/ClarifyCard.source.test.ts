import { describe, expect, it } from 'vitest'

import source from './ClarifyCard.vue?raw'

describe('ClarifyCard submit feedback', () => {
  it('shows immediate visible feedback while a clarify reply is being sent', () => {
    // Localized (i18n) but the feedback contract is unchanged: a busy/idle submit
    // label, a live submit-status row, and the "reply received" outcome title.
    expect(source).toContain("busy ? t('chat.clarify.sendingReply') : t('chat.clarify.sendReply')")
    expect(source).toContain('data-testid="clarify-submit-status"')
    expect(source).toContain("t('chat.clarify.sendingContinuing')")
    expect(source).toContain("t('chat.clarify.replyReceived')")
  })

  it('renders a prominent submitted banner instead of a low-contrast text row', () => {
    expect(source).toContain('clarify-outcome__icon')
    expect(source).toContain('clarify-outcome__title')
    expect(source).toContain('clarify-outcome__detail')
    expect(source).toContain('class="{ \'is-busy\': busy }"')
    expect(source).toContain('border: 1px solid color-mix(in srgb, var(--ok) 42%, var(--border));')
    expect(source).toContain('box-shadow: 0 8px 22px color-mix(in srgb, var(--ok) 10%, transparent);')
  })

  it('allows an empty clarify reply so the backend can continue with defaults/autofill', () => {
    expect(source).toContain(':disabled="busy"')
    expect(source).not.toContain(':disabled="busy || !canSubmit"')
    expect(source).not.toContain('if (Object.keys(fields).length === 0) return')
  })

  it('preloads schema defaults as editable presets', () => {
    expect(source).toContain("values[field.name] = field.defaultValue || ''")
    expect(source).toContain(":placeholder=\"field.defaultValue ? `default: ${field.defaultValue}` : ''\"")
  })

  it('preserves entered values when the same clarify request is re-rendered', () => {
    expect(source).toContain("() => `${props.request.runId}\\u0000${props.request.step}`")
    expect(source).not.toContain('watch(() => props.request, request =>')
  })
})
