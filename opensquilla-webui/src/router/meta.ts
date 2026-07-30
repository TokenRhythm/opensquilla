import 'vue-router'

/** Stable route taxonomy used by navigation and settings surfaces. */
export type NavGroup = 'Work' | 'Operate' | 'Observe' | 'Settings'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    group?: NavGroup
    icon?: import('@/utils/icons').IconName
    nav?: 'primary' | 'bottom'
    navOrder?: number
    /** Optional i18n key for navigation surfaces when the rail label differs
     *  from the route's page/document title. */
    navLabelKey?: string
    /** Optional i18n key for the page/document title when it differs from the
     *  route-name token used by navigation surfaces. */
    titleKey?: string
    platforms?: Array<'web' | 'desktop'>
    /** Stable route-view identity for sibling routes that must not remount when
     *  only route params change the internal view state. */
    viewKey?: string
    /** App-level route transition override. Use for route-mounted overlays that
     *  already own their own enter/leave motion. */
    routeTransition?: 'none'
    /** Keep this view mounted across visits so stateful or poll-heavy views do
     *  not restart on every navigation. Chat is excluded (it re-inits per
     *  session). */
    keepAlive?: boolean
    /** Axis-B expressive skin (a registered `kind:'expressive'` theme id) to
     *  apply to this route's content area only. Composes over the active
     *  light/dark ground; never applies to the console shell. Reserved for
     *  narrative surfaces (changelog, design pages) — never operational views. */
    skin?: string
  }
}
