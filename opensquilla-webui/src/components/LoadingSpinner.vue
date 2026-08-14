<template>
  <ThinkingOrb
    v-if="!useFallback"
    :state="state"
    :size="size"
    theme="auto"
    :speed="speed"
    :aria-label="ariaLabel"
  />
  <span
    v-else
    class="loading-spinner"
    :aria-label="ariaLabel"
    role="status"
  />
</template>

<script setup lang="ts">
/**
 * LoadingSpinner.vue — 向后兼容的加载指示器
 *
 * 使用 ThinkingOrb 动画组件替代了原来的纯 CSS spinner。
 * 保持完全相同的接口 (LoadingSpinner import)，无需修改任何引用代码。
 *
 * 如果 ThinkingOrb 组件加载失败，会自动降级为原始 CSS spinner。
 */
import { computed, ref, onErrorCaptured } from 'vue'
import { useI18n } from 'vue-i18n'
import ThinkingOrb from './thinking-orb/ThinkingOrb.vue'

const props = withDefaults(defineProps<{
  /** 动画状态：默认为 working，可传入其他 8 种状态 */
  state?: 'working' | 'searching' | 'solving' | 'listening' | 'connecting' | 'weaving' | 'composing' | 'breathing' | 'shaping'
  /** 尺寸（CSS px），默认 24 匹配原 spinner */
  size?: number
  /** 速度倍率 */
  speed?: number
}>(), {
  state: 'working',
  size: 28,
  speed: 1,
})

const { t } = useI18n()
const useFallback = ref(false)

const ariaLabel = computed(() => {
  return t('shared.loading')
})

// 如果 ThinkingOrb 出错，降级到 CSS spinner
onErrorCaptured(() => {
  useFallback.value = true
  return false
})
</script>

<style scoped>
.loading-spinner {
  display: inline-block;
  width: 24px;
  height: 24px;
  border: 2px solid var(--border-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .loading-spinner {
    animation-duration: var(--dur-pulse);
  }
}
</style>