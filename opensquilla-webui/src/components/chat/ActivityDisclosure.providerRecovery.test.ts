// @vitest-environment happy-dom
import { createApp, h, nextTick, ref, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { StatusPart } from '@/types/parts'
import {
  projectAssistantActivityTimeline,
  providerActivityLabel,
} from '@/utils/chat/assistantActivity'
import { clearAssistantActivityExpansionState } from '@/utils/chat/activityDisclosureState'
import ActivityDisclosure from './ActivityDisclosure.vue'
import AssistantActivityTimeline from './AssistantActivityTimeline.vue'
import UnifiedAssistantActivityTimeline from './UnifiedAssistantActivityTimeline.vue'
import chatViewSource from '@/views/ChatView.vue?raw'

const apps: App[] = []
const now = Date.now()
function waitStep(): StatusPart {
  return {
    action: 'provider:rate_limited:20', label: 'Rate limited', at: now, activityOrder: 1,
    providerRecovery: { phase: 'retry_wait', reason: 'rate_limited', startedAt: now, retryAfterMs: 20_000, httpStatus: 429 },
    httpStatus: 429,
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(now)
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
  clearAssistantActivityExpansionState()
})
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('one owner for the current provider wait', () => {
  it.each(['en', 'zh-Hans'] as const)('shows fallback names in the existing process row in %s', async locale => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    const settled = ref(false)
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({ render() {
      const projection = projectAssistantActivityTimeline([], {
        lifecycle: settled.value ? 'settled' : 'working',
        statusHistory: [{ action: 'provider:fallback', label: '', at: now, activityOrder: 1,
          providerRecovery: { phase: 'fallback', reason: 'provider_overloaded', model: 'vendor/model-b', fromModel: 'vendor/model-a' } }],
      })
      return h(AssistantActivityTimeline, {
        projection,
        isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
        toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      })
    } })
    apps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()
    expect(host.querySelectorAll('.assistant-activity-status__row')).toHaveLength(1)
    expect(host.textContent).toContain(locale === 'en' ? 'Trying a backup model · vendor/model-a → vendor/model-b' : '正在尝试备用模型 · vendor/model-a → vendor/model-b')
    settled.value = true
    await nextTick()
    expect(host.textContent).toContain(locale === 'en' ? 'Tried a backup model · vendor/model-a → vendor/model-b' : '曾尝试备用模型 · vendor/model-a → vendor/model-b')
    expect(host.querySelector('.assistant-activity-status__row--current')).toBeNull()
  })
  it.each(['ordered', 'legacy'] as const)('renders the wait once with the %s timeline and retains its later history', async mode => {
    const stage = ref<'waiting' | 'retrying' | 'settled'>('waiting')
    const elapsed = ref('21s')
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({ render() {
      const lifecycle = stage.value === 'settled' ? 'settled' : 'working'
      const statusHistory = [waitStep()]
      if (stage.value !== 'waiting') statusHistory.push({
        action: 'provider:retrying:1:3', label: 'Retrying', at: now + 20_000, activityOrder: 2,
        providerRecovery: { phase: 'retrying', reason: 'rate_limited', attempt: 1, limit: 3 },
      })
      const projection = projectAssistantActivityTimeline([], { lifecycle, statusHistory, endedAt: now + 21_000 })
      const current = projection.statusSteps.find(step => step.isCurrent)
      const waiting = current?.providerRecovery?.phase === 'retry_wait'
      const label = current && providerActivityLabel(current, now)
      const announcement = current && providerActivityLabel(current, now, true)
      const timeline = mode === 'ordered' ? UnifiedAssistantActivityTimeline : AssistantActivityTimeline
      return h(ActivityDisclosure, {
        lifecycle, defaultOpen: true, stepCount: 0, failureCount: 0,
        stateKey: 'provider-wait-regression',
        phaseLabel: label ? String(i18n.global.t(label.code, label.params)) : '',
        phaseAnnouncement: announcement ? String(i18n.global.t(announcement.code, announcement.params)) : '',
        phaseWaiting: waiting,
        elapsedLabel: elapsed.value,
      }, { default: () => h(timeline, {
        projection,
        isToolGroupOpen: () => undefined, isToolItemOpen: () => undefined,
        toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
      }) })
    } })
    apps.push(app)
    app.use(i18n)
    app.mount(host)
    await nextTick()

    const header = host.querySelector('.assistant-activity__header')
    const announcement = host.querySelector('[role="status"]')
    expect(header?.textContent?.trim()).toBe('Time spent 21s')
    expect(host.querySelectorAll('.assistant-activity-status__row')).toHaveLength(1)
    expect(host.querySelector('.assistant-activity-status__row')?.textContent).toContain('retrying in about 20s')
    expect(host.textContent?.match(/HTTP 429/g)).toHaveLength(1)
    expect(header?.textContent).not.toMatch(/429|retry|step/i)
    expect(announcement?.textContent).not.toContain('20s')
    const stableAnnouncement = announcement?.textContent
    await vi.advanceTimersByTimeAsync(1000)
    elapsed.value = '22s'
    await nextTick()
    expect(header?.textContent?.trim()).toBe('Time spent 22s')
    expect(host.querySelector('.assistant-activity-status__row')?.textContent).toContain('retrying in about 19s')
    expect(announcement?.textContent).toBe(stableAnnouncement)
    expect(host.textContent?.match(/HTTP 429/g)).toHaveLength(1)

    // The projection does not own or reset a user's disclosure choice.
    ;(host.querySelector('.assistant-activity__live-head') as HTMLButtonElement).click()
    await nextTick()
    expect(host.querySelector('.assistant-activity')?.getAttribute('data-share-expanded')).toBe('false')
    stage.value = 'retrying'
    await nextTick()
    expect(host.querySelector('.assistant-activity')?.getAttribute('data-share-expanded')).toBe('false')
    expect(header?.textContent?.trim()).toBe('Time spent 22s')
    expect(host.querySelectorAll('.assistant-activity-status__row')).toHaveLength(2)
    expect(host.querySelector('.assistant-activity-status__row--current')?.textContent).toContain('Retrying 1/3')
    expect(host.textContent).toContain('Waited for the model rate limit')
    expect(host.querySelector('.assistant-activity__live-head')?.textContent).not.toContain('HTTP 429')

    ;(host.querySelector('.assistant-activity__live-head') as HTMLButtonElement).click()
    await nextTick()
    stage.value = 'settled'
    await nextTick()
    expect(host.querySelector('.assistant-activity')?.getAttribute('data-share-expanded')).toBe('true')
    expect(host.textContent).toContain('Waited for the model rate limit')
    expect(host.textContent).toContain('Retried the model request')
    expect(host.textContent).not.toContain('retrying in about')
  })

  it('keeps provider waits in both ChatView display projections while tool previews replace only their preparation step', () => {
    for (const name of ['UnifiedAssistantActivityTimeline', 'AssistantActivityTimeline']) {
      const opening = chatViewSource.match(new RegExp(`<${name}\\b[^>]*>`))?.[0]
      expect(opening).toContain(':projection="presentedLive.liveActivityDisplayProjection"')
    }
    expect(chatViewSource).toContain("!(step.isCurrent && step.label.code === 'chat.activity.lifecycle.preparingToolCall')")
    expect(chatViewSource).not.toContain('withoutHeaderProviderWait')
  })
})
