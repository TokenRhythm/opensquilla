<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAppStore } from '@/stores/app'
import { SUPPORTED_LOCALES, type LocaleCode } from '@/i18n'
import Icon from '@/components/Icon.vue'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useChatTopbarPopoverCoordination } from '@/composables/useChatTopbarPopoverCoordinator'

// Topbar language switcher. Mirrors the theme menu next to it (reuses the global
// .theme-menu* classes) so the two controls can never drift in look or
// behaviour. Locale labels are the languages' own native names and are NOT
// translated. Writes through appStore.setLocale, the same store the Settings
// Appearance Language row uses, so both surfaces stay in lockstep.

const appStore = useAppStore()
const { t } = useI18n()

const LOCALE_LABELS: Record<LocaleCode, string> = {
  en: 'English',
  'zh-Hans': '中文',
  ja: '日本語',
  fr: 'Français',
  de: 'Deutsch',
  es: 'Español',
}
const localeOptions = SUPPORTED_LOCALES.map((code) => ({ code, label: LOCALE_LABELS[code] }))

const menuOpen = ref(false)
const buttonRef = ref<HTMLButtonElement | null>(null)
useChatTopbarPopoverCoordination('language', menuOpen)
const menuIsTopmost = useDialogLayer(computed(() => menuOpen.value))

function pick(code: LocaleCode) {
  void appStore.setLocale(code)
  menuOpen.value = false
  buttonRef.value?.focus()
}

useDocumentEvent('click', (e) => {
  if (!menuOpen.value) return
  const wrap = buttonRef.value?.closest('.lang-menu-wrap')
  if (wrap && e.target instanceof Node && !wrap.contains(e.target)) {
    menuOpen.value = false
  }
})

useDocumentEvent('keydown', (e) => {
  if (e.defaultPrevented || e.key !== 'Escape') return
  if (!menuOpen.value || !menuIsTopmost.value) return
  e.preventDefault()
  menuOpen.value = false
  buttonRef.value?.focus()
})
</script>

<template>
  <div class="theme-menu-wrap lang-menu-wrap">
    <button
      ref="buttonRef"
      type="button"
      class="btn btn--ghost lang-menu-trigger"
      :title="t('chrome.language')"
      :aria-label="t('chrome.language')"
      aria-haspopup="menu"
      :aria-expanded="menuOpen"
      data-testid="language-switcher-trigger"
      @click.stop="menuOpen = !menuOpen"
    >
      <Icon name="languages" :size="16" />
      <span class="lang-menu-current">{{ LOCALE_LABELS[appStore.locale] }}</span>
    </button>
    <div
      v-if="menuOpen"
      class="theme-menu lang-menu"
      role="menu"
      :aria-label="t('chrome.language')"
      data-chat-topbar-popover="language"
    >
      <button
        v-for="opt in localeOptions"
        :key="opt.code"
        type="button"
        class="theme-menu__item"
        role="menuitemradio"
        :aria-checked="appStore.locale === opt.code"
        :data-testid="`language-option-${opt.code}`"
        @click="pick(opt.code)"
      >
        <span>{{ opt.label }}</span>
        <Icon v-if="appStore.locale === opt.code" class="theme-menu__check" name="check" :size="14" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.lang-menu-trigger {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  width: auto;
  padding: 0 var(--sp-2);
  color: var(--text-muted);
}

.lang-menu-trigger:hover {
  color: var(--text);
}

.lang-menu-current {
  font-size: var(--fs-sm);
}

.lang-menu {
  min-width: 140px;
}
</style>
