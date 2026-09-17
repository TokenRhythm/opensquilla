import { describe, expect, it } from 'vitest'
import { localizedCronTemplate } from './templateNames'

describe('localized cron templates', () => {
  it('localizes display fields without replacing the executable task prompt', () => {
    const template = {
      id: 'project-risk',
      title: '项目风险巡检',
      description: '展示简介',
      category: '项目研发',
      scheduleLabel: '工作日 10:00',
      name: '项目风险巡检',
      message: '完整提示词：不要执行删除、发布或修改生产配置等不可逆操作。',
    }
    const translations: Record<string, string> = {
      'cronSkills.view.templates.project-risk.title': 'Project risk check',
      'cronSkills.view.templates.project-risk.description': 'Localized summary',
      'cronSkills.view.templates.project-risk.category': 'Projects',
      'cronSkills.view.templates.project-risk.schedule': 'Weekdays at 10:00',
    }

    const localized = localizedCronTemplate(
      template,
      key => translations[key] || key,
    )

    expect(localized.name).toBe('Project risk check')
    expect(localized.description).toBe('Localized summary')
    expect(localized.message).toBe(template.message)
  })
})
