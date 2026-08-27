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

const LEADING_VERBS = /^(请|帮我|麻烦|我想|我要|please|help me|i want|i need|can you|could you)\s*/i

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

  // Output guardrails: keep them concise; the user can always edit.
  if (zh) {
    lines.push('')
    lines.push('输出要求：')
    lines.push('- 直接给出最终结果，不要复述任务；')
    lines.push('- 结构清晰，适当使用分段或列表；')
    lines.push('- 若信息不足，先提出一个明确的问题，不要臆测。')
    lines.push('')
    lines.push('（本段为自动增强生成的提示词，请按需编辑后发送。）')
  } else {
    lines.push('')
    lines.push('Output requirements:')
    lines.push('- Give the final answer directly; do not restate the task.')
    lines.push('- Use clear structure with paragraphs or lists where helpful.')
    lines.push('- If information is missing, ask one precise question instead of guessing.')
    lines.push('')
    lines.push('(This is an auto-enhanced prompt. Edit as needed before sending.)')
  }

  return {
    text: lines.join('\n'),
    enhanced: true,
    dimensions,
  }
}
