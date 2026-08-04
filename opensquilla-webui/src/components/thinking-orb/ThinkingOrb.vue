<template>
  <canvas
    ref="canvasRef"
    :style="canvasStyle"
    role="img"
    :aria-label="ariaLabel"
  />
</template>

<script setup lang="ts">
/**
 * ThinkingOrb.vue — 点阵思维球体动画组件
 *
 * 专为 AI Agent UI 设计的 9 种状态动画指示器。
 * 纯 Canvas 2D 渲染，无 WebGL，无需外部依赖。
 *
 * 独创性（与原版 thinking-orbs 的区别）：
 * 1. Vue 3 组合式 API：用 composable 替代 React hooks
 * 2. 类 + 插件注册系统：而非函数式 ModeDraw
 * 3. 多层渲染管线：背景/主层/高亮层独立合成
 * 4. 全局共享时钟：所有实例同步，自适应帧率
 * 5. 可扩展形状注册：MorphMode 支持自定义形状
 * 6. 任意尺寸：不限于 64/20，支持响应式
 */

import { ref, computed, watch } from 'vue'
import { useOrbTheme, useReducedMotion, type OrbTheme } from './composables/useOrbTheme'
import { useOrbAnimation } from './composables/useOrbAnimation'
import { STATE_LABELS, type OrbState } from './presets'

const props = withDefaults(defineProps<{
  /** 动画状态 */
  state?: OrbState
  /** 尺寸（CSS px），默认 64，支持任意值 */
  size?: number
  /** 主题模式 */
  theme?: OrbTheme
  /** 速度倍率 */
  speed?: number
  /** 暂停动画 */
  paused?: boolean
  /** 自定义 aria-label */
  'aria-label'?: string
}>(), {
  state: 'working',
  size: 64,
  theme: 'auto',
  speed: 1,
  paused: false,
})

const canvasRef = ref<HTMLCanvasElement | null>(null)
const stateRef = ref(props.state)
const sizeRef = ref(props.size)
const speedRef = ref(props.speed)
const pausedRef = ref(props.paused)
const themeRef = ref(props.theme)

const isDark = useOrbTheme(themeRef, canvasRef)
const reduced = useReducedMotion()

const { isRunning } = useOrbAnimation(
  canvasRef,
  stateRef,
  sizeRef,
  isDark,
  speedRef,
  pausedRef,
  reduced,
)

// 监听 props 变化
watch(() => props.state, (v) => { stateRef.value = v })
watch(() => props.size, (v) => { sizeRef.value = v })
watch(() => props.speed, (v) => { speedRef.value = v })
watch(() => props.paused, (v) => { pausedRef.value = v })
watch(() => props.theme, (v) => { themeRef.value = v })

const canvasStyle = computed(() => ({
  width: `${props.size}px`,
  height: `${props.size}px`,
  display: 'block',
}))

const ariaLabel = computed(() => {
  return props['aria-label'] ?? STATE_LABELS[props.state] ?? '工作中…'
})
</script>