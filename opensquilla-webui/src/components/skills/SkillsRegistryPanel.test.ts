// @vitest-environment happy-dom

import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it } from 'vitest'
import SkillsRegistryPanel from './SkillsRegistryPanel.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  while (apps.length) apps.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('SkillsRegistryPanel install provenance', () => {
  it('shows the source and trust level before installation', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp({
      setup: () => () => h(SkillsRegistryPanel, {
        registryQuery: 'calendar',
        githubUrl: '',
        results: [{
          name: 'community-calendar',
          description: 'Community calendar integration',
          source: 'github',
          trust_level: 'community',
          identifier: 'example/community-calendar',
          installed: false,
        }],
        loading: false,
        installingId: null,
      }),
    })
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      missingWarn: false,
      fallbackWarn: false,
      messages: { en: {} },
    }))
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(host.textContent).toContain('github')
    expect(host.textContent).toContain('community')
  })
})
