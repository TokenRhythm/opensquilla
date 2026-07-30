import type { IconName } from '@/utils/icons'

// Well-known skills get an intentional icon. Similar skills may deliberately
// share a glyph: semantic recognition is more important than artificial
// uniqueness.
const EXACT_SKILL_ICONS: Record<string, IconName> = {
  'audio-cog': 'music',
  'advanced-dubbing-studio': 'microphone',
  'voiceover-studio': 'microphone',
  'voice-conversion-studio': 'volume',
  'voice-clone-lab': 'user',
  'music-and-singing-studio': 'music',
  docx: 'fileText',
  xlsx: 'table',
  pptx: 'monitor',
  'pdf-toolkit': 'fileText',
  'nano-pdf': 'fileText',
  'html-to-pdf': 'fileCode',
  'html-coder': 'fileCode',
  'latex-compile': 'fileCode',
  'text-file-read': 'fileText',
  filesystem: 'save',
  'git-diff': 'fork',
  github: 'share',
  'http-fetch': 'download',
  'web-search': 'search',
  'multi-search-engine': 'search',
  'deep-research': 'search',
  weather: 'cloud',
  'history-explorer': 'clock',
  cron: 'cron',
  memory: 'sessions',
  tmux: 'monitor',
  'sub-agent': 'agents',
  'swe-bench': 'listChecks',
  'code-task': 'fileCode',
  'meta-skill-creator': 'skills',
  'skill-creator': 'skills',
  'skill-creator-smoke-test': 'check',
  'skill-creator-proposals': 'listChecks',
  'skill-creator-linter': 'check',
  'meta-kid-project-planner': 'listChecks',
  'meta-short-drama': 'play',
  'meta-paper-write': 'fileText',
  'title-card-image': 'image',
  'nano-banana-pro': 'image',
  'nano-banana-pro-openrouter': 'image',
  'openrouter-video-generator': 'play',
  'video-still-animator': 'play',
  'video-merger': 'play',
  'seedance-2-prompt': 'play',
  'ai-video-script': 'fileText',
  'srt-from-script': 'languages',
  'subtitle-burner': 'languages',
  'AwesomeWebpageMetaSkill': 'home',
  'awesome-webpage-research': 'search',
  'awesome-webpage-image-download': 'image',
}

const SEMANTIC_RULES: Array<{ terms: string[]; icon: IconName }> = [
  { terms: ['spreadsheet', 'excel', 'xlsx', 'table'], icon: 'table' },
  { terms: ['slide', 'deck', 'ppt', 'presentation'], icon: 'monitor' },
  { terms: ['image', 'banana', 'photo', 'visual'], icon: 'image' },
  { terms: ['video', 'drama', 'animation'], icon: 'play' },
  { terms: ['audio', 'music', 'singing'], icon: 'music' },
  { terms: ['voice', 'dubbing'], icon: 'microphone' },
  { terms: ['search', 'research', 'arxiv', 'source-curator'], icon: 'search' },
  { terms: ['github', 'pull-request', '-pr-', 'git-'], icon: 'fork' },
  { terms: ['stack-trace', 'code', 'html', 'latex'], icon: 'fileCode' },
  { terms: ['pdf', 'paper', 'document', 'docx', 'author', 'writing'], icon: 'fileText' },
  { terms: ['security', 'compliance', 'audit'], icon: 'shield' },
  { terms: ['schedule', 'cron', 'morning-digest', 'watchdog'], icon: 'clock' },
  { terms: ['travel', 'weather'], icon: 'cloud' },
  { terms: ['agent', 'assistant'], icon: 'agents' },
  { terms: ['workflow', 'pipeline', 'planner', 'project'], icon: 'listChecks' },
  { terms: ['webpage', 'web-', 'http', 'fetch'], icon: 'home' },
  { terms: ['skill'], icon: 'skills' },
  { terms: ['history', 'memory'], icon: 'sessions' },
  { terms: ['file', 'export', 'migration'], icon: 'save' },
]

export function assignedFallbackIcon(name: string): IconName {
  const exact = EXACT_SKILL_ICONS[name]
  if (exact) return exact

  const normalized = name.toLowerCase()
  const match = SEMANTIC_RULES.find(rule =>
    rule.terms.some(term => normalized.includes(term)),
  )
  return match?.icon ?? 'skills'
}
