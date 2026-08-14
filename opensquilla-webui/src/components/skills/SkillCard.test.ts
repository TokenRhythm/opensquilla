// @vitest-environment happy-dom

import { createApp, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import SkillCard from '@/components/skills/SkillCard.vue'
import i18n from '@/i18n'

const apps: ReturnType<typeof createApp>[] = []

beforeEach(() => {
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('SkillCard provider status', () => {
  it('renders a neutral launch-time provider label for an otherwise-ready MetaSkill', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(SkillCard, {
      skill: {
        name: 'meta-short-drama',
        description: 'Generate a short drama.',
        kind: 'meta',
        status: 'ready',
        provider_check_at_launch: true,
      },
    })
    app.use(i18n)
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.querySelector('.sk-card__provider-status')?.textContent?.trim())
      .toBe('Provider will be checked at launch')
    expect(host.querySelector('.sk-card__dot')?.classList.contains('is-provider-check')).toBe(true)
    expect(host.querySelector('.sk-card__dot')?.classList.contains('is-ready')).toBe(false)
  })
})
