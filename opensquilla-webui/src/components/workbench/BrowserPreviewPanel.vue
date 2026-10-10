<template>
  <section class="browser-preview" @keydown.capture="onPanelKeydown">
    <form class="browser-preview__toolbar" @submit.prevent="navigate">
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.back')"
        :disabled="!canGoBack"
        @click="emitAction('back')"
      >
        <Icon name="chevronLeft" :size="15" />
      </button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.forward')"
        :disabled="!canGoForward"
        @click="emitAction('forward')"
      >
        <Icon name="chevronRight" :size="15" />
      </button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="loading ? t('workbench.browser.stop') : t('workbench.refresh')"
        @click="emitAction(loading ? 'stop' : 'reload')"
      >
        <Icon :name="loading ? 'x' : 'refresh'" :size="15" />
      </button>
      <input
        v-model="address"
        class="browser-preview__address"
        type="text"
        inputmode="url"
        autocomplete="off"
        spellcheck="false"
        :aria-label="t('workbench.browser.address')"
        :aria-invalid="invalidAddress || undefined"
        @input="invalidAddress = false"
      >
      <button type="submit" class="btn btn--ghost">
        {{ t('workbench.browser.go') }}
      </button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.copyUrl')"
        @click="copyUrl"
      >
        <Icon name="copy" :size="15" />
      </button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.openExternal')"
        @click="emitAction('open-external')"
      >
        <Icon name="externalLink" :size="15" />
      </button>
    </form>
    <div class="browser-preview__reading-tools">
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.find')"
        :aria-expanded="findOpen"
        :title="t('workbench.browser.find')"
        @click="openFind"
      >
        <Icon name="search" :size="15" />
      </button>
      <div class="browser-preview__zoom" role="group" :aria-label="t('workbench.browser.zoom')">
        <button
          type="button"
          class="btn btn--ghost browser-preview__zoom-step"
          :aria-label="t('workbench.browser.zoomOut')"
          :disabled="zoomFactor <= 0.5"
          @click="changeZoom(-1)"
        >−</button>
        <button
          type="button"
          class="btn btn--ghost browser-preview__zoom-value"
          :aria-label="t('workbench.browser.zoomReset')"
          :title="t('workbench.browser.zoomReset')"
          @click="setZoom(1)"
        >{{ zoomPercent }}</button>
        <button
          type="button"
          class="btn btn--ghost browser-preview__zoom-step"
          :aria-label="t('workbench.browser.zoomIn')"
          :disabled="zoomFactor >= 3"
          @click="changeZoom(1)"
        >+</button>
      </div>
    </div>
    <div v-if="findOpen" class="browser-preview__find" role="search">
      <input
        ref="findInput"
        v-model="findText"
        class="browser-preview__find-input"
        type="search"
        maxlength="512"
        autocomplete="off"
        :aria-label="t('workbench.browser.find')"
        :placeholder="t('workbench.browser.findPlaceholder')"
        @input="onFindInput"
        @keydown.enter.prevent="findNext(!$event.shiftKey)"
      >
      <span class="browser-preview__find-count" role="status" aria-live="polite">{{ findCount }}</span>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.findPrevious')"
        :disabled="!findText"
        @click="findNext(false)"
      ><Icon name="arrowUp" :size="15" /></button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.findNext')"
        :disabled="!findText"
        @click="findNext(true)"
      ><Icon name="chevronDown" :size="15" /></button>
      <button
        type="button"
        class="btn btn--icon btn--ghost"
        :aria-label="t('workbench.browser.findClose')"
        @click="closeFind"
      ><Icon name="x" :size="15" /></button>
    </div>
    <div v-if="downloadId && downloadState" class="browser-preview__download" role="status" aria-live="polite">
      <span class="browser-preview__download-name" :title="downloadName">{{ downloadStatus }}</span>
      <span v-if="downloadState === 'progressing' && downloadPercent !== null">{{ downloadPercent }}%</span>
      <button v-if="downloadState === 'completed'" type="button" class="btn btn--ghost"
        :aria-label="t('workbench.browser.downloadOpen')"
        @click="emitAction('download-open', { downloadId })">
        {{ t('workbench.browser.downloadOpen') }}
      </button>
    </div>
    <div v-if="controlError" class="browser-preview__control-error" role="alert">
      {{ controlError }}
    </div>
    <div v-if="invalidAddress" class="browser-preview__address-error" role="alert">
      {{ t('workbench.browser.invalidAddress') }}
    </div>
    <div
      v-if="errorMessage"
      class="browser-preview__error"
      role="alert"
    >
      <Icon name="info" :size="18" />
      <strong>{{ t('workbench.browser.failed') }}</strong>
      <span>{{ errorMessage }}</span>
      <button type="button" class="btn btn--ghost" @click="emitAction('reload')">
        <Icon name="refresh" :size="14" />
        {{ t('chat.retry') }}
      </button>
    </div>
    <div
      v-else
      class="browser-preview__native-slot"
      data-workbench-native-surface-slot
      :aria-label="t('workbench.browser.preview')"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { copyTextWithFallback } from '@/utils/browser'
import { normalizeBrowserAddress } from '@/workbench/browserItems'
import type { WorkbenchComponentEvent } from '@/workbench/types'

const props = withDefaults(defineProps<{
  canGoBack?: boolean
  canGoForward?: boolean
  currentUrl?: string
  errorMessage?: string
  loading?: boolean
  findOpen?: boolean
  findQuery?: string
  findMatches?: number | null
  findActiveMatch?: number
  zoomFactor?: number
  downloadId?: string
  downloadName?: string
  downloadState?: 'progressing' | 'completed' | 'cancelled' | 'interrupted' | ''
  downloadReceivedBytes?: number
  downloadTotalBytes?: number
  controlError?: string
}>(), {
  canGoBack: false,
  canGoForward: false,
  currentUrl: '',
  errorMessage: '',
  loading: false,
  findOpen: false,
  findQuery: '',
  findMatches: null,
  findActiveMatch: 0,
  zoomFactor: 1,
  downloadId: '',
  downloadName: '',
  downloadState: '',
  downloadReceivedBytes: 0,
  downloadTotalBytes: 0,
  controlError: '',
})

const emit = defineEmits<{
  'workbench-event': [event: WorkbenchComponentEvent]
}>()
const { t } = useI18n()
const address = ref(props.currentUrl)
const invalidAddress = ref(false)
const findInput = ref<HTMLInputElement | null>(null)
const findText = ref(props.findQuery)
const lastFindQuery = ref(props.findQuery)
const ZOOM_STEPS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3]
let findTimer: ReturnType<typeof setTimeout> | null = null

const zoomPercent = computed(() => `${Math.round(props.zoomFactor * 100)}%`)
const downloadPercent = computed(() => props.downloadTotalBytes > 0
  ? Math.min(100, Math.floor(100 * props.downloadReceivedBytes / props.downloadTotalBytes)) : null)
const downloadStatus = computed(() => {
  const key = props.downloadState === 'completed' ? 'downloadCompleted'
    : props.downloadState === 'cancelled' ? 'downloadCancelled'
      : props.downloadState === 'interrupted' ? 'downloadInterrupted' : 'downloadProgress'
  return t(`workbench.browser.${key}`, { name: props.downloadName })
})
const findCount = computed(() => props.findMatches === null || findText.value !== props.findQuery
  ? ''
  : t('workbench.browser.findCount', {
    current: props.findActiveMatch,
    total: props.findMatches,
  }))

watch(() => props.currentUrl, value => {
  address.value = value
  invalidAddress.value = false
})

watch(() => props.findOpen, opened => {
  if (opened) void nextTick(() => findInput.value?.focus({ preventScroll: true }))
})

watch(() => props.findQuery, value => {
  findText.value = value
  lastFindQuery.value = value
})

onBeforeUnmount(() => clearFindTimer())

function emitAction(action: string, detail: Record<string, unknown> = {}) {
  emit('workbench-event', {
    type: 'browser-action',
    payload: { action, ...detail },
  })
}

function navigate() {
  const url = normalizeBrowserAddress(address.value)
  invalidAddress.value = !url
  if (url) emitAction('navigate', { url })
}

function clearFindTimer() {
  if (findTimer !== null) clearTimeout(findTimer)
  findTimer = null
}

function openFind() {
  emitAction('find-open')
  void nextTick(() => findInput.value?.focus({ preventScroll: true }))
}

function closeFind() {
  clearFindTimer()
  findText.value = ''
  lastFindQuery.value = ''
  emitAction('find-close')
}

function onFindInput() {
  clearFindTimer()
  if (!findText.value) {
    lastFindQuery.value = ''
    emitAction('find-stop')
    return
  }
  findTimer = setTimeout(() => {
    findTimer = null
    lastFindQuery.value = findText.value
    emitAction('find', { query: findText.value })
  }, 120)
}

function findNext(forward: boolean) {
  if (!findText.value) return
  if (lastFindQuery.value !== findText.value) {
    clearFindTimer()
    lastFindQuery.value = findText.value
    emitAction('find', { query: findText.value })
    return
  }
  emitAction('find-next', { forward })
}

function onPanelKeydown(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'f') {
    event.preventDefault()
    event.stopPropagation()
    openFind()
  } else if (event.key === 'Escape' && props.findOpen) {
    event.preventDefault()
    event.stopPropagation()
    closeFind()
  }
}

function setZoom(zoomFactor: number) {
  if (Math.abs(props.zoomFactor - zoomFactor) < 0.001) return
  emitAction('zoom', { zoomFactor })
}

function changeZoom(direction: -1 | 1) {
  const next = direction > 0
    ? ZOOM_STEPS.find(value => value > props.zoomFactor + 0.001)
    : [...ZOOM_STEPS].reverse().find(value => value < props.zoomFactor - 0.001)
  if (next !== undefined) setZoom(next)
}

async function copyUrl() {
  const value = props.currentUrl || address.value
  if (!value) return
  try {
    await copyTextWithFallback(value)
  } catch {}
}
</script>

<style scoped>
.browser-preview {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  background: var(--bg-surface);
}

.browser-preview__toolbar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--sp-1);
  min-height: 46px;
  padding: var(--sp-2);
  border-bottom: 1px solid var(--border);
}

.browser-preview__address {
  min-width: 0;
  height: 32px;
  flex: 1;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
}

.browser-preview__reading-tools,
.browser-preview__find {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--sp-1);
  min-width: 0;
  padding: var(--sp-1) var(--sp-2);
  border-bottom: 1px solid var(--border);
}

.browser-preview__download {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  padding: var(--sp-1) var(--sp-2);
  border-bottom: 1px solid var(--border);
  font-size: var(--fs-sm);
}

.browser-preview__download-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.browser-preview__zoom {
  display: flex;
  align-items: center;
  margin-left: auto;
}

.browser-preview__zoom-step,
.browser-preview__zoom-value {
  min-width: 30px;
  padding: 0 var(--sp-1);
  font-size: var(--fs-sm);
}

.browser-preview__zoom-value {
  min-width: 49px;
}

.browser-preview__find-input {
  flex: 1;
  min-width: 0;
  height: 30px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
}

.browser-preview__find-count {
  min-width: 42px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
  text-align: center;
}

.browser-preview__control-error {
  padding: var(--sp-1) var(--sp-3);
  color: var(--danger);
  font-size: var(--fs-sm);
}

.browser-preview__native-slot {
  min-width: 0;
  min-height: 0;
  flex: 1;
}

.browser-preview__address-error {
  padding: var(--sp-2) var(--sp-3);
  color: var(--danger);
  font-size: var(--fs-sm);
}

.browser-preview__error {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-5);
  color: var(--text-muted);
  text-align: center;
}
</style>
