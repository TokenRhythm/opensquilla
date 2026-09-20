/**
 * purposeToOrbState.ts — 将 AssistantActivityPurposeCode + lifecycle
 * 映射到 ThinkingOrb 的动画状态。
 *
 * 优先级（明确定义）：
 * 1. lifecycle 为 `answering` → 恒为 `composing`，purpose 不参与。
 *    模型正在产出最终回答是一个「阶段级」信号，比单个工具调用的目的更稳定，
 *    中途的 read/search 尾巴不应打断创作态。
 * 2. lifecycle 为 `working` → purpose 细化：每个动作有对应视觉反馈；
 *    未知 purpose 回退 `working`。
 * 3. 终态（settled / interrupted / failed）→ 回退 `working`。
 *    当前集成点只在 live（working/answering）时渲染 orb，该分支仅作为
 *    纯函数的防御性兜底，保证任意输入都有确定输出。
 *
 * purpose code 兼容两套前缀（见 utils/chat/assistantActivity.ts）：
 * - 基础/过去时：`chat.activity.purpose.<suffix>`
 * - 进行中（活跃 cluster 实际下发的形态）：`chat.activity.purposeRunning.<suffix>`
 */

import type { OrbState } from './presets'

/** purpose 后缀（去掉前缀后的部分），与 AssistantActivityPurposeBaseCode 对齐 */
const PURPOSE_SUFFIXES = [
  'discover',
  'search',
  'read',
  'inspect',
  'change',
  'run',
  'create',
  'recall',
  'use',
] as const

type PurposeSuffix = (typeof PURPOSE_SUFFIXES)[number]

/** lifecycle 值，与 AssistantActivityLifecycle 对齐 */
export type OrbLifecycle = 'working' | 'answering' | 'settled' | 'interrupted' | 'failed'

/**
 * purpose → orbState 细化映射（仅 working 阶段生效）
 */
const PURPOSE_MAP: Record<PurposeSuffix, OrbState> = {
  discover:  'searching',   // 发现 → 地球仪扫描
  search:    'searching',   // 搜索 → 地球仪扫描
  read:      'listening',   // 读取 → 声波
  inspect:   'listening',   // 审查 → 声波
  change:    'solving',     // 修改 → 魔方
  run:       'connecting',  // 执行 → 轨道旋转
  create:    'solving',     // 创建 → 魔方
  recall:    'breathing',   // 回忆 → 呼吸光环
  use:       'working',     // 通用工具 → 星座连线
}

/** 活跃 cluster 的进行时前缀（makeCluster 会用 RUNNING_PURPOSE_CODES 替换） */
const PURPOSE_PREFIXES = [
  'chat.activity.purpose.',
  'chat.activity.purposeRunning.',
] as const

/**
 * 从 purpose code 中提取后缀；无法识别时返回 null。
 */
export function extractPurposeSuffix(code: string | null | undefined): PurposeSuffix | null {
  if (!code) return null
  for (const prefix of PURPOSE_PREFIXES) {
    if (code.startsWith(prefix)) {
      const suffix = code.slice(prefix.length)
      return (PURPOSE_SUFFIXES as readonly string[]).includes(suffix)
        ? (suffix as PurposeSuffix)
        : null
    }
  }
  return null
}

/**
 * 将 lifecycle + purpose code 映射为 ThinkingOrb 动画状态。
 *
 * @param lifecycle - 当前生命周期（来自 AssistantActivityLifecycle）
 * @param purposeCode - 完整 purpose code（`purpose.*` 或 `purposeRunning.*` 前缀均可），可选
 * @returns ThinkingOrb 动画状态
 */
export function resolveOrbState(
  lifecycle: OrbLifecycle,
  purposeCode?: string | null,
): OrbState {
  // 1. answering 恒为 composing：阶段级信号优先于工具级 purpose
  if (lifecycle === 'answering') return 'composing'
  // 2. working：purpose 细化，未知或缺失回退 working
  if (lifecycle === 'working') {
    const suffix = extractPurposeSuffix(purposeCode)
    return suffix ? PURPOSE_MAP[suffix] : 'working'
  }
  // 3. 终态防御性兜底（集成点不会以终态渲染 orb）
  return 'working'
}
