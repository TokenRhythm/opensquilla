<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from './Icon.vue'
import { getPlatform } from '@/platform'

// Passive "a newer version is available" notice. The gateway injects the update
// info into #opensquilla-data (data-update) only when a newer published release
// exists; here we render an unobtrusive, dismissible card for it. No download
// or install happens from the web — the link points at the release page.
//
// Suppressed wherever the desktop shell manages update discovery and actions.
// That includes native electron-updater on macOS and the versioned manual
// installer flow on unsigned Windows. Browser surfaces keep this passive banner;
// older Windows shells without the managed capability retain their legacy banner.

const { t } = useI18n()
const platform = getPlatform()
const isDesktop = platform.id === 'desktop'

const DISMISS_KEY = 'opensquilla-update-dismissed'
const RELEASES_FALLBACK = 'https://github.com/opensquilla/opensquilla/releases'
const UPDATE_STATUS_URL = '/api/system/update'
const POLL_INTERVAL_MS = 15 * 60 * 1000
const REQUEST_TIMEOUT_MS = 5 * 1000

interface UpdateInfo {
  current?: string
  latest?: string
  available?: boolean
  url?: string
}

interface UpdateStatusPayload {
  current: string
  latest: string | null
  available: boolean
  url: string | null
  checkedAt: string | null
}

function readUpdate(): UpdateInfo | null {
  try {
    const raw = document.getElementById('opensquilla-data')?.dataset.update
    if (!raw) return null
    const parsed = JSON.parse(raw) as UpdateInfo | null
    if (parsed && parsed.available === true && typeof parsed.latest === 'string' && parsed.latest) {
      return parsed
    }
    return null
  } catch {
    return null
  }
}

function normalizeUpdateStatus(payload: unknown): UpdateInfo | null | undefined {
  if (!payload || typeof payload !== 'object') return undefined
  const raw = payload as Partial<UpdateStatusPayload>
  if (
    typeof raw.current !== 'string'
    || typeof raw.available !== 'boolean'
    || (raw.latest !== null && typeof raw.latest !== 'string')
    || (raw.url !== null && typeof raw.url !== 'string')
    || (raw.checkedAt !== null && typeof raw.checkedAt !== 'string')
  ) {
    return undefined
  }
  if (!raw.available) return null
  if (typeof raw.latest !== 'string' || !raw.latest.trim()) return undefined
  return {
    current: raw.current,
    latest: raw.latest,
    available: true,
    url: typeof raw.url === 'string' && raw.url ? raw.url : undefined,
  }
}

const info = ref<UpdateInfo | null>(readUpdate())

// Assume managed on desktop until the shell confirms otherwise, preventing a
// native/manual desktop notice from flashing beside the passive banner.
const managedUpdate = ref(isDesktop)
let mounted = false
let pollingEnabled = false
let pollTimer: number | null = null
let activeController: AbortController | null = null
let activeRequestTimeout: number | null = null
let inFlight: Promise<void> | null = null

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  try {
    const token = sessionStorage.getItem('opensquilla.wsToken') || ''
    if (token) headers.Authorization = `Bearer ${token}`
  } catch {
    // sessionStorage unavailable (private mode) — let gateway auth decide.
  }
  return headers
}

async function refreshUpdateInfo(): Promise<void> {
  if (inFlight) return inFlight

  const controller = new AbortController()
  activeController = controller
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  activeRequestTimeout = timeout

  const request = (async () => {
    try {
      const response = await fetch(UPDATE_STATUS_URL, {
        cache: 'no-store',
        headers: authHeaders(),
        signal: controller.signal,
      })
      if (!response.ok) return

      const next = normalizeUpdateStatus(await response.json())
      // undefined means an invalid response: preserve the last known status.
      if (mounted && next !== undefined) info.value = next
    } catch {
      // A transient gateway/network/parse failure must not erase a known update.
    } finally {
      window.clearTimeout(timeout)
      if (activeRequestTimeout === timeout) activeRequestTimeout = null
      if (activeController === controller) activeController = null
    }
  })()

  inFlight = request
  try {
    await request
  } finally {
    if (inFlight === request) inFlight = null
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function startVisiblePolling(): void {
  stopPolling()
  if (!pollingEnabled || document.visibilityState !== 'visible') return
  void refreshUpdateInfo()
  pollTimer = window.setInterval(() => {
    void refreshUpdateInfo()
  }, POLL_INTERVAL_MS)
}

function onVisibilityChange(): void {
  if (document.visibilityState === 'visible') startVisiblePolling()
  else stopPolling()
}

onMounted(async () => {
  mounted = true
  let enabled: boolean
  try {
    enabled = await platform.desktopUpdateManaged()
  } catch {
    // Capability failures are conservative: keep the bootstrap behavior, but
    // do not start a second update channel that might duplicate native UI.
    return
  }
  if (!mounted) return

  managedUpdate.value = enabled
  if (enabled) return

  pollingEnabled = true
  document.addEventListener('visibilitychange', onVisibilityChange)
  startVisiblePolling()
})

onBeforeUnmount(() => {
  mounted = false
  pollingEnabled = false
  document.removeEventListener('visibilitychange', onVisibilityChange)
  stopPolling()
  if (activeRequestTimeout !== null) {
    window.clearTimeout(activeRequestTimeout)
    activeRequestTimeout = null
  }
  activeController?.abort()
  activeController = null
})

function readDismissed(): string | null {
  try {
    return localStorage.getItem(DISMISS_KEY)
  } catch {
    return null
  }
}

// Dismissal is keyed to the version, so a future release re-arms the notice.
const dismissedVersion = ref<string | null>(readDismissed())
const visible = computed(
  () => !!info.value && !managedUpdate.value && dismissedVersion.value !== info.value.latest,
)
const releaseUrl = computed(() => info.value?.url || RELEASES_FALLBACK)

function dismiss() {
  const latest = info.value?.latest ?? null
  dismissedVersion.value = latest
  try {
    if (latest) localStorage.setItem(DISMISS_KEY, latest)
  } catch {
    // localStorage unavailable (private mode) — dismissal is just session-local.
  }
}
</script>

<template>
  <div
    v-if="visible && info"
    class="update-banner"
    role="status"
    aria-live="polite"
    data-testid="update-banner"
  >
    <Icon class="update-banner__icon" name="download" :size="16" aria-hidden="true" />
    <div class="update-banner__body">
      <p class="update-banner__title">{{ t('updates.available', { version: info.latest }) }}</p>
      <a
        class="update-banner__link"
        :href="releaseUrl"
        target="_blank"
        rel="noopener noreferrer"
      >{{ t('updates.viewRelease') }}</a>
    </div>
    <button
      type="button"
      class="update-banner__dismiss"
      :title="t('updates.dismiss')"
      :aria-label="t('updates.dismiss')"
      @click="dismiss"
    >
      <Icon name="x" :size="14" aria-hidden="true" />
    </button>
  </div>
</template>

<style scoped>
.update-banner {
  position: fixed;
  right: var(--sp-4);
  bottom: var(--sp-4);
  z-index: 950;
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  max-width: 340px;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border-strong));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-elevated));
  color: var(--text);
  box-shadow: var(--shadow-md);
  animation: update-banner-in var(--dur-base) var(--ease-out);
}

.update-banner__icon {
  flex-shrink: 0;
  margin-top: 1px;
  color: var(--accent);
}

.update-banner__body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.update-banner__title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  overflow-wrap: anywhere;
}

.update-banner__link {
  align-self: flex-start;
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--accent);
  text-decoration: none;
}

.update-banner__link:hover {
  text-decoration: underline;
}

.update-banner__link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--radius-sm);
}

.update-banner__dismiss {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: var(--sp-1);
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition);
}

.update-banner__dismiss:hover {
  color: var(--text);
  background: var(--bg-hover);
}

.update-banner__dismiss:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

@keyframes update-banner-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .update-banner {
    animation: none;
  }
}

@media (max-width: 768px) {
  .update-banner {
    left: var(--sp-4);
    max-width: none;
  }
}
</style>
