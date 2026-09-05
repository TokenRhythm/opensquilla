/**
 * promptEnhancer.ts
 *
 * Pure, dependency-free prompt enhancement for the composer.
 *
 * The enhancer turns a rough, unstructured user instruction into a
 * structured prompt that is easier for a model to follow, following the
 * same dimensions the community prompt-optimizer projects use:
 *
 *   1. Role  - who the model should act as (when the user implies one)
 *   2. Task  - what to do, stated as an explicit objective
 *   3. Constraint - output format / length / tone guardrails
 *   4. Context - missing background the model would otherwise guess at
 *
 * Beyond the base scaffolding, the enhancer detects the TASK TYPE and
 * injects a type-specific scaffold (pre-flight checks, deliverable
 * contract, and anti-patterns), distilled from a general agent-method
 * library:
 *
 *   - Spec-driven development: "do X and verify Y", explicit "out of
 *     scope", machine-checkable acceptance criteria.
 *   - Unified cognition model (7-stage pipeline) x method library:
 *     the 8 task types below map onto which stages matter and which
 *     methods to load.
 *
 * Design constraints (deliberate):
 *  - PURE / DETERMINISTIC: no network calls, no model calls, no state.
 *    This keeps the feature offline, privacy-friendly, and unit-testable.
 *  - NEVER SENDS: the returned text replaces the composer draft. The user
 *    reviews it and decides to send. This feature must not hijack the
 *    normal send path.
 *  - IDEMPOTENT-ish: enhancing an already-enhanced prompt is cheap and
 *    returns a coherent structure again (it does not stack banners).
 */

export interface PromptEnhanceOptions {
  /** Language hint: 'zh' produces Chinese scaffolding, 'en' English. */
  locale?: 'zh' | 'en'
}

export interface PromptEnhanceResult {
  /** The enhanced prompt text, ready to replace the composer draft. */
  text: string
  /** Whether any enhancement actually happened (false for empty input). */
  enhanced: boolean
  /** Human-readable dimensions applied, for tooltips/tests. */
  dimensions: string[]
}

/** Task types the enhancer can detect and scaffold for. */
export type TaskType =
  | 'analyze'
  | 'design'
  | 'implement'
  | 'review'
  | 'write'
  | 'translate'
  | 'query'
  | 'orchestrate'

const TRIMMABLE_SUFFIXES = ['.', '。', '!', '！', '?', '？', ';', '；']

/**
 * Normalise user input: trim, collapse repeated blank lines, drop a
 * trailing sentence-period so a generated scaffold sentence can follow.
 */
function normaliseInput(raw: string): string {
  const collapsed = raw.replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim()
  if (!collapsed) return ''
  let text = collapsed
  while (
    text.length > 1
    && TRIMMABLE_SUFFIXES.includes(text[text.length - 1])
    && !text.endsWith('...')
  ) {
    text = text.slice(0, -1).trimEnd()
  }
  return text.trim()
}

/** Rough CJK ratio heuristic: > 0.3 => treat as Chinese content. */
function looksChinese(text: string): boolean {
  const cjk = (text.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) ?? []).length
  if (cjk === 0) return false
  return cjk / Math.max(text.length, 1) > 0.3
}

const QUESTION_WORDS = /(^|[\s,，。;；])(what|how|why|when|where|who|which|is|are|can|do|does|should|能否|如何|怎么|为什么|何时|哪里|谁|是不是|能否|可以)/i

function isQuestionLike(text: string): boolean {
  return QUESTION_WORDS.test(text)
}

const LEADING_VERBS = /^(请|帮我|麻烦|我想|我要|please|help me|i want|i need|can you|could you)\s*/i

/** Map a leading verb/adverb onto a plausible role the model can adopt. */
function inferRole(text: string, zh: boolean): string | null {
  const t = text.toLowerCase()
  if (/(代码|code|程序|函数|bug|重构|review|debug|python|javascript|typescript|shell|sql)/.test(t)) {
    return zh ? '资深软件工程师（注重代码可读性、健壮性与最佳实践）' : 'a senior software engineer focused on readable, robust, idiomatic code'
  }
  if (/(文案|营销|广告|标题|slogan|copywriting|marketing)/.test(t)) {
    return zh ? '资深文案与营销专家' : 'an experienced copywriter and marketing strategist'
  }
  if (/(翻译|translate)/.test(t)) {
    return zh ? '专业的笔译与本地化专家' : 'a professional translator and localisation specialist'
  }
  if (/(总结|摘要|归纳|summariz)/.test(t)) {
    return zh ? '严谨的分析师（擅长提炼要点与归纳结构）' : 'a rigorous analyst skilled at distilling key points'
  }
  if (/(方案|计划|规划|架构|plan|proposal|design)/.test(t)) {
    return zh ? '资深解决方案架构师' : 'a senior solutions architect'
  }
  if (/(数据|分析|报表|dashboard|data|analytics|统计)/.test(t)) {
    return zh ? '资深数据分析师' : 'a senior data analyst'
  }
  return null
}

/**
 * Detect the task type from the user's instruction.
 *
 * Matching is keyword-driven and deterministic. Priority matters: more
 * specific signals (translate, query) are checked before broad ones
 * (write, implement). A compound task ("analyze then write a report")
 * returns its primary type; the model can still follow both.
 */
export function detectTaskType(text: string): TaskType {
  const t = text.toLowerCase()

  // Translate: cross-language conversion.
  if (/(翻译|译成|译作|中译英|英译中|中译日|translate|translation|into english|into chinese|into japanese|into french|into german)/.test(t)) {
    return 'translate'
  }

  // Query: find a factual answer, no new content generated.
  if (/(^(查|查询|查找|搜|搜索|查一下|看看|查查|what is|what's|who is|when did|where is|how many|is there|does|多少钱|几点|在哪里|是什么|有没有)|^(帮我|麻烦|请)(查|搜索|搜|看看|查查|查一下))/i.test(t.trim())
    || /[?？](?!.*(代码|写|实现|设计|方案|review|分析))/.test(t) && t.length < 60) {
    return 'query'
  }

  // Review / audit: judge existing output.
  if (/(审查|评审|审计|巡检|检查|复查|找.?bug|找问题|review|audit|inspect|code review|检查一下|看看.*(合理|问题|怎么样))/.test(t)) {
    return 'review'
  }

  // Analyze: break down data/facts/phenomena.
  if (/(分析|解读|归因|复盘|对比|比较|总结|归纳|提炼|摘要|why is|why did|原因是什么|为什么)/.test(t)) {
    return 'analyze'
  }

  // Design: produce a plan/architecture/proposal, no implementation.
  if (/(设计|方案|规划|架构|选型|how should i|how to design|proposal|architecture|blueprint|roadmap)/.test(t)) {
    return 'design'
  }

  // Write: produce human-readable prose.
  if (/(写一份|起草|润色|改写|扩写|提炼|写个|撰写|写篇|write|draft|polish|rewrite|expand|compose|essay|article|report|email|post|story|poem)/.test(t)) {
    return 'write'
  }

  // Orchestrate / coordinate: multi-step, cross-session tracking.
  if (/(安排|跟踪|推进|协调|跟进|schedul|track|coordinate|follow.?up|plan.*steps|项目.*进度|任务.*列表)/.test(t)) {
    return 'orchestrate'
  }

  // Implement: land a concrete artifact (code/file/config).
  if (/(写|实现|开发|构建|造|修|改|implement|build|create|make|fix|add|write.*code|写.*代码)/.test(t)) {
    return 'implement'
  }

  // Fallback: treat as a general implement-ish task.
  return 'implement'
}

interface TaskScaffold {
  /** Pre-flight checks: what the model should confirm before acting. */
  checks: readonly [string, string]
  /** Deliverable contract: what the output must contain. */
  deliverables: readonly string[]
  /** Anti-patterns: what the model must avoid. */
  antiPatterns: readonly [string, string]
}

/** Type-specific scaffold, localised per language. */
const TASK_SCAFFOLDS: Record<TaskType, {
  zh: TaskScaffold
  en: TaskScaffold
}> = {
  analyze: {
    zh: {
      checks: ['数据/来源是否可信、口径是否一致？', '结论是否都有至少一个数据点支撑？'],
      deliverables: ['对比表或结论 + 证据链', '归因路径（如适用）'],
      antiPatterns: ['不要逐个罗列数据而不提炼结论', '不要只验证单个数据点就下结论'],
    },
    en: {
      checks: ['Is the data/source trustworthy and the definitions consistent?', 'Does every conclusion have at least one data point behind it?'],
      deliverables: ['comparison table or conclusion with an evidence chain', 'attribution path (if applicable)'],
      antiPatterns: ['do not just list data points without drawing conclusions', 'do not conclude from a single data point'],
    },
  },
  design: {
    zh: {
      checks: ['约束清单（成本/时间/技术/兼容性）是否完整？', '是否已调研现有可选方案？'],
      deliverables: ['≥2 个候选方案 + 对比表', '明确推荐 + 备选退路'],
      antiPatterns: ['不要只给一个方案没有退路', '不要用形容词代替具体指标'],
    },
    en: {
      checks: ['Is the constraint list (cost/time/tech/compatibility) complete?', 'Have existing alternatives been researched?'],
      deliverables: ['at least 2 candidate options with a comparison table', 'a clear recommendation and a fallback path'],
      antiPatterns: ['do not offer a single option with no fallback', 'do not use adjectives where concrete metrics are needed'],
    },
  },
  implement: {
    zh: {
      checks: ['目标环境与依赖是什么？', '如何验证它真的可用？'],
      deliverables: ['可运行的产物（代码/文件/配置）', '验证方式或自测步骤'],
      antiPatterns: ['不要只给片段不说明怎么跑', '不要写完就结束而不验证'],
    },
    en: {
      checks: ['What is the target environment and its dependencies?', 'How will you verify it actually works?'],
      deliverables: ['a runnable artifact (code/file/config)', 'a verification step or self-test'],
      antiPatterns: ['do not hand over a snippet without saying how to run it', 'do not claim done without verifying'],
    },
  },
  review: {
    zh: {
      checks: ['审查标准与检查清单是否显式约定？', '评审对象/范围是什么？'],
      deliverables: ['问题清单（位置 + 严重度 + 修复建议）', '通过 / 不通过判定'],
      antiPatterns: ['不要只挑问题不给修复建议', '不要全量平推找茬而不分优先级'],
    },
    en: {
      checks: ['Is the review standard/checklist explicit?', 'What exactly is in scope?'],
      deliverables: ['an issue list (location + severity + fix suggestion)', 'a pass / fail verdict'],
      antiPatterns: ['do not report problems without suggesting fixes', 'do not spread evenly over everything without prioritising'],
    },
  },
  write: {
    zh: {
      checks: ['读者是谁？交付格式（Markdown/邮件/报告）？', '篇幅上限与语气？'],
      deliverables: ['结构清晰的成稿', '贴近目标受众、可直接交付'],
      antiPatterns: ['不要绕弯子说废话', '不要虚构不存在的来源或数据'],
    },
    en: {
      checks: ['Who is the audience and what is the format (Markdown/email/report)?', 'What is the length limit and tone?'],
      deliverables: ['a well-structured draft', 'something tailored to the audience and ready to ship'],
      antiPatterns: ['do not pad with filler', 'do not invent sources or data'],
    },
  },
  translate: {
    zh: {
      checks: ['源语言与目标语言？专业领域术语表？', '风格：正式 / 口语？'],
      deliverables: ['译文 + 术语对照（如适用）', '存疑处显式标注'],
      antiPatterns: ['不要逐字硬翻', '不要前后术语不一致'],
    },
    en: {
      checks: ['Source and target languages? Domain terminology glossary?', 'Register: formal or casual?'],
      deliverables: ['the translation with a terminology glossary (if applicable)', 'explicit markers where the meaning is uncertain'],
      antiPatterns: ['do not translate word-for-word', 'do not use inconsistent terminology'],
    },
  },
  query: {
    zh: {
      checks: ['前提是否成立（时间/地点/口径）？', '信息来源是否可靠？'],
      deliverables: ['直接答案 + 来源'],
      antiPatterns: ['不要用“根据搜索结果…”开头绕弯', '不要编造来源'],
    },
    en: {
      checks: ['Do the preconditions hold (time/place/definition)?', 'Is the source reliable?'],
      deliverables: ['a direct answer with its source'],
      antiPatterns: ['do not pad with "according to search results…"', 'do not fabricate a source'],
    },
  },
  orchestrate: {
    zh: {
      checks: ['现有状态/依赖/阻塞点是什么？', '任务可拆成哪些可独立交付的步骤？'],
      deliverables: ['状态可见的步骤列表 + 依赖关系', '下一步行动建议'],
      antiPatterns: ['不要依赖记忆而不用文件记录', '不要目标漂移'],
    },
    en: {
      checks: ['What is the current state, dependencies, and blockers?', 'Which independently deliverable steps can the task be split into?'],
      deliverables: ['a visible step list with dependencies', 'a suggested next action'],
      antiPatterns: ['do not rely on memory instead of written records', 'do not let the goal drift'],
    },
  },
}

const TASK_TYPE_LABEL_ZH: Record<TaskType, string> = {
  analyze: '分析',
  design: '设计',
  implement: '实现',
  review: '审查',
  write: '写作',
  translate: '翻译',
  query: '查询',
  orchestrate: '协作规划',
}

const TASK_TYPE_LABEL_EN: Record<TaskType, string> = {
  analyze: 'analysis',
  design: 'design',
  implement: 'implementation',
  review: 'review',
  write: 'writing',
  translate: 'translation',
  query: 'query',
  orchestrate: 'orchestration',
}

/**
 * Build the structured, enhanced prompt.
 *
 * The exact framing matters less than giving the model an explicit
 * objective, a role, output guardrails and a place for context. We keep
 * the scaffolding tight so it reads naturally for both CLI and chat use.
 */
export function enhancePrompt(raw: string, options: PromptEnhanceOptions = {}): PromptEnhanceResult {
  const text = normaliseInput(raw)
  if (!text) {
    return { text: '', enhanced: false, dimensions: [] }
  }

  const zh = options.locale === 'zh' || (options.locale !== 'en' && looksChinese(text))
  const dimensions: string[] = []
  const role = inferRole(text, zh)
  const taskType = detectTaskType(text)
  const scaffold = TASK_SCAFFOLDS[taskType][zh ? 'zh' : 'en']

  const lines: string[] = []
  if (role) {
    dimensions.push(zh ? '角色定义' : 'role')
    lines.push(zh ? `请你扮演${role}。` : `Act as ${role}.`)
  }
  dimensions.push(zh ? '任务目标' : 'task')
  if (zh) {
    lines.push(`请完成以下任务：`)
  } else {
    lines.push('Complete the following task:')
  }

  const body = LEADING_VERBS.test(text)
    ? text.replace(LEADING_VERBS, '').replace(/^\s*[，,]\s*/, '').trim()
    : text

  if (isQuestionLike(body) && zh) {
    lines.push(`「${body}」`)
  } else if (isQuestionLike(body)) {
    lines.push(`“${body}”`)
  } else {
    lines.push(body)
  }

  // Type-specific scaffold: pre-flight checks + deliverable contract.
  dimensions.push(zh ? `类型-${TASK_TYPE_LABEL_ZH[taskType]}` : `type-${TASK_TYPE_LABEL_EN[taskType]}`)

  if (zh) {
    lines.push('')
    lines.push('开始前请先确认：')
    for (const c of scaffold.checks) lines.push(`- ${c}`)
    lines.push('')
    lines.push('输出要求：')
    lines.push('- 直接给出最终结果，不要复述任务；')
    lines.push('- 结构清晰，适当使用分段或列表；')
    lines.push(`- 交付内容应包含：${scaffold.deliverables.join('；')}；`)
    lines.push('- 若信息不足，先提出一个明确的问题，不要臆测。')
    lines.push('')
    lines.push('注意避免：')
    for (const a of scaffold.antiPatterns) lines.push(`- ${a}`)
    lines.push('')
    lines.push('（本段为自动增强生成的提示词，请按需编辑后发送。）')
  } else {
    lines.push('')
    lines.push('Before you begin, confirm:')
    for (const c of scaffold.checks) lines.push(`- ${c}`)
    lines.push('')
    lines.push('Output requirements:')
    lines.push('- Give the final answer directly; do not restate the task.')
    lines.push('- Use clear structure with paragraphs or lists where helpful.')
    lines.push(`- The deliverable should include: ${scaffold.deliverables.join('; ')}.`)
    lines.push('- If information is missing, ask one precise question instead of guessing.')
    lines.push('')
    lines.push('Avoid:')
    for (const a of scaffold.antiPatterns) lines.push(`- ${a}`)
    lines.push('')
    lines.push('(This is an auto-enhanced prompt. Edit as needed before sending.)')
  }

  return {
    text: lines.join('\n'),
    enhanced: true,
    dimensions,
  }
}
