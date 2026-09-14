/**
 * useOrbTheme — 独创性主题检测 composable
 *
 * 原版 thinking-orbs 用 React hooks (useState + useEffect + MutationObserver)
 * 本设计用 Vue 3 composable：
 * - 使用 Vue 3 响应式系统（ref/computed）
 * - 支持 data-theme / class / prefers-color-scheme 三层检测
 * - 自动监听变化
 * - 支持 SSR 安全
 */

import { ref, onMounted, onUnmounted, watch, type Ref } from 'vue'

export type OrbTheme = 'auto' | 'dark' | 'light'

function ancestorTheme(el: Element | null): boolean | null {
  let node: Element | null = el
  while (node) {
    const attr = node.getAttribute('data-theme')
    if (attr === 'dark') return true
    if (attr === 'light') return false
    if (node.classList.contains('dark')) return true
    if (node.classList.contains('light')) return false
    node = node.parentElement
  }
  return null
}

function systemDark(): boolean {
  if (typeof matchMedia === 'undefined') return false
  return matchMedia('(prefers-color-scheme: dark)').matches
}

/**
 * 解析暗色/亮色主题
 * @param theme 主题模式
 * @param hostRef 宿主元素的 ref
 * @returns 是否为暗色主题
 */
export function useOrbTheme(theme: Ref<OrbTheme>, hostRef: Ref<HTMLElement | null>): Ref<boolean> {
  const isDark = ref(true)

  const resolve = () => {
    if (theme.value === 'dark') {
      isDark.value = true
      return
    }
    if (theme.value === 'light') {
      isDark.value = false
      return
    }
    const fromTree = ancestorTheme(hostRef.value)
    isDark.value = fromTree ?? systemDark()
  }

  // 监听主题变化
  let mq: MediaQueryList | null = null
  let mo: MutationObserver | null = null

  onMounted(() => {
    resolve()

    // OS 主题切换
    if (typeof matchMedia !== 'undefined') {
      mq = matchMedia('(prefers-color-scheme: dark)')
      mq.addEventListener('change', resolve)
    }

    // DOM 属性变化监听
    if (typeof MutationObserver !== 'undefined') {
      mo = new MutationObserver(resolve)
      mo.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ['class', 'data-theme'],
        subtree: true,
      })
    }
  })

  onUnmounted(() => {
    mq?.removeEventListener('change', resolve)
    mo?.disconnect()
  })

  // 当 theme prop 变化时重新解析
  watch(theme, resolve)

  return isDark
}

/**
 * 是否启用减少动画模式
 */
export function useReducedMotion(): Ref<boolean> {
  const reduced = ref(false)

  onMounted(() => {
    if (typeof matchMedia === 'undefined') return
    const mq = matchMedia('(prefers-reduced-motion: reduce)')
    reduced.value = mq.matches
    const handler = (e: MediaQueryListEvent) => { reduced.value = e.matches }
    mq.addEventListener('change', handler)
    onUnmounted(() => mq.removeEventListener('change', handler))
  })

  return reduced
}