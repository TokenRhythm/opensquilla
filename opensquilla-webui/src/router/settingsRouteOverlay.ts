import { computed, defineComponent, provide, reactive, shallowRef, watch, type PropType } from 'vue'
import { routeLocationKey, type RouteLocationNormalizedLoaded, type Router } from 'vue-router'

const isSettings = (route: RouteLocationNormalizedLoaded) => (
  route.name === 'settings' || route.name === 'settings-section'
)
const isChat = (route: RouteLocationNormalizedLoaded) => (
  route.path === '/chat' || route.path === '/chat/new'
)

/** Retain the live composer only while Settings covers the chat it came from. */
export function useSettingsRouteOverlay(router: Router) {
  const backgroundRoute = shallowRef<RouteLocationNormalizedLoaded | null>(null)
  watch(router.currentRoute, (next, previous) => {
    if (!isSettings(next)) backgroundRoute.value = null
    else if (!backgroundRoute.value && isChat(previous)) backgroundRoute.value = previous
  }, { flush: 'sync' })
  const contentRoute = computed(() => backgroundRoute.value ?? router.currentRoute.value)
  return { backgroundRoute, contentRoute }
}

// RouterView's route prop controls component matching, but useRoute otherwise
// still reads the global Settings URL. Scope both together so chat's route
// watchers cannot reset its project, session, attachments, or model selection.
export const SettingsBackgroundRoute = defineComponent({
  name: 'SettingsBackgroundRoute',
  props: { route: { type: Object as PropType<RouteLocationNormalizedLoaded>, required: true } },
  setup(props, { slots }) {
    const scopedRoute = {} as RouteLocationNormalizedLoaded
    for (const key of Object.keys(props.route)) {
      Object.defineProperty(scopedRoute, key, {
        enumerable: true,
        get: () => props.route[key as keyof RouteLocationNormalizedLoaded],
      })
    }
    provide(routeLocationKey, reactive(scopedRoute))
    return () => slots.default?.()
  },
})
