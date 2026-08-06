// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'

import i18n from '@/i18n'
import { useMetaSkillSetup, type MetaSetupStorage } from '@/composables/chat/useMetaSkillSetup'
import type { MetaSetupReadiness } from '@/types/metaSetup'
import MetaSkillSetupCard from './MetaSkillSetupCard.vue'

const SESSION = 'agent:main:webchat:manual-setup-flow'

async function settle(): Promise<void> {
  for (let index = 0; index < 10; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function memoryStorage(): MetaSetupStorage {
  const values = new Map<string, string>()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('MetaSkill setup manual recovery flow', () => {
  it('rechecks after a provider-settings handoff and continues the original launch', async () => {
    i18n.global.locale.value = 'en'
    const launchText = '/meta meta-short-drama -- Create a three-scene launch story'
    const blocked: MetaSetupReadiness = {
      ready: false,
      status: 'needs_setup',
      missing_env: ['OPENROUTER_API_KEY'],
      reasons: ['OPENROUTER_API_KEY is required'],
      setup_actions: [],
      manual_setup_actions: [{
        id: 'provider:openrouter',
        kind: 'provider_connection',
        provider_id: 'openrouter',
        label: 'OpenRouter',
        capability_ids: ['image.generate', 'video.generate'],
        available: true,
      }],
    }
    const call = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'meta.run') return { ok: true }
      throw new Error(`Unexpected RPC: ${method}`)
    })
    const dispatchHidden = vi.fn(async (
      _providerText: string,
      _displayText: string,
      clientRequestId = '',
    ) => ({
      status: 'accepted' as const,
      reason: 'accepted' as const,
      clientRequestId,
      sessionKey: SESSION,
    }))

    const Host = defineComponent({
      setup() {
        const setup = useMetaSkillSetup({
          rpc: {
            call: async <T = unknown>(method: string, params?: Record<string, unknown>) => (
              await call(method, params) as T
            ),
          },
          currentSessionKey: ref(SESSION),
          dispatchHidden,
          storage: memoryStorage(),
        })
        void setup.requestSetup('meta-short-drama', blocked, SESSION, launchText)

        return () => {
          const current = setup.setupState.value
          if (!current) return h('p', { 'data-testid': 'flow-launched' }, 'Launched')
          return h(MetaSkillSetupCard, {
            state: current,
            onConfirm: setup.confirmSetup,
            onRetry: setup.retrySetup,
            onCancel: setup.cancelSetup,
            onConfigure: (providerId: string) => {
              if (!setup.beginProviderHandoff(providerId)) return
              void setup.restoreSetupJob()
            },
          })
        }
      },
    })

    const root = document.createElement('div')
    document.body.appendChild(root)
    const app = createApp(Host)
    app.use(i18n)
    app.mount(root)
    await settle()

    expect(root.querySelector('[data-testid="meta-setup-provider"]')?.textContent)
      .toContain('Connect OpenRouter to continue')
    const configure = root.querySelector<HTMLButtonElement>(
      '[data-testid="meta-setup-configure-provider"]',
    )
    expect(configure?.textContent).toContain('Connect OpenRouter')

    configure?.click()
    await settle()

    expect(call.mock.calls.map(([method]) => method)).toEqual(['meta.run'])
    expect(dispatchHidden).toHaveBeenCalledWith(
      launchText,
      launchText,
      expect.any(String),
    )
    expect(root.querySelector('[data-testid="meta-setup-card"]')).toBeNull()
    expect(root.querySelector('[data-testid="flow-launched"]')).toBeTruthy()
    app.unmount()
  })
})
