/**
 * useOrbAnimation — 动画循环 composable
 *
 * 独创性设计：
 * 1. 使用全局共享时钟（而非每个实例独立 rAF）
 * 2. 支持 IntersectionObserver 离屏暂停
 * 3. 支持 visibilitychange 标签页隐藏暂停
 * 4. 支持自适应帧率（通过全局时钟）
 * 5. 支持暂停/恢复控制
 * 6. prefers-reduced-motion 下只渲染一帧静态画面（不接时钟、不闪缩）
 * 7. Canvas 上下文 / 模式渲染异常时上报错误，由宿主降级为静态指示器
 */

import { ref, watch, onMounted, onUnmounted, type Ref } from 'vue'
import { subscribeToClock } from '../engine/clock'
import { renderLayers, emptyRenderResult } from '../engine/core'
import type { RenderResult } from '../engine/core'
import { resolvePreset, registerBuiltinModes, type OrbState } from '../presets'
import { modeRegistry } from '../registry'

export interface AnimationControls {
  /** 是否正在运行 */
  isRunning: Ref<boolean>
  /** 当前帧率 */
  currentFps: Ref<number>
  /** Canvas 2D 上下文不可用或模式渲染抛出异常（宿主应降级为静态指示器） */
  renderError: Ref<boolean>
  /** 暂停/恢复 */
  pause: () => void
  resume: () => void
}

/** 静态帧的时间相位：取周期中段附近，保证各模式都呈现有代表性的形态 */
const STATIC_PHASE = 1.5

/**
 * 驱动 Canvas 动画循环
 */
export function useOrbAnimation(
  canvasRef: Ref<HTMLCanvasElement | null>,
  state: Ref<OrbState>,
  size: Ref<number>,
  dark: Ref<boolean>,
  speed: Ref<number>,
  paused: Ref<boolean>,
  reduced: Ref<boolean>,
): AnimationControls {
  const isRunning = ref(false)
  const currentFps = ref(60)
  const renderError = ref(false)

  let unsubscribe: (() => void) | null = null
  let io: IntersectionObserver | null = null
  let visible = true
  let lastSize = 0
  let lastDpr = 1
  let staticMode = false
  let consecutiveModeErrors = 0

  // 确保内置模式已注册
  registerBuiltinModes()

  /** 单帧绘制。返回 false 表示本帧渲染失败且已升级为不可恢复（内部已停循环）。 */
  const paintFrame = (time: number): boolean => {
    const canvas = canvasRef.value
    if (!canvas) return true

    const failHard = () => {
      renderError.value = true
      detachLoop()
      isRunning.value = false
      return false
    }

    const ctx = canvas.getContext('2d')
    if (!ctx) return failHard()

    const dpr = Math.min(2, typeof devicePixelRatio !== 'undefined' ? devicePixelRatio : 1)
    const currentSize = size.value

    // 尺寸变化时重建 canvas buffer
    if (currentSize !== lastSize || dpr !== lastDpr) {
      canvas.width = Math.round(currentSize * dpr)
      canvas.height = Math.round(currentSize * dpr)
      lastSize = currentSize
      lastDpr = dpr
    }

    const { speed: baseSpeed, density, opts } = resolvePreset(state.value, currentSize)
    const effSpeed = baseSpeed * speed.value

    const modeInstance = modeRegistry.get(state.value)

    // 设置变换
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, currentSize, currentSize)

    if (!modeInstance) {
      // 注册表里找不到当前状态的绘制模式：无法产出任何视觉，上报降级
      return failHard()
    }

    const frameCtx = {
      time: time * effSpeed,
      delta: 0.016,
      size: currentSize,
      dark: dark.value,
      speed: effSpeed,
      projector: createDummyProjector(),
      opts: { ...opts, density },
    }

    let result: RenderResult
    try {
      result = modeInstance.update(frameCtx)
    } catch {
      consecutiveModeErrors += 1
      result = emptyRenderResult()
      if (consecutiveModeErrors >= 3) {
        // 同一模式连续 3 帧抛异常：视为该模式不可用，交还给宿主做 CSS 降级，
        // 而不是永久渲染一张空白画布。
        return failHard()
      }
    }
    if (result.main.particles.length || result.highlight.particles.length
      || result.background.particles.length) {
      consecutiveModeErrors = 0
    }

    renderLayers(ctx, result, dark.value)
    return true
  }

  const _drawFrame = (time: number) => {
    if (!canvasRef.value || !visible || paused.value || staticMode || renderError.value) return
    paintFrame(time)
  }

  // reduced-motion 或 paused：不进 rAF 循环，只画一帧代表相位上的静态画面
  const renderStaticFrame = () => {
    staticMode = true
    detachLoop()
    paintFrame(STATIC_PHASE)
    isRunning.value = false
  }

  function createDummyProjector() {
    return (x: number, y: number, _z: number) => [x, y, 0] as [number, number, number]
  }

  const attachLoop = () => {
    if (unsubscribe || !visible) return
    unsubscribe = subscribeToClock(_drawFrame)
    isRunning.value = true
  }

  const detachLoop = () => {
    unsubscribe?.()
    unsubscribe = null
  }

  onMounted(() => {
    const canvas = canvasRef.value
    if (!canvas) {
      renderError.value = true
      return
    }

    // 离屏检测 + 标签页可见性：隐藏时摘掉时钟订阅（全局共享时钟因此少一个
    // 回调，多个 orb 全部离屏时时钟会自行休眠），恢复时立即重绘避免残帧。
    const syncLoop = () => {
      if (staticMode || renderError.value) return
      if (visible && !documentHidden()) attachLoop()
      else detachLoop()
      isRunning.value = visible && !documentHidden()
    }

    if (typeof IntersectionObserver !== 'undefined') {
      io = new IntersectionObserver(([entry]) => {
        visible = entry.isIntersecting
        syncLoop()
        // 回到视口时立即补一帧，避免等待下一次时钟 tick 出现空白。
        // 静态模式（reduced-motion）下不重绘，保持定格帧。
        if (visible && !staticMode && !renderError.value) {
          paintFrame(performance.now() / 1000)
        }
      })
      io.observe(canvas)
    }

    const onVis = () => syncLoop()
    document.addEventListener('visibilitychange', onVis)

    if (reduced.value) {
      renderStaticFrame()
    } else {
      attachLoop()
      paintFrame(performance.now() / 1000)
    }

    // 运行中切换 prefers-reduced-motion（系统设置改变）：即时生效
    watch(reduced, (isReduced) => {
      if (isReduced) {
        renderStaticFrame()
      } else {
        staticMode = false
        consecutiveModeErrors = 0
        syncLoop()
        paintFrame(performance.now() / 1000)
      }
    })

    // 静态模式下 state/size 变化需要重绘对应帧
    watch([state, size, dark], () => {
      if (staticMode) paintFrame(STATIC_PHASE)
    })

    onUnmounted(() => {
      detachLoop()
      io?.disconnect()
      document.removeEventListener('visibilitychange', onVis)
      isRunning.value = false
    })
  })

  return {
    isRunning,
    currentFps,
    renderError,
    pause: () => { paused.value = true },
    resume: () => { paused.value = false },
  }
}

function documentHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden'
}
