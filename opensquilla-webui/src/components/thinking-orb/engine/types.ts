/**
 * 动画模式插件接口 —— 独创性设计
 *
 * 原版 thinking-orbs 用函数式 ModeDraw，每个模式是一个纯函数。
 * 本设计改用类 + 生命周期模式：
 * - 每个模式是一个 class，实现 AnimationMode 接口
 * - 有 init() / update() / destroy() 生命周期
 * - 支持状态切换过渡
 * - 可插拔：注册新模式只需实现接口并注册
 */

import type { RenderResult, Projector } from './core'

/** 模式配置参数 */
export interface ModeConfig {
  /** 模式名称 */
  name: string
  /** 默认配置参数 */
  defaults: Record<string, number>
}

/** 每帧更新的上下文 */
export interface FrameContext {
  /** 归一化时间 */
  time: number
  /** 帧间隔（秒） */
  delta: number
  /** 画布尺寸（CSS px） */
  size: number
  /** 是否暗色主题 */
  dark: boolean
  /** 速度倍率 */
  speed: number
  /** 投影矩阵 */
  projector: Projector
  /** 运行时参数 */
  opts: Record<string, number>
}

/**
 * 动画模式接口
 *
 * 实现此接口可注册自定义动画模式。
 * 生命周期：init → update (每帧) → destroy
 */
export interface AnimationMode {
  /** 模式配置 */
  readonly config: ModeConfig

  /**
   * 初始化 —— 在模式第一次使用前调用
   * 可用于预计算、生成固定粒子等
   */
  init(): void

  /**
   * 每帧更新 —— 返回当前帧的渲染结果
   * 通过多层 RenderResult 支持复杂合成
   */
  update(ctx: FrameContext): RenderResult

  /**
   * 销毁 —— 清理资源
   */
  destroy(): void
}

/**
 * 模式构造函数类型
 */
export type ModeConstructor = new () => AnimationMode