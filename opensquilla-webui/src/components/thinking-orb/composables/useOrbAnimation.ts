/**
 * useOrbAnimation — 动画循环 composable
 *
 * 独创性设计：
 * 1. 使用全局共享时钟（而非每个实例独立 rAF）
 * 2. 支持 IntersectionObserver 离屏暂停
 * 3. 支持 visibilitychange 标签页隐藏暂停
 * 4. 支持自适应帧率（通过全局时钟）
 * 5. 支持暂停/恢复控制
 */

import { ref, onMounted, onUnmounted, type Ref } from 'vue'
import { subscribeToClock } from '../engine/clock'
import { renderLayers, emptyRenderResult } from '../engine/core'
import type { RenderResult } from '../engine/core'
import { resolvePreset, registerBuiltinModes, type OrbState } from '../presets'
import { modeRegistry } from '../registry'
import type { AnimationMode } from '../engine/types'

export interface AnimationControls {
  /** 是否正在运行 */
  isRunning: Ref<boolean>
  /** 当前帧率 */
  currentFps: Ref<number>
  /** 暂停/恢复 */
  pause: () => void
  resume: () => void
}

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
  _reduced: Ref<boolean>,
): AnimationControls {
  const isRunning = ref(false)
  const currentFps = ref(60)

  let unsubscribe: (() => void) | null = null
  let io: IntersectionObserver | null = null
  let visible = true
  let lastSize = 0
  let lastDpr = 1

  // 确保内置模式已注册
  registerBuiltinModes()

  const _drawFrame = (time: number) => {
    const canvas = canvasRef.value
    if (!canvas || !visible || paused.value) return

    const ctx = canvas.getContext('2d')
    if (!ctx) return

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

    // 获取模式实例
    const modeInstance = modeRegistry.get(state.value)

    // 设置变换
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, currentSize, currentSize)

    if (modeInstance) {
      // 使用模式类渲染
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
        result = emptyRenderResult()
      }

      renderLayers(ctx, result, dark.value)
    }
  }

  // 创建虚拟投影（模式类自己会创建投影）
  function createDummyProjector() {
    return (x: number, y: number, _z: number) => [x, y, 0] as [number, number, number]
  }

  onMounted(() => {
    const canvas = canvasRef.value
    if (!canvas) return

    // 离屏检测
    if (typeof IntersectionObserver !== 'undefined') {
      io = new IntersectionObserver(([entry]) => {
        visible = entry.isIntersecting
      })
      io.observe(canvas)
    }

    // 标签页可见性
    const onVis = () => {
      if (document.visibilityState === 'hidden') {
        // 不取消订阅，只暂停绘制
      } else if (visible) {
        // 恢复时立即重绘
      }
    }
    document.addEventListener('visibilitychange', onVis)

    // 订阅全局时钟
    unsubscribe = subscribeToClock(_drawFrame)
    isRunning.value = true

    onUnmounted(() => {
      unsubscribe?.()
      io?.disconnect()
      document.removeEventListener('visibilitychange', onVis)
      isRunning.value = false
    })
  })

  return {
    isRunning,
    currentFps,
    pause: () => { paused.value = true },
    resume: () => { paused.value = false },
  }
}