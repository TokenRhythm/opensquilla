import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import i18n from './i18n'
import { useAppStore } from './stores/app'
import { useRpcStore } from './stores/rpc'
import { createGatewayAdapters } from './adapters/gateway/gatewayAdapters'
import { createPrivateHttpTransport } from './adapters/gateway/privateHttpTransport'
import { SESSION_DIRECTORY_KEY } from './modules/sessionDirectory'
import { SESSION_DIRECTORY_CHANGES_KEY } from './modules/sessionDirectoryChanges'
import { SESSION_LIFECYCLE_KEY } from './modules/sessionLifecycle'
import { SESSION_ROUTING_KEY } from './modules/sessionRouting'
import { TURN_COMMANDS_KEY } from './modules/turnCommands'
import { PENDING_INPUT_QUEUE_KEY } from './modules/pendingInputQueue'
import { APPROVAL_CENTER_KEY } from './modules/approvalCenter'
import 'katex/dist/katex.min.css'
import './assets/base.css'
import './themes/tokens' // eagerly bundles every value theme's token block
import './styles/control-visual-system.css'
import './styles/route-fx.css'
import './styles/chat-markdown.css'
import './styles/chat-shared.css'
import './styles/apple-modern.css'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)

const appStore = useAppStore()
appStore.initTheme()

const rpcStore = useRpcStore()
rpcStore.init()
const gatewayAdapters = createGatewayAdapters(rpcStore, {
  http: createPrivateHttpTransport(),
})
app.provide(
  SESSION_DIRECTORY_KEY,
  gatewayAdapters.sessionDirectory,
)
app.provide(
  SESSION_DIRECTORY_CHANGES_KEY,
  gatewayAdapters.sessionDirectoryChanges,
)
app.provide(
  SESSION_LIFECYCLE_KEY,
  gatewayAdapters.sessionLifecycle,
)
app.provide(SESSION_ROUTING_KEY, gatewayAdapters.sessionRouting)
app.provide(TURN_COMMANDS_KEY, gatewayAdapters.turnCommands)
app.provide(PENDING_INPUT_QUEUE_KEY, gatewayAdapters.pendingInputQueue)
app.provide(APPROVAL_CENTER_KEY, gatewayAdapters.approvalCenter)
router.afterEach(() => {
  rpcStore.applyLinkTokenFromUrl()
})

// Resolve + load the active locale before mounting so the first paint is
// already in the right language (no English flash). initLocale never rejects
// (it falls back to en internally); finally() guarantees the app still mounts.
appStore.initLocale().finally(() => {
  app.mount('#app')
})
