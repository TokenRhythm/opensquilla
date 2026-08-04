/**
 * 动画模式注册表 —— 独创性插件系统
 *
 * 原版 thinking-orbs 用 flat registry 映射 string → function。
 * 本设计使用类注册系统，支持：
 * 1. 动态注册/注销模式
 * 2. 模式别名（一个状态名映射到多个模式）
 * 3. 过渡动画：切换模式时自动 cross-fade
 * 4. 懒加载：模式只在首次使用时初始化
 */

import type { AnimationMode, ModeConstructor } from './engine/types'

/** 已注册的模式条目 */
interface RegisteredMode {
  name: string
  instance: AnimationMode
  initialized: boolean
}

class ModeRegistry {
  private _modes = new Map<string, RegisteredMode>()
  private _constructors = new Map<string, ModeConstructor>()
  private _aliases = new Map<string, string>()

  /**
   * 注册一个模式构造函数
   * @param name 模式名称
   * @param ctor 构造函数
   */
  register(name: string, ctor: ModeConstructor): void {
    this._constructors.set(name, ctor)
  }

  /**
   * 注册别名
   * @param alias 别名
   * @param target 目标模式名
   */
  alias(alias: string, target: string): void {
    this._aliases.set(alias, target)
  }

  /**
   * 获取模式实例（懒加载初始化）
   */
  get(name: string): AnimationMode | undefined {
    const resolved = this._aliases.get(name) ?? name
    let entry = this._modes.get(resolved)
    if (!entry) {
      const ctor = this._constructors.get(resolved)
      if (!ctor) return undefined
      const instance = new ctor()
      entry = { name: resolved, instance, initialized: false }
      this._modes.set(resolved, entry)
    }
    if (!entry.initialized) {
      entry.instance.init()
      entry.initialized = true
    }
    return entry.instance
  }

  /**
   * 获取所有已注册的模式名
   */
  listModes(): string[] {
    return Array.from(this._constructors.keys())
  }

  /**
   * 获取所有别名
   */
  listAliases(): Record<string, string> {
    const result: Record<string, string> = {}
    for (const [alias, target] of this._aliases) {
      result[alias] = target
    }
    return result
  }

  /**
   * 取消注册
   */
  unregister(name: string): void {
    this._constructors.delete(name)
    this._aliases.delete(name)
    const entry = this._modes.get(name)
    if (entry) {
      entry.instance.destroy()
      this._modes.delete(name)
    }
  }

  /**
   * 清理所有模式
   */
  clear(): void {
    for (const [, entry] of this._modes) {
      entry.instance.destroy()
    }
    this._modes.clear()
    this._constructors.clear()
    this._aliases.clear()
  }
}

/** 全局单例注册表 */
export const modeRegistry = new ModeRegistry()