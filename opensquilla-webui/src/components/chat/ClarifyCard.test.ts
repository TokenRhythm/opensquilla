// @vitest-environment happy-dom
import { createApp, h, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import type { ChatClarifyRequest } from '@/composables/chat/useChatApprovals'
import ClarifyCard from './ClarifyCard.vue'

const mountedApps: ReturnType<typeof createApp>[] = []

function planQuestionnaire(): ChatClarifyRequest {
  return {
    intro: 'Confirm the deployment choices.',
    presentation: 'plan_questionnaire_v1',
    requestId: 'request-plan-1',
    runId: 'run-1',
    step: 'clarify',
    fields: [
      {
        name: 'target',
        header: 'Target',
        prompt: 'Where should this run?',
        type: 'enum',
        required: true,
        defaultValue: '',
        choices: ['Local', 'Cloud'],
        options: [
          { label: 'Local', description: 'Run on this machine.' },
          { label: 'Cloud', description: 'Run on a remote host.' },
        ],
        allowOther: true,
      },
      {
        name: 'release',
        header: 'Release',
        prompt: 'Which release channel?',
        type: 'enum',
        required: true,
        defaultValue: '',
        choices: ['Stable', 'Preview'],
        allowOther: true,
      },
      {
        name: 'notes',
        header: 'Notes',
        prompt: 'Any final constraints?',
        type: 'text',
        required: false,
        defaultValue: '',
        choices: [],
      },
    ],
  }
}

function mountCard(
  request: ChatClarifyRequest,
  props: Record<string, unknown> = {},
) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const onSubmit = vi.fn()
  const app = createApp({
    render: () => h(ClarifyCard, { request, onSubmit, ...props }),
  })
  mountedApps.push(app)
  app.use(i18n)
  app.mount(host)
  return { host, onSubmit }
}

function choose(host: HTMLElement, label: string) {
  const input = Array.from(host.querySelectorAll<HTMLInputElement>('input[type="radio"]'))
    .find(candidate => candidate.value === label)
  expect(input).toBeTruthy()
  input?.click()
}

afterEach(() => {
  while (mountedApps.length) mountedApps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('ClarifyCard Plan questionnaire', () => {
  it('shows one question at a time and preserves answers while navigating', async () => {
    const { host } = mountCard(planQuestionnaire())
    await nextTick()

    expect(host.querySelectorAll('.clarify-field')).toHaveLength(1)
    expect(host.textContent).toContain('Target')
    expect(host.textContent).not.toContain('Release')
    expect(host.textContent).not.toContain('Confirm the deployment choices.')
    expect(host.querySelector('[data-testid="clarify-question-progress"]')?.textContent)
      .toContain('1 / 3')

    const previous = host.querySelector<HTMLButtonElement>('[data-testid="clarify-previous"]')
    const next = host.querySelector<HTMLButtonElement>('[data-testid="clarify-next"]')
    expect(previous?.disabled).toBe(true)
    expect(next?.disabled).toBe(true)

    choose(host, 'Local')
    await nextTick()
    expect(next?.disabled).toBe(false)
    next?.click()
    await nextTick()

    expect(host.textContent).toContain('Release')
    expect(host.textContent).not.toContain('Where should this run?')
    previous?.click()
    await nextTick()
    expect(host.querySelector<HTMLInputElement>('input[value="Local"]')?.checked).toBe(true)
  })

  it('merges Other and its text input into one selectable row', async () => {
    const { host } = mountCard(planQuestionnaire())
    await nextTick()

    const otherRow = host.querySelector<HTMLElement>('.clarify-choice--other')
    const otherInput = otherRow?.querySelector<HTMLInputElement>('input[type="text"]')
    const otherRadio = otherRow?.querySelector<HTMLInputElement>('input[type="radio"]')

    expect(otherRow).toBeTruthy()
    expect(otherInput).toBeTruthy()
    expect(otherRadio?.getAttribute('aria-label')).toBe(i18n.global.t('chat.clarify.other'))
    expect(host.querySelector('.clarify-field__other')).toBeNull()

    otherInput?.focus()
    if (otherInput) {
      otherInput.value = 'Private relay'
      otherInput.dispatchEvent(new Event('input', { bubbles: true }))
    }
    await nextTick()

    expect(otherRadio?.checked).toBe(true)
    expect(host.querySelector<HTMLButtonElement>('[data-testid="clarify-next"]')?.disabled)
      .toBe(false)
  })

  it('submits the collected answers only from the last question', async () => {
    const { host, onSubmit } = mountCard(planQuestionnaire())
    await nextTick()

    choose(host, 'Cloud')
    await nextTick()
    host.querySelector<HTMLButtonElement>('[data-testid="clarify-next"]')?.click()
    await nextTick()
    choose(host, 'Stable')
    await nextTick()
    host.querySelector<HTMLButtonElement>('[data-testid="clarify-next"]')?.click()
    await nextTick()

    expect(host.querySelector('[data-testid="clarify-next"]')).toBeNull()
    const submit = host.querySelector<HTMLButtonElement>('[data-testid="clarify-submit"]')
    expect(submit).toBeTruthy()
    submit?.click()
    await nextTick()

    expect(onSubmit).toHaveBeenCalledWith({ target: 'Cloud', release: 'Stable' })
  })

  it('keeps generic clarify forms multi-field with their dismiss action', async () => {
    const request = planQuestionnaire()
    delete request.presentation
    const { host } = mountCard(request)
    await nextTick()

    expect(host.querySelectorAll('.clarify-field')).toHaveLength(3)
    expect(host.querySelector('[data-testid="clarify-question-progress"]')).toBeNull()
    expect(host.textContent).toContain('Confirm the deployment choices.')
    expect(host.textContent).toContain(i18n.global.t('chat.clarify.dismiss'))
  })
})
