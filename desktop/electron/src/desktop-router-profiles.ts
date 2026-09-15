import type { RouterTier } from './router-tier-normalization.js'

function textRouterProfile(
  provider: string,
  c0: string,
  c1: string,
  c2: string,
  c3: string,
  subject: string,
): Record<string, RouterTier> {
  return {
    c0: { provider, model: c0, description: `${subject} fast route`, thinkingLevel: 'off' },
    c1: { provider, model: c1, description: `${subject} balanced route`, thinkingLevel: 'low' },
    c2: { provider, model: c2, description: `${subject} strong route`, thinkingLevel: 'medium' },
    c3: { provider, model: c3, description: `${subject} highest route`, thinkingLevel: 'high' },
  }
}

function minimaxRouterProfile(provider: string): Record<string, RouterTier> {
  return textRouterProfile(
    provider,
    'MiniMax-M2.7',
    'MiniMax-M2.7',
    'MiniMax-M3',
    'MiniMax-M3',
    'MiniMax',
  )
}

export const ROUTER_PROFILES: Record<string, Record<string, RouterTier>> = {
  tokenrhythm: {
    c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash-0731', description: 'Fast DeepSeek V4 Flash 0731 route for simple work' },
    c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro-0813', description: 'Default DeepSeek V4 Pro 0813 route for normal agent work' },
    c2: { provider: 'tokenrhythm', model: 'kimi-k2.7-code', description: 'Strong Kimi 2.7 Code route for harder coding and analysis' },
    c3: { provider: 'tokenrhythm', model: 'glm-5.2', description: 'Highest tier: shared B5 fusion; GLM 5.2 is retained for single-model C3 mode', ensembleEnabled: true },
    image_model: { provider: 'tokenrhythm', model: 'kimi-k2.6', description: 'Vision route for image attachments', imageOnly: true },
  },
  openrouter: {
    c0: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash', description: 'Fast everyday work', thinkingLevel: 'high' },
    c1: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro', description: 'Balanced agent work', thinkingLevel: 'high' },
    c2: { provider: 'openrouter', model: 'z-ai/glm-5.2', description: 'Complex reasoning', thinkingLevel: 'high' },
    c3: { provider: 'openrouter', model: 'anthropic/claude-opus-4.8', description: 'Highest quality review and planning', thinkingLevel: 'high' },
    image_model: { provider: 'openrouter', model: 'moonshotai/kimi-k2.6', description: 'Vision route for image attachments', imageOnly: true, thinkingLevel: 'medium' },
  },
  openai: {
    c0: { provider: 'openai', model: 'gpt-5.4-nano', description: 'Fast simple work', thinkingLevel: 'none' },
    c1: { provider: 'openai', model: 'gpt-5.4-mini', description: 'Balanced agent work', thinkingLevel: 'low' },
    c2: { provider: 'openai', model: 'gpt-5.5', description: 'Complex text tasks', thinkingLevel: 'medium' },
    c3: { provider: 'openai', model: 'gpt-5.5', description: 'Deep review and analysis', thinkingLevel: 'high' },
  },
  dashscope: {
    c0: { provider: 'dashscope', model: 'qwen3.6-flash', description: 'Fast simple work' },
    c1: { provider: 'dashscope', model: 'qwen3.7-plus', description: 'Balanced agent work' },
    c2: { provider: 'dashscope', model: 'qwen3.7-max', description: 'Complex text tasks' },
    c3: { provider: 'dashscope', model: 'qwen3.7-max', description: 'Deep reasoning' },
  },
  deepseek: {
    c0: { provider: 'deepseek', model: 'deepseek-v4-flash', description: 'Fast simple work' },
    c1: { provider: 'deepseek', model: 'deepseek-v4-flash', description: 'Balanced agent work' },
    c2: { provider: 'deepseek', model: 'deepseek-v4-pro', description: 'Complex text tasks' },
    c3: { provider: 'deepseek', model: 'deepseek-v4-pro', description: 'Deep reasoning' },
  },
  gemini: {
    c0: { provider: 'gemini', model: 'gemini-3.1-flash-lite', description: 'Fast simple work' },
    c1: { provider: 'gemini', model: 'gemini-3.5-flash', description: 'Balanced agent work', thinkingLevel: 'low' },
    c2: { provider: 'gemini', model: 'gemini-3.1-pro-preview', description: 'Complex text tasks', thinkingLevel: 'medium' },
    c3: { provider: 'gemini', model: 'gemini-3.1-pro-preview', description: 'Deep reasoning', thinkingLevel: 'high' },
  },
  moonshot: {
    c0: { provider: 'moonshot', model: 'kimi-k2.6', description: 'Fast multimodal work', thinkingLevel: 'low' },
    c1: { provider: 'moonshot', model: 'kimi-k2.6', description: 'Balanced multimodal work', thinkingLevel: 'medium' },
    c2: { provider: 'moonshot', model: 'kimi-k2.6', description: 'Complex text and image work', thinkingLevel: 'medium' },
    c3: { provider: 'moonshot', model: 'kimi-k2.7-code', description: 'Code-heavy deep reasoning', thinkingLevel: 'high' },
  },
  kimi_coding_openai: textRouterProfile(
    'kimi_coding_openai',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'Kimi Coding',
  ),
  kimi_coding_anthropic: textRouterProfile(
    'kimi_coding_anthropic',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'Kimi Coding',
  ),
  volcengine: {
    c0: { provider: 'volcengine', model: 'doubao-seed-2-0-lite-260215', description: 'Fast simple work' },
    c1: { provider: 'volcengine', model: 'doubao-seed-2-0-lite-260215', description: 'Balanced agent work' },
    c2: { provider: 'volcengine', model: 'doubao-seed-2-0-pro-260215', description: 'Complex text tasks' },
    c3: { provider: 'volcengine', model: 'doubao-seed-2-0-pro-260215', description: 'Deep review and analysis' },
  },
  volcengine_coding_plan: textRouterProfile(
    'volcengine_coding_plan',
    'doubao-seed-2.0-lite',
    'doubao-seed-2.0-pro',
    'doubao-seed-2.0-code',
    'doubao-seed-2.0-code',
    'Volcengine Coding Plan',
  ),
  zhipu: {
    c0: { provider: 'zhipu', model: 'glm-5-turbo', description: 'Fast simple work' },
    c1: { provider: 'zhipu', model: 'glm-5', description: 'Balanced agent work' },
    c2: { provider: 'zhipu', model: 'glm-5.1', description: 'Complex text tasks' },
    c3: { provider: 'zhipu', model: 'glm-5.2', description: 'Deep reasoning', thinkingLevel: 'high' },
  },
  minimax: minimaxRouterProfile('minimax'),
  minimax_cn: minimaxRouterProfile('minimax_cn'),
  minimax_global: minimaxRouterProfile('minimax_global'),
  minimax_coding_openai: minimaxRouterProfile('minimax_coding_openai'),
  minimax_coding_anthropic: minimaxRouterProfile('minimax_coding_anthropic'),
  mimo_openai: textRouterProfile(
    'mimo_openai',
    'mimo-v2.5',
    'mimo-v2.5',
    'mimo-v2.5-pro',
    'mimo-v2.5-pro',
    'MiMo',
  ),
  mimo_anthropic: textRouterProfile(
    'mimo_anthropic',
    'mimo-v2.5',
    'mimo-v2.5',
    'mimo-v2.5-pro',
    'mimo-v2.5-pro',
    'MiMo',
  ),
}

function cloneRouterTiers(tiers: Record<string, RouterTier>): Record<string, RouterTier> {
  return Object.fromEntries(Object.entries(tiers).map(([name, tier]) => [name, { ...tier }]))
}

export function defaultRouterTiers(provider: string, mode: string): Record<string, RouterTier> {
  if (mode === 'disabled') return {}
  if (mode === 'openrouter-mix') return cloneRouterTiers(ROUTER_PROFILES.openrouter)
  return cloneRouterTiers(ROUTER_PROFILES[provider] || ROUTER_PROFILES.openrouter)
}
