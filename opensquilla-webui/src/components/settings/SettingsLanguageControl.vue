<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { SUPPORTED_LOCALES, type LocaleCode } from '@/i18n'
import { useAppStore } from '@/stores/app'

const { t } = useI18n()
const appStore = useAppStore()

// Native names remain readable after switching to an unfamiliar locale.
const LOCALE_LABELS: Record<LocaleCode, string> = {
  en: 'English',
  'zh-Hans': '中文',
  ja: '日本語',
  fr: 'Français',
  de: 'Deutsch',
  es: 'Español',
}

const localeOptions = SUPPORTED_LOCALES.map(code => ({ code, label: LOCALE_LABELS[code] }))

function pickLocale(code: LocaleCode) {
  void appStore.setLocale(code)
}
</script>

<template>
  <div class="control-row control-row--stack">
    <div class="control-row__label-block">
      <span class="control-row__label">{{ t('settings.appearance.languageLabel') }}</span>
      <span class="control-row__desc">{{ t('settings.appearance.languageDesc') }}</span>
    </div>
    <div class="control-row__control">
      <div
        class="language-options"
        role="radiogroup"
        :aria-label="t('settings.appearance.languageLabel')"
        data-testid="settings-language-group"
      >
        <label
          v-for="option in localeOptions"
          :key="option.code"
          class="language-options__item"
          :class="{ 'is-active': appStore.locale === option.code }"
        >
          <input
            class="language-options__radio"
            type="radio"
            name="settings-locale"
            :value="option.code"
            :checked="appStore.locale === option.code"
            :data-testid="`settings-language-${option.code}`"
            @change="pickLocale(option.code)"
          >
          <span>{{ option.label }}</span>
        </label>
      </div>
    </div>
  </div>
</template>

<style scoped>
.language-options {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  padding: 2px;
}

.language-options__item {
  align-items: center;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font-size: var(--fs-sm);
  padding: 6px var(--sp-3);
  position: relative;
}

.language-options__item:hover { color: var(--text); }
.language-options__item.is-active {
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
  color: var(--text);
}
.language-options__item:focus-within {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}
.language-options__radio {
  height: 1px;
  margin: 0;
  opacity: 0;
  position: absolute;
  width: 1px;
}
</style>
