import { beforeEach, describe, expect, it } from 'vitest'

import i18n, { loadLocaleMessages } from '@/i18n'
import type { Skill } from '@/types/skills'
import {
  skillProviderCheckAtLaunch,
  skillStatusChipClass,
  skillStatusChipText,
  skillStatusDotClass,
  skillStatusDotTitle,
} from './useSkillsCatalog'

describe('provider-backed MetaSkill catalog status', () => {
  beforeEach(() => {
    i18n.global.locale.value = 'en'
  })

  it('uses a neutral launch-time status instead of green Ready', () => {
    const skill: Skill = {
      name: 'provider-backed-meta',
      status: 'ready',
      status_detail: 'Ready — 0/0 dependencies satisfied',
      provider_check_at_launch: true,
    }

    expect(skillProviderCheckAtLaunch(skill)).toBe(true)
    expect(skillStatusDotClass(skill)).toBe('is-provider-check')
    expect(skillStatusDotTitle(skill)).toBe('Provider will be checked at launch')
    expect(skillStatusChipClass(skill)).toBe('sk-chip--unverified')
    expect(skillStatusChipText(skill)).toBe('Provider will be checked at launch')
  })

  it('keeps local setup failures authoritative', () => {
    const skill: Skill = {
      name: 'provider-backed-meta',
      status: 'needs_setup',
      provider_check_at_launch: true,
    }

    expect(skillProviderCheckAtLaunch(skill)).toBe(false)
    expect(skillStatusDotClass(skill)).toBe('is-needs')
    expect(skillStatusChipClass(skill)).toBe('sk-chip--warn')
    expect(skillStatusChipText(skill)).toBe('needs deps')
  })

  it('uses the launch-time provider status when no local dependencies are declared', () => {
    const skill: Skill = {
      name: 'provider-backed-meta',
      status: 'not_declared',
      provider_check_at_launch: true,
    }

    expect(skillProviderCheckAtLaunch(skill)).toBe(true)
    expect(skillStatusDotClass(skill)).toBe('is-provider-check')
    expect(skillStatusChipText(skill)).toBe('Provider will be checked at launch')
  })

  it('localizes the launch-time status', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    const skill: Skill = {
      name: 'provider-backed-meta',
      status: 'ready',
      provider_check_at_launch: true,
    }

    expect(skillStatusChipText(skill)).toBe('服务商将在启动时检查')
  })
})
