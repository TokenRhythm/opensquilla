import { ref, type Ref } from 'vue'
import { usePlatform } from '@/platform'
import type { CliInvocation } from '@/platform'

// Matches the leading `opensquilla` CLI token only — `opensquilla gateway …`
// rewrites; `export FOO=…` and other shell lines pass through untouched.
const CLI_TOKEN = /^opensquilla(?=\s|$)/

// Gateway process-lifecycle commands. On the desktop shell these cannot work
// from a copied command — the shell supervises its own gateway child (pid lock
// + respawn), so a CLI restart/start/stop refuses, times out, or races the
// shell. They are surfaced as guidance (use the app's restart) instead.
const GATEWAY_LIFECYCLE = /^opensquilla\s+gateway\s+(?:restart|start|stop)(?:\s|$)/

// localStorage key the rpc store persists the active gateway URL under. Kept in
// sync with stores/rpc.ts WS_URL_KEY.
const WS_URL_KEY = 'opensquilla.wsUrl'

export function isGatewayLifecycleCommand(command: string): boolean {
  return GATEWAY_LIFECYCLE.test(command)
}

// The bundled-CLI prefix targets the desktop's OWN gateway (its config/state
// roots). It is only valid when the UI is connected to that owned gateway — the
// one that served this page. A desktop operator who repoints the connection at
// a remote gateway must see the raw command, not one rewritten for the local
// bundle.
export function isOwnedGatewayConnection(): boolean {
  if (typeof window === 'undefined') return true
  try {
    const raw = window.localStorage.getItem(WS_URL_KEY)
    if (!raw) return true
    return new URL(raw).host === window.location.host
  } catch {
    // An invalid persisted URL cannot prove ownership. Fail closed so Desktop
    // neither rewrites commands nor opens local Runtime controls for an
    // ambiguous connection.
    return false
  }
}

const invocation: Ref<CliInvocation | null> = ref(null)
let loaded: Promise<void> | null = null

function ensureLoaded(): Promise<void> {
  if (!loaded) {
    loaded = (async () => {
      const platform = usePlatform()
      // Web installs are CLI-launched: the bare `opensquilla` token already
      // resolves in the operator's shell, so no rewrite is needed there.
      if (platform.capabilities.hasTerminalWorkflow) return
      invocation.value = (await platform.gateway.getCliInvocation?.()) ?? null
    })().catch(() => {
      invocation.value = null
    })
  }
  return loaded
}

/**
 * Rewrites copyable `opensquilla …` commands into invocations that actually
 * run on this machine. On desktop the CLI ships inside the app bundle (off
 * PATH) and the gateway reads its config/state roots from environment
 * variables, so the shell reports a paste-ready prefix carrying both. Any
 * failure to obtain the prefix degrades to the identity function.
 */
export function useCliInvocation() {
  void ensureLoaded()

  function format(command: string): string {
    const prefix = invocation.value?.prefix
    if (!prefix || !CLI_TOKEN.test(command) || !isOwnedGatewayConnection()) return command
    // Function replacement keeps the prefix literal: paths inside it may
    // contain $-sequences that String.replace would otherwise expand.
    return command.replace(CLI_TOKEN, () => prefix)
  }

  return { format, invocation }
}

// Test-only: drop the module-level cache so specs can exercise fresh loads.
export function resetCliInvocationForTest() {
  invocation.value = null
  loaded = null
}
