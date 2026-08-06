/**
 * purposeToOrbState.ts — 将 OpenSquilla 的 AssistantActivityPurposeCode
 * 映射到 ThinkingOrb 的 9 种动画状态
 *
 * 映射逻辑：
 * - lifecycle 决定基础状态（working→orbits, answering→ribbon）
 * - purpose 在 lifecycle 基础上细化，让 agent 的每个动作都有对应的视觉反馈
 * - 未知 purpose 或未提供 purpose 时，回退到 lifecycle 的基础映射
 */

import type { OrbState } from './presets'

/** AssistantActivityPurposeCode 的后缀部分（去掉 'chat.activity.purpose.' 前缀） */
type PurposeSuffix =
  | 'discover'
  | 'search'
  | 'read'
  | 'inspect'
  | 'change'
  | 'run'
  | 'create'
  | 'recall'
  | 'use'

/** lifecycle 值 */
type Lifecycle = 'working' | 'answering' | 'settled' | 'interrupted' | 'failed'

/**
 * purpose → orbState 映射表
 * 只定义活跃生命周期（working/answering）下的细化映射
 * settled/interrupted/failed 时动画不显示，无需映射
 */
const PURPOSE_MAP: Record<PurposeSuffix, OrbState> = {
  discover:  'searching',   // 发现 → globe 扫描
  search:    'searching',   // 搜索 → globe 扫描
  read:      'listening',   // 读取 → wave 波形
  inspect:   'listening',   // 审查 → wave 波形
  change:    'solving',     // 修改 → rubik 魔方
  run:       'working',     // 执行 → orbits 轨道
  create:    'solving',     // 创建 → rubik 魔方
  recall:    'breathing',   // 回忆 → ring 脉搏
  use:       'connecting',  // 使用 → web 星座
}

/**
 * lifecycle 的基础回退映射
 */
const LIFECYCLE_FALLBACK: Record<Lifecycle, OrbState> = {
  working:     'working',
  answering:   'composing',
  settled:     'working',
  interrupted: 'working',
  failed:      'working',
}

/**
 * 从 purpose code 中提取后缀
 * 'chat.activity.purpose.search' → 'search'
 */
function extractPurposeSuffix(code: string): PurposeSuffix | null {
  const prefix = 'chat.activity.purpose.'
  if (code.startsWith(prefix)) {
    const suffix = code.slice(prefix.length) as PurposeSuffix
    return suffix
  }
  return null
}

/**
 * 将 lifecycle + purpose code 映射为 ThinkingOrb 的动画状态
 *
 * @param lifecycle - 当前生命周期（来自 AssistantActivityLifecycle）
 * @param purposeCode - 完整 purpose code（如 'chat.activity.purpose.search'），可选
 * @returns ThinkingOrb 动画状态
 */
export function resolveOrbState(
  lifecycle: Lifecycle,
  purposeCode?: string | null,
): OrbState {
  // 如果提供了 purpose code，尝试细化映射
  if (purposeCode) {
    const suffix = extractPurposeSuffix(purposeCode)
    if (suffix && suffix in PURPOSE_MAP) {
      return PURPOSE_MAP[suffix]
    }
  }
  // 回退到 lifecycle 基础映射
  return LIFECYCLE_FALLBACK[lifecycle] ?? 'working'
}