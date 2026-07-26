import { computed } from 'vue'
import { getNavigationItems, getWorkNavigationSection } from '@/router/nav'

export function useNavigation() {
  const bottomRoutes = computed(() => getNavigationItems('bottom'))
  // Flat primary rows, single-sourced from route metadata so the desktop rail,
  // mobile drawer, and command palette stay in the same order.
  const workNav = computed(() => getWorkNavigationSection())

  return {
    bottomRoutes,
    workNav,
  }
}
