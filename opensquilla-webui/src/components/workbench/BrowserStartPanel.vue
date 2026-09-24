<template>
  <section class="browser-start" data-testid="browser-start">
    <Icon name="languages" :size="30" aria-hidden="true" />
    <h2>{{ t(external ? 'chat.composer.browserUse' : 'workbench.browser.newTab') }}</h2>
    <p>{{ t(external ? 'chat.composer.browserWindowHint' : 'workbench.browser.startHint') }}</p>
    <form class="browser-start__form" @submit.prevent="open">
      <input
        ref="addressRef"
        v-model="address"
        class="browser-start__address"
        type="text"
        inputmode="url"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        :aria-label="t('workbench.browser.address')"
        :aria-invalid="invalid || undefined"
        :placeholder="t('workbench.browser.addressPlaceholder')"
        @input="invalid = false"
      >
      <button type="submit" class="btn btn--primary" :disabled="!ready">
        {{ t(external ? 'workbench.openExternal' : 'workbench.browser.go') }}
      </button>
    </form>
    <p v-if="invalid" class="browser-start__error" role="alert">
      {{ t('workbench.browser.invalidAddress') }}
    </p>
    <button v-if="canReopen" type="button" class="btn btn--ghost" @click="$emit('reopen')">
      <Icon name="refresh" :size="14" aria-hidden="true" />
      {{ t('workbench.browser.reopen') }}
    </button>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { normalizeBrowserAddress } from '@/workbench/browserItems'

const props = defineProps<{ ready: boolean; canReopen: boolean; external?: boolean }>()
const emit = defineEmits<{ open: [url: string]; reopen: [] }>()
const { t } = useI18n()
const address = ref('')
const invalid = ref(false)
const addressRef = ref<HTMLInputElement | null>(null)

function open() {
  if (!props.ready) return
  const url = normalizeBrowserAddress(address.value)
  invalid.value = !url
  if (url) emit('open', url)
}

onMounted(() => addressRef.value?.focus({ preventScroll: true }))
</script>

<style scoped>
.browser-start {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  padding: var(--sp-5);
  color: var(--text-dim);
  text-align: center;
}

.browser-start h2,
.browser-start p {
  margin: 0;
}

.browser-start h2 {
  color: var(--text);
  font-size: var(--fs-lg);
}

.browser-start p {
  font-size: var(--fs-sm);
}

.browser-start__form {
  display: flex;
  width: 100%;
  max-width: 440px;
  gap: var(--sp-2);
  margin-block: var(--sp-2);
}

.browser-start__address {
  min-width: 0;
  height: 38px;
  flex: 1;
  padding: 0 var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
}

.browser-start__address:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.browser-start__error {
  color: var(--danger);
}
</style>
