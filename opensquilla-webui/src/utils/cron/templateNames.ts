const TEMPLATE_NAME_KEYS: Record<string, string> = {
  '每日 AI 资讯简报': 'ai-daily',
  '每周工作复盘': 'weekly-report',
  '每天 5 个英语单词': 'english-five',
  '项目风险巡检': 'project-risk',
  '知识库周回顾': 'knowledge-review',
  '每日灵感与冷知识': 'daily-idea',
  '每日儿童睡前故事': 'bedtime-story',
  '经典电影推荐': 'classic-movie',
  '历史上的今天': 'today-in-history',
}

interface LocalizableCronTemplate {
  id: string
  title: string
  description: string
  category: string
  scheduleLabel: string
  name?: string
  message?: string
}

export function localizedCronTemplate<T extends LocalizableCronTemplate>(
  template: T,
  translate: (key: string) => string,
): T {
  const key = `cronSkills.view.templates.${template.id}`
  const title = translate(`${key}.title`)
  return {
    ...template,
    title,
    description: translate(`${key}.description`),
    category: translate(`${key}.category`),
    scheduleLabel: translate(`${key}.schedule`),
    name: title,
  }
}

export function localizedCronJobName(
  name: string | undefined,
  id: string,
  translate: (key: string) => string,
): string {
  const raw = name || id
  const templateId = TEMPLATE_NAME_KEYS[raw]
  return templateId
    ? translate(`cronSkills.view.templates.${templateId}.title`)
    : raw
}
