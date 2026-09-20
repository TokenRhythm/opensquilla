/**
 * 全局共享时钟 —— 独创性设计
 *
 * 原版 thinking-orbs 每个实例跑自己的 requestAnimationFrame 循环，
 * 彼此独立，不同步。
 *
 * 本设计改为全局单例时钟：
 * 1. 所有 ThinkingOrb 实例共享同一个时钟源
 * 2. 所有实例的动画相位自然同步
 * 3. 只在至少有一个实例活跃时才运行 rAF，空闲时自动休眠
 * 4. 支持自适应帧率：根据性能自动降帧（60 → 30 → 15 fps）
 */

type TickCallback = (time: number, delta: number) => void

let _rafId = 0
let _running = false
let _lastTime = 0
let _callbacks = new Set<TickCallback>()
let _fps = 60
let _frameInterval = 1000 / 60
let _lastFrameTime = 0
let _frameCount = 0
let _lastFpsCheck = 0

function _tick(now: number) {
  if (!_running) return

  // 自适应帧率：每秒检查一次实际帧率
  _frameCount++
  if (now - _lastFpsCheck >= 1000) {
    const actualFps = _frameCount / ((now - _lastFpsCheck) / 1000)
    _lastFpsCheck = now
    _frameCount = 0

    // 如果实际帧率低于目标帧率的 70%，降级
    if (actualFps < _fps * 0.7 && _fps > 15) {
      _fps = Math.max(15, _fps / 2)
      _frameInterval = 1000 / _fps
    } else if (actualFps > _fps * 0.95 && _fps < 60) {
      // 如果性能充裕，升级
      _fps = Math.min(60, _fps * 2)
      _frameInterval = 1000 / _fps
    }
  }

  // 帧率控制：跳过过早的帧
  if (now - _lastFrameTime < _frameInterval - 1) {
    _rafId = requestAnimationFrame(_tick)
    return
  }
  _lastFrameTime = now

  const delta = _lastTime ? (now - _lastTime) / 1000 : 0.016
  _lastTime = now
  const time = now / 1000

  // 通知所有订阅者
  for (const cb of _callbacks) {
    try {
      cb(time, delta)
    } catch {
      // 单个回调出错不影响其他
    }
  }

  _rafId = requestAnimationFrame(_tick)
}

/** 注册一个时钟回调，返回取消注册的函数 */
export function subscribeToClock(cb: TickCallback): () => void {
  _callbacks.add(cb)
  _startIfNeeded()
  return () => {
    _callbacks.delete(cb)
    _stopIfIdle()
  }
}

function _startIfNeeded() {
  if (!_running && _callbacks.size > 0) {
    _running = true
    _lastTime = 0
    _lastFrameTime = 0
    _frameCount = 0
    _lastFpsCheck = 0
    _fps = 60
    _frameInterval = 1000 / 60
    _rafId = requestAnimationFrame(_tick)
  }
}

function _stopIfIdle() {
  if (_running && _callbacks.size === 0) {
    _running = false
    cancelAnimationFrame(_rafId)
    _lastTime = 0
  }
}

/** 获取当前目标帧率 */
export function getCurrentFps(): number {
  return _fps
}

/** 重置时钟（用于清理测试） */
export function resetClock(): void {
  _running = false
  cancelAnimationFrame(_rafId)
  _callbacks.clear()
  _lastTime = 0
}