<template>
  <div ref="wrapRef" class="support-diagnostics theme-menu-wrap">
    <button
      ref="triggerRef"
      type="button"
      class="btn btn--ghost support-diagnostics__trigger"
      :class="{ 'is-open': menuOpen }"
      aria-haspopup="menu"
      :aria-expanded="menuOpen"
      :title="t('monitorSupport.title')"
      data-testid="support-diagnostics-trigger"
      @click.stop="toggleMenu"
      @keydown.down.prevent="openMenu('first')"
      @keydown.up.prevent="openMenu('last')"
    >
      <Icon name="gauge" :size="16" />
      <span>{{ t('monitorSupport.title') }}</span>
      <Icon name="chevronDown" :size="13" />
    </button>

    <div
      v-if="menuOpen"
      ref="menuRef"
      class="theme-menu support-diagnostics__menu"
      role="menu"
      :aria-label="t('monitorSupport.menuLabel')"
      @keydown="onMenuKeydown"
    >
      <template v-if="route.path !== '/logs'">
        <button
          type="button"
          class="theme-menu__item support-diagnostics__item"
          role="menuitem"
          data-testid="support-view-logs"
          @click="openLogs"
        >
          <span class="support-diagnostics__item-icon"><Icon name="logs" :size="16" /></span>
          <span class="support-diagnostics__item-copy">
            <strong>{{ t('monitorSupport.viewLogs') }}</strong>
            <small>{{ t('monitorSupport.viewLogsDescription') }}</small>
          </span>
        </button>
        <div class="support-diagnostics__divider" role="separator"></div>
      </template>
      <button
        type="button"
        class="theme-menu__item support-diagnostics__item"
        role="menuitem"
        :disabled="copyInFlight"
        data-testid="support-copy-readiness"
        @click="copyReadinessReport"
      >
        <span class="support-diagnostics__item-icon"><Icon name="copy" :size="16" /></span>
        <span class="support-diagnostics__item-copy">
          <strong>{{ t('monitorSupport.copyReport') }}</strong>
          <small>{{ t('monitorSupport.copyReportDescription') }}</small>
        </span>
      </button>
      <div class="support-diagnostics__divider" role="separator"></div>
      <button
        type="button"
        class="theme-menu__item support-diagnostics__item"
        role="menuitem"
        data-testid="support-download-bundle"
        @click="openBundleDialog"
      >
        <span class="support-diagnostics__item-icon"><Icon name="download" :size="16" /></span>
        <span class="support-diagnostics__item-copy">
          <strong>{{ t('monitorSupport.downloadBundle') }}</strong>
          <small>{{ t('monitorSupport.downloadBundleDescription') }}</small>
        </span>
      </button>
      <p class="support-diagnostics__privacy">
        <Icon name="shield" :size="13" />
        <span>{{ t('monitorSupport.privacySummary') }}</span>
      </p>
    </div>

    <DiagnosticsBundleDialog
      :open="bundleDialogOpen"
      :busy="bundleInFlight"
      @close="closeBundleDialog"
      @confirm="downloadBundle"
    />
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import DiagnosticsBundleDialog from '@/components/DiagnosticsBundleDialog.vue'
import Icon from '@/components/Icon.vue'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { useToasts } from '@/composables/useToasts'
import { useRpcStore } from '@/stores/rpc'
import {
  copyTextWithFallback,
  downloadBlob,
  filenameFromContentDisposition,
} from '@/utils/browser'
import { normalizeHomePaths } from '@/utils/overviewDiagnostics'

const SUPPORT_BUNDLE_DAYS = 1

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const rpc = useRpcStore()
const { pushToast } = useToasts()
const menuOpen = ref(false)
const bundleDialogOpen = ref(false)
const copyInFlight = ref(false)
const bundleInFlight = ref(false)
const wrapRef = ref<HTMLDivElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const menuRef = ref<HTMLDivElement | null>(null)

function gatewayContextUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}

function closeMenuAndRestoreFocus() {
  menuOpen.value = false
  void nextTick(() => triggerRef.value?.focus())
}

function menuItems(): HTMLButtonElement[] {
  return Array.from(
    menuRef.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not([disabled])') ?? [],
  )
}

function focusMenuItem(position: 'first' | 'last') {
  const items = menuItems()
  const target = position === 'last' ? items[items.length - 1] : items[0]
  target?.focus()
}

function openMenu(position: 'first' | 'last' = 'first') {
  menuOpen.value = true
  void nextTick(() => focusMenuItem(position))
}

function toggleMenu() {
  if (menuOpen.value) {
    closeMenuAndRestoreFocus()
    return
  }
  openMenu()
}

function openLogs() {
  menuOpen.value = false
  void router.push('/logs')
}

function onMenuKeydown(event: KeyboardEvent) {
  const items = menuItems()
  if (!items.length) return
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'Escape') {
    event.preventDefault()
    closeMenuAndRestoreFocus()
    return
  }
  if (event.key === 'Tab') {
    menuOpen.value = false
    return
  }
  let nextIndex: number | null = null
  if (event.key === 'ArrowDown') nextIndex = current < 0 ? 0 : (current + 1) % items.length
  if (event.key === 'ArrowUp') nextIndex = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
  if (event.key === 'Home') nextIndex = 0
  if (event.key === 'End') nextIndex = items.length - 1
  if (nextIndex == null) return
  event.preventDefault()
  items[nextIndex]?.focus()
}

function openBundleDialog() {
  menuOpen.value = false
  bundleDialogOpen.value = true
}

function closeBundleDialog() {
  bundleDialogOpen.value = false
  void nextTick(() => triggerRef.value?.focus())
}

async function copyReadinessReport() {
  if (copyInFlight.value) return
  copyInFlight.value = true
  menuOpen.value = false
  await nextTick()
  triggerRef.value?.focus()
  try {
    await rpc.waitForConnection()
    const data = await rpc.call<Record<string, unknown>>('doctor.status', {
      agentId: 'main',
      deep: true,
    })
    const report = {
      ...(data || {}),
      gatewayUrl: data?.gatewayUrl || gatewayContextUrl(),
      copiedAt: new Date().toISOString(),
    }
    await copyTextWithFallback(normalizeHomePaths(JSON.stringify(report, null, 2)))
    pushToast(t('monitorSupport.copySuccess'), { tone: 'ok' })
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    pushToast(t('monitorSupport.copyFailed', { error: detail }), { tone: 'danger' })
  } finally {
    copyInFlight.value = false
    void nextTick(() => triggerRef.value?.focus())
  }
}

async function downloadBundle(options: { includeContent: boolean }) {
  if (bundleInFlight.value) return
  closeBundleDialog()
  bundleInFlight.value = true
  try {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    // Match the owner-authenticated diagnostics route used by the prior Logs
    // action. Some hardened/embedded contexts reject sessionStorage access.
    let token = ''
    try { token = sessionStorage.getItem('opensquilla.wsToken') || '' } catch {}
    if (token) headers.Authorization = `Bearer ${token}`
    const response = await fetch('/api/v1/diagnostics/bundle', {
      method: 'POST',
      headers,
      credentials: 'same-origin',
      body: JSON.stringify({
        include_content: options.includeContent,
        days: SUPPORT_BUNDLE_DAYS,
      }),
    })
    if (!response.ok) {
      pushToast(t('monitorSupport.bundleFailed'), { tone: 'danger' })
      return
    }
    const blob = await response.blob()
    const filename = filenameFromContentDisposition(response.headers.get('content-disposition'))
      || 'opensquilla-bundle.zip'
    downloadBlob(blob, filename)
    pushToast(t('monitorSupport.bundleReady'), { tone: 'ok' })
  } catch {
    pushToast(t('monitorSupport.bundleFailed'), { tone: 'danger' })
  } finally {
    bundleInFlight.value = false
  }
}

useDocumentEvent('click', (event) => {
  if (!menuOpen.value) return
  if (event.target instanceof Node && !wrapRef.value?.contains(event.target)) {
    menuOpen.value = false
  }
})

useDocumentEvent('keydown', (event) => {
  if (event.key === 'Escape' && menuOpen.value) {
    event.preventDefault()
    closeMenuAndRestoreFocus()
  }
})

watch(() => route.path, () => {
  menuOpen.value = false
  bundleDialogOpen.value = false
})
</script>

<style scoped>
.support-diagnostics {
  flex: 0 0 auto;
}

.support-diagnostics__trigger {
  align-items: center;
  display: inline-flex;
  gap: var(--sp-2);
  white-space: nowrap;
}

.support-diagnostics__trigger.is-open {
  border-color: var(--border-focus);
  color: var(--text);
}

.support-diagnostics__menu {
  max-width: calc(100vw - (2 * var(--sp-3)));
  min-width: min(360px, calc(100vw - (2 * var(--sp-3))));
  padding: var(--sp-2);
}

.support-diagnostics__item {
  align-items: flex-start;
  gap: var(--sp-3);
  padding: 10px;
}

.support-diagnostics__item:disabled {
  cursor: progress;
  opacity: 0.65;
}

.support-diagnostics__item-icon {
  align-items: center;
  background: var(--bg-surface-2);
  border-radius: var(--radius-sm);
  color: var(--accent);
  display: inline-flex;
  flex: 0 0 30px;
  height: 30px;
  justify-content: center;
}

.support-diagnostics__item-copy {
  display: block;
  min-width: 0;
}

.support-diagnostics__item-copy strong,
.support-diagnostics__item-copy small {
  display: block;
}

.support-diagnostics__item-copy strong {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  line-height: 1.35;
}

.support-diagnostics__item-copy small {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.45;
  margin-top: 2px;
}

.support-diagnostics__divider {
  border-top: 1px solid var(--hairline);
  margin: var(--sp-1) 0;
}

.support-diagnostics__privacy {
  align-items: center;
  color: var(--ok);
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  margin: var(--sp-2) var(--sp-2) 2px;
}

@media (max-width: 600px) {
  .support-diagnostics__trigger {
    padding-left: var(--sp-2);
    padding-right: var(--sp-2);
  }
}
</style>
