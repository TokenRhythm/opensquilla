<template>
  <section class="browser-preview">
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
        type="url"
        inputmode="url"
        autocomplete="off"
        spellcheck="false"
        :aria-label="t('workbench.browser.address')"
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
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { copyTextWithFallback } from '@/utils/browser'
import type { WorkbenchComponentEvent } from '@/workbench/types'

const props = withDefaults(defineProps<{
  canGoBack?: boolean
  canGoForward?: boolean
  currentUrl?: string
  errorMessage?: string
  loading?: boolean
}>(), {
  canGoBack: false,
  canGoForward: false,
  currentUrl: '',
  errorMessage: '',
  loading: false,
})

const emit = defineEmits<{
  'workbench-event': [event: WorkbenchComponentEvent]
}>()
const { t } = useI18n()
const address = ref(props.currentUrl)

watch(() => props.currentUrl, value => {
  address.value = value
})

function emitAction(action: string, url = '') {
  emit('workbench-event', {
    type: 'browser-action',
    payload: { action, ...(url ? { url } : {}) },
  })
}

function navigate() {
  emitAction('navigate', address.value)
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

.browser-preview__native-slot {
  min-width: 0;
  min-height: 0;
  flex: 1;
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
