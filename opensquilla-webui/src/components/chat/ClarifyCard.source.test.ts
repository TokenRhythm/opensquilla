import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import chatViewSource from '@/views/ChatView.vue?raw'
import composerSource from './ChatComposer.vue?raw'
import source from './ClarifyCard.vue?raw'

const chatViewStyles = readFileSync(new URL('../../styles/chat-view.css', import.meta.url), 'utf8')

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
    expect(source).toContain("'is-busy': busy")
    expect(source).toContain('border: 1px solid color-mix(in srgb, var(--ok) 42%, var(--border));')
    expect(source).toContain('box-shadow: 0 8px 22px color-mix(in srgb, var(--ok) 10%, transparent);')
  })

  it('requires Plan questionnaire fields without tightening generic clarify forms', () => {
    expect(source).toContain(':disabled="busy || !canSubmit"')
    expect(source).toContain('!isPlanQuestionnaire.value')
    expect(source).toContain("props.request.presentation === 'plan_questionnaire_v1'")
    expect(source).toContain('v-if="!isPlanQuestionnaire"')
    expect(source).not.toContain('if (Object.keys(fields).length === 0) return')
  })

  it('preloads schema defaults as editable presets', () => {
    expect(source).toContain("const defaultValue = field.defaultValue || ''")
    expect(source).toContain('values[field.name] = defaultValue')
    expect(source).toContain(":placeholder=\"field.defaultValue ? `default: ${field.defaultValue}` : ''\"")
  })

  it('preserves entered values when the same clarify request is re-rendered', () => {
    expect(source).toContain("() => props.request.requestId || `${props.request.runId}\\u0000${props.request.step}`")
    expect(source).not.toContain('watch(() => props.request, request =>')
  })

  it('docks only the pending Plan questionnaire directly above the composer', () => {
    const dockIndex = chatViewSource.indexOf('<div class="chat-composer-dock">')
    const questionnaireIndex = chatViewSource.indexOf('class="plan-questionnaire-dock"', dockIndex)
    const composerIndex = chatViewSource.indexOf('<ChatComposer', dockIndex)

    expect(questionnaireIndex).toBeGreaterThan(dockIndex)
    expect(questionnaireIndex).toBeLessThan(composerIndex)
    expect(chatViewSource).toContain("presentation === 'plan_questionnaire_v1'")
    expect(chatViewSource).toContain(':docked="true"')
    expect(chatViewSource).toContain(':submitted="clarifySubmitted"')
    expect(chatViewSource).not.toContain("&& !clarifySubmitted.value")
    expect(chatViewSource).toContain(
      ':input-disabled="Boolean(dockedPlanQuestionnaire) || Boolean(forkTransition)"',
    )
    expect(composerSource).toContain(':disabled="inputDisabled"')
  })

  it('floats the Plan questionnaire without adding height to the composer dock', () => {
    expect(chatViewSource).toContain('class="plan-questionnaire-dock"')
    expect(chatViewSource).toContain('<div class="chat-composer-dock">')
    expect(chatViewStyles).toMatch(
      /\.plan-questionnaire-dock\s*\{[^}]*position:\s*absolute;[^}]*bottom:\s*calc\(100% \+ var\(--sp-2\)\);/s,
    )
    expect(chatViewStyles).toMatch(
      /\.plan-questionnaire-dock\s*>\s*\.clarify-card\s*\{[^}]*pointer-events:\s*auto;/s,
    )
    expect(chatViewSource).toContain('@wheel="handlePlanQuestionnaireWheel"')
    expect(chatViewStyles).toContain(
      '.chat--plan-questionnaire-open .chat-thread :deep(.clarify-card--plan)',
    )
  })

  it('uses restrained Plan-only presentation without changing generic outcome feedback', () => {
    expect(source).toContain("'clarify-card--plan': isPlanQuestionnaire")
    expect(source).toContain("'clarify-outcome--plan': isPlanQuestionnaire")
    expect(source).toContain('.clarify-card--plan')
    expect(source).toContain('box-shadow: none;')
    expect(source).toContain('animation: none;')
  })
})
