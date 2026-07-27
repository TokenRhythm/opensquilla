<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAppStore, type ThemeMode } from '@/stores/app'
import { themePickerOptions } from '@/themes/registry'
import { SUPPORTED_LOCALES, type LocaleCode } from '@/i18n'
import Icon from '@/components/Icon.vue'
import ControlSwitch from '@/components/ControlSwitch.vue'
import { useBgm } from '@/composables/useBgm'
import { useSidebarLayout } from '@/composables/useSidebarLayout'
import {
  TOOL_DETAIL_DISPLAY_MODES,
  type ToolDetailDisplayMode,
  useToolDetailPreference,
} from '@/composables/useToolDetailPreference'
import {
  SIDEBAR_MAX_WIDTH,
  SIDEBAR_MIN_WIDTH,
  SIDEBAR_WIDTH_PRESETS,
  type SidebarWidthPreference,
  type SidebarWidthSource,
} from '@/utils/sidebarLayout'

// Client-only preferences: applied instantly to this browser and persisted via
// the app store. No readiness state; never part of the settings dirty bar.
// This is the canonical home for theme AND language — the sidebar theme button
// and the topbar LanguageSwitcher are reactive shortcuts over the SAME store, so
// the surfaces can never drift.
const appStore = useAppStore()
const { t } = useI18n()

// Registry-driven, full list: every selectable value theme (incl. custom ones) +
// system. The compact topbar menu shows only the basic modes (scope: 'basic')
// and links here via "More themes…"; this panel is the home for the full set.
const themeOptions = themePickerOptions({ scope: 'all' })

// Native language names — deliberately NOT translated.
const LOCALE_LABELS: Record<LocaleCode, string> = {
  en: 'English',
  'zh-Hans': '中文',
  ja: '日本語',
  fr: 'Français',
  de: 'Deutsch',
  es: 'Español',
}
const localeOptions = SUPPORTED_LOCALES.map((code) => ({ code, label: LOCALE_LABELS[code] }))
const toolDetailOptions = TOOL_DETAIL_DISPLAY_MODES.map(mode => ({
  mode,
  labelKey: `settings.appearance.toolDetails${mode[0].toUpperCase()}${mode.slice(1)}`,
}))

function pickTheme(mode: ThemeMode) {
  appStore.setTheme(mode)
}

function pickLocale(code: LocaleCode) {
  void appStore.setLocale(code)
}

const {
  mode: toolDetailDisplayMode,
  setMode: setToolDetailDisplayMode,
} = useToolDetailPreference()

function pickToolDetailDisplay(mode: ToolDetailDisplayMode) {
  setToolDetailDisplayMode(mode)
}

// Background-music feature gate (off by default). Same singleton the topbar
// control and the command palette read, so all three surfaces stay in lockstep.
const { enabled: bgmEnabled, setEnabled: setBgmEnabled } = useBgm()

const {
  mode: sidebarLayoutMode,
  effectiveWidth: sidebarEffectiveWidth,
  preferenceLimited: sidebarPreferenceLimited,
} = useSidebarLayout()

type NamedSidebarWidthSource = Exclude<SidebarWidthSource, 'custom'>

const sidebarWidthOptions: Array<{
  source: SidebarWidthSource
  width: number | null
  labelKey: string
}> = [
  {
    source: 'compact',
    width: SIDEBAR_WIDTH_PRESETS.compact.width,
    labelKey: 'settings.appearance.sidebarWidthCompact',
  },
  {
    source: 'default',
    width: SIDEBAR_WIDTH_PRESETS.default.width,
    labelKey: 'settings.appearance.sidebarWidthDefault',
  },
  {
    source: 'wide',
    width: SIDEBAR_WIDTH_PRESETS.wide.width,
    labelKey: 'settings.appearance.sidebarWidthWide',
  },
  {
    source: 'custom',
    width: null,
    labelKey: 'settings.appearance.sidebarWidthCustom',
  },
]

const selectedSidebarWidthSource = ref<SidebarWidthSource>(appStore.sidebarWidthPreference.source)
const customSidebarWidthDraft = ref(String(appStore.sidebarWidthPreference.width))
const customSidebarWidthInputRef = ref<HTMLInputElement | null>(null)

const customSidebarWidth = computed<number | null>(() => {
  const draft = String(customSidebarWidthDraft.value).trim()
  if (!/^\d+$/.test(draft)) return null
  const width = Number(draft)
  if (!Number.isInteger(width) || width < SIDEBAR_MIN_WIDTH || width > SIDEBAR_MAX_WIDTH) {
    return null
  }
  return width
})
const customWidthSelected = computed(() => selectedSidebarWidthSource.value === 'custom')
const canDecreaseCustomWidth = computed(() => (
  customWidthSelected.value
  && (customSidebarWidth.value === null || customSidebarWidth.value > SIDEBAR_MIN_WIDTH)
))
const canIncreaseCustomWidth = computed(() => (
  customWidthSelected.value
  && (customSidebarWidth.value === null || customSidebarWidth.value < SIDEBAR_MAX_WIDTH)
))

const sidebarModeLabel = computed(() => {
  if (sidebarLayoutMode.value === 'drawer') return t('settings.appearance.sidebarWidthModeDrawer')
  if (sidebarLayoutMode.value === 'compact') return t('settings.appearance.sidebarWidthModeCompact')
  return t('settings.appearance.sidebarWidthModeResizable')
})

const sidebarWidthStatus = computed(() => {
  const saved = appStore.sidebarWidthPreference.width
  const current = sidebarEffectiveWidth.value
  if (sidebarLayoutMode.value === 'drawer') {
    return t('settings.appearance.sidebarWidthDrawerStatus', { saved, current })
  }
  if (sidebarLayoutMode.value === 'compact') {
    return t('settings.appearance.sidebarWidthCompactStatus', { saved, current })
  }
  if (sidebarPreferenceLimited.value) {
    return t('settings.appearance.sidebarWidthClampedStatus', { saved, current })
  }
  return t('settings.appearance.sidebarWidthResizableStatus', { current })
})

function chooseSidebarWidth(source: SidebarWidthSource) {
  selectedSidebarWidthSource.value = source
  if (source === 'custom') return
  if (source === 'default') {
    appStore.resetSidebarWidthPreference()
    return
  }
  appStore.setSidebarWidthPreference({ ...SIDEBAR_WIDTH_PRESETS[source as NamedSidebarWidthSource] })
}

function applyCustomSidebarWidth() {
  const width = customSidebarWidth.value
  if (width === null) return
  const preference: SidebarWidthPreference = { version: 1, width, source: 'custom' }
  appStore.setSidebarWidthPreference(preference)
  selectedSidebarWidthSource.value = 'custom'
}

function stepCustomSidebarWidth(delta: -1 | 1) {
  const base = customSidebarWidth.value ?? appStore.sidebarWidthPreference.width
  const next = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, base + delta))
  customSidebarWidthDraft.value = String(next)
}

const STEP_REPEAT_DELAY_MS = 400
const STEP_REPEAT_INTERVAL_MS = 60
let stepRepeatDelay = 0
let stepRepeatInterval = 0

function stopCustomStepRepeat() {
  if (stepRepeatDelay) window.clearTimeout(stepRepeatDelay)
  if (stepRepeatInterval) window.clearInterval(stepRepeatInterval)
  stepRepeatDelay = 0
  stepRepeatInterval = 0
}

function startCustomStepRepeat(delta: -1 | 1, event: PointerEvent) {
  if (event.button !== 0) return
  stopCustomStepRepeat()
  const button = event.currentTarget as HTMLButtonElement
  try {
    button.setPointerCapture?.(event.pointerId)
  } catch {
    // Synthetic pointer events and older webviews can reject capture. The
    // repeat still stops through pointerup/leave/cancel handlers.
  }
  stepCustomSidebarWidth(delta)
  stepRepeatDelay = window.setTimeout(() => {
    stepCustomSidebarWidth(delta)
    stepRepeatInterval = window.setInterval(() => stepCustomSidebarWidth(delta), STEP_REPEAT_INTERVAL_MS)
  }, STEP_REPEAT_DELAY_MS)
}

function onCustomStepClick(delta: -1 | 1, event: MouseEvent) {
  // Pointer activation already applies the first step on pointerdown so a held
  // press can repeat. Keyboard/synthetic clicks have detail=0 and still need
  // their single step, without remounting the control or moving focus.
  if (event.detail > 0) return
  stepCustomSidebarWidth(delta)
}

watch(() => appStore.sidebarWidthPreference, (preference) => {
  selectedSidebarWidthSource.value = preference.source
  if (
    preference.source === 'custom'
    && document.activeElement !== customSidebarWidthInputRef.value
  ) {
    customSidebarWidthDraft.value = String(preference.width)
  }
}, { deep: true })

onBeforeUnmount(stopCustomStepRepeat)
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('settings.appearance.title') }}</h3>
      <p class="control-section__desc">{{ t('settings.appearance.desc') }}</p>
    </div>

    <div class="control-row control-row--stack">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.appearance.themeLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.appearance.themeDesc') }}</span>
      </div>
      <div class="control-row__control">
        <!-- Native radio group: the browser handles arrow-key roving, focus and
             state announcement; the inputs are visually hidden and the labels
             render the segmented control. -->
        <div class="appearance-theme" role="radiogroup" :aria-label="t('settings.appearance.themeLabel')">
          <label
            v-for="opt in themeOptions"
            :key="opt.mode"
            class="appearance-theme__opt"
            :class="{ 'is-active': appStore.theme === opt.mode }"
          >
            <input
              class="appearance-theme__radio"
              type="radio"
              name="appearance-theme"
              :value="opt.mode"
              :checked="appStore.theme === opt.mode"
              @change="pickTheme(opt.mode)"
            >
            <Icon :name="opt.icon" :size="15" aria-hidden="true" />
            <span>{{ opt.labelKey ? t(opt.labelKey) : opt.label }}</span>
          </label>
        </div>
      </div>
    </div>

    <div class="control-row control-row--stack">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.appearance.languageLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.appearance.languageDesc') }}</span>
      </div>
      <div class="control-row__control">
        <div
          class="appearance-theme"
          role="radiogroup"
          :aria-label="t('settings.appearance.languageLabel')"
          data-testid="settings-language-group"
        >
          <label
            v-for="opt in localeOptions"
            :key="opt.code"
            class="appearance-theme__opt"
            :class="{ 'is-active': appStore.locale === opt.code }"
          >
            <input
              class="appearance-theme__radio"
              type="radio"
              name="appearance-locale"
              :value="opt.code"
              :checked="appStore.locale === opt.code"
              :data-testid="`settings-language-${opt.code}`"
              @change="pickLocale(opt.code)"
            >
            <span>{{ opt.label }}</span>
          </label>
        </div>
      </div>
    </div>

    <div class="control-row control-row--stack">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.appearance.toolDetailsLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.appearance.toolDetailsDesc') }}</span>
      </div>
      <div class="control-row__control">
        <div
          class="appearance-theme"
          role="radiogroup"
          :aria-label="t('settings.appearance.toolDetailsLabel')"
          data-testid="settings-tool-details-group"
        >
          <label
            v-for="option in toolDetailOptions"
            :key="option.mode"
            class="appearance-theme__opt"
            :class="{ 'is-active': toolDetailDisplayMode === option.mode }"
          >
            <input
              class="appearance-theme__radio"
              type="radio"
              name="appearance-tool-details"
              :value="option.mode"
              :checked="toolDetailDisplayMode === option.mode"
              :data-testid="`settings-tool-details-${option.mode}`"
              @change="pickToolDetailDisplay(option.mode)"
            >
            <span>{{ t(option.labelKey) }}</span>
          </label>
        </div>
      </div>
    </div>

    <div class="control-row control-row--stack">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.appearance.sidebarWidthLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.appearance.sidebarWidthDesc') }}</span>
      </div>
      <div class="control-row__control sidebar-width">
        <div
          class="appearance-theme sidebar-width__presets"
          role="radiogroup"
          :aria-label="t('settings.appearance.sidebarWidthLabel')"
          data-testid="settings-sidebar-width-group"
        >
          <label
            v-for="option in sidebarWidthOptions"
            :key="option.source"
            class="appearance-theme__opt sidebar-width__preset"
            :class="{ 'is-active': selectedSidebarWidthSource === option.source }"
          >
            <input
              class="appearance-theme__radio"
              type="radio"
              name="appearance-sidebar-width"
              :value="option.source"
              :checked="selectedSidebarWidthSource === option.source"
              :data-testid="`settings-sidebar-width-${option.source}`"
              @change="chooseSidebarWidth(option.source)"
            >
            <span>{{ t(option.labelKey) }}</span>
            <span v-if="option.width !== null" class="sidebar-width__preset-value">{{ option.width }}px</span>
          </label>
        </div>

        <div
          class="sidebar-width__custom"
          :class="{ 'is-disabled': !customWidthSelected }"
          data-testid="settings-sidebar-width-custom-controls"
        >
          <div class="sidebar-width__custom-copy">
            <label class="sidebar-width__custom-label" for="settings-sidebar-width-value">
              {{ t('settings.appearance.sidebarWidthCustomLabel') }}
            </label>
            <span class="sidebar-width__custom-hint">{{ t('settings.appearance.sidebarWidthCustomHint') }}</span>
          </div>
          <div class="sidebar-width__custom-controls">
            <button
              type="button"
              class="sidebar-width__step"
              :disabled="!canDecreaseCustomWidth"
              :aria-label="t('settings.appearance.sidebarWidthDecrease')"
              data-testid="settings-sidebar-width-decrease"
              @pointerdown="startCustomStepRepeat(-1, $event)"
              @pointerup="stopCustomStepRepeat"
              @pointercancel="stopCustomStepRepeat"
              @pointerleave="stopCustomStepRepeat"
              @lostpointercapture="stopCustomStepRepeat"
              @blur="stopCustomStepRepeat"
              @click="onCustomStepClick(-1, $event)"
            >−</button>
            <div class="sidebar-width__input-wrap">
              <input
                id="settings-sidebar-width-value"
                ref="customSidebarWidthInputRef"
                v-model="customSidebarWidthDraft"
                class="control-input sidebar-width__input"
                type="number"
                inputmode="numeric"
                step="1"
                :min="SIDEBAR_MIN_WIDTH"
                :max="SIDEBAR_MAX_WIDTH"
                :disabled="!customWidthSelected"
                :aria-invalid="customWidthSelected && customSidebarWidth === null ? 'true' : undefined"
                aria-describedby="settings-sidebar-width-range"
                data-testid="settings-sidebar-width-value"
                @keydown.enter.prevent="applyCustomSidebarWidth"
              >
              <span aria-hidden="true">px</span>
            </div>
            <button
              type="button"
              class="sidebar-width__step"
              :disabled="!canIncreaseCustomWidth"
              :aria-label="t('settings.appearance.sidebarWidthIncrease')"
              data-testid="settings-sidebar-width-increase"
              @pointerdown="startCustomStepRepeat(1, $event)"
              @pointerup="stopCustomStepRepeat"
              @pointercancel="stopCustomStepRepeat"
              @pointerleave="stopCustomStepRepeat"
              @lostpointercapture="stopCustomStepRepeat"
              @blur="stopCustomStepRepeat"
              @click="onCustomStepClick(1, $event)"
            >+</button>
            <button
              type="button"
              class="btn btn--primary sidebar-width__apply"
              :disabled="!customWidthSelected || customSidebarWidth === null"
              data-testid="settings-sidebar-width-apply"
              @click="applyCustomSidebarWidth"
            >{{ t('settings.appearance.sidebarWidthApply') }}</button>
          </div>
          <span id="settings-sidebar-width-range" class="sidebar-width__range">
            {{ t('settings.appearance.sidebarWidthRange', { min: SIDEBAR_MIN_WIDTH, max: SIDEBAR_MAX_WIDTH }) }}
          </span>
        </div>

        <div class="sidebar-width__status" aria-live="polite" data-testid="settings-sidebar-width-status">
          <span class="sidebar-width__mode">{{ sidebarModeLabel }}</span>
          <span>{{ sidebarWidthStatus }}</span>
        </div>
      </div>
    </div>

    <div class="control-row control-row--stack">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('settings.appearance.bgmLabel') }}</span>
        <span class="control-row__desc">{{ t('settings.appearance.bgmDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="bgmEnabled"
          :aria-label="t('settings.appearance.bgmLabel')"
          name="appearance-bgm"
          data-testid="settings-bgm-toggle"
          @change="setBgmEnabled"
        />
      </div>
    </div>
  </section>
</template>

<style scoped>
.appearance-theme {
  /* Wraps to multiple rows so many themes / locales never overflow or crush the
     row (the parent row is .control-row--stack, so this fills the width). */
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  padding: 2px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.appearance-theme__opt {
  align-items: center;
  background: transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font-size: var(--fs-sm);
  gap: var(--sp-1);
  padding: 6px var(--sp-3);
  position: relative;
}

/* Visually hidden but focusable / arrow-navigable native radio. */
.appearance-theme__radio {
  height: 1px;
  margin: 0;
  opacity: 0;
  position: absolute;
  width: 1px;
}

.appearance-theme__opt:hover {
  color: var(--text);
}

.appearance-theme__opt.is-active {
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
  color: var(--text);
}

.appearance-theme__opt:focus-within {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.sidebar-width {
  align-items: stretch;
  flex-direction: column;
}

.sidebar-width__presets {
  align-self: flex-start;
}

.sidebar-width__preset-value {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.sidebar-width__custom {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
  justify-content: space-between;
  min-width: 0;
  padding-top: var(--sp-1);
}

.sidebar-width__custom.is-disabled {
  color: var(--text-dim);
}

.sidebar-width__custom-copy {
  display: flex;
  flex: 1 1 220px;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.sidebar-width__custom-label {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.sidebar-width__custom-hint,
.sidebar-width__range {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.sidebar-width__custom-controls {
  align-items: center;
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-2);
}

.sidebar-width__step {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-lg);
  height: 44px;
  justify-content: center;
  line-height: 1;
  min-width: 44px;
  padding: 0;
  touch-action: manipulation;
  width: 44px;
}

.sidebar-width__step:not(:disabled):hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
}

.sidebar-width__step:focus-visible,
.sidebar-width__apply:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.sidebar-width__step:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.sidebar-width__input-wrap {
  align-items: center;
  display: flex;
  gap: var(--sp-1);
}

.sidebar-width__input-wrap .sidebar-width__input {
  height: 44px;
  max-width: 92px;
  padding: var(--sp-2);
  text-align: right;
  width: 92px;
}

.sidebar-width__input::-webkit-inner-spin-button,
.sidebar-width__input::-webkit-outer-spin-button {
  appearance: none;
  margin: 0;
}

.sidebar-width__apply {
  min-height: 44px;
}

.sidebar-width__range {
  flex-basis: 100%;
}

.sidebar-width__status {
  align-items: center;
  color: var(--text-dim);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
}

.sidebar-width__mode {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  font-weight: 600;
  padding: 2px 7px;
  white-space: nowrap;
}

@media (max-width: 560px) {
  .sidebar-width__presets,
  .sidebar-width__custom-controls {
    width: 100%;
  }

  .sidebar-width__custom-controls {
    flex-wrap: wrap;
  }

  .sidebar-width__apply {
    flex: 1 1 auto;
  }
}

@media (forced-colors: active) {
  /* High Contrast removes the shadow/background distinction used by the
     segmented controls. Keep the checked option visibly selected even after
     focus leaves it with a real system-colour outline. */
  .appearance-theme__opt.is-active {
    background: Canvas;
    color: CanvasText;
    outline: 2px solid Highlight;
    outline-offset: -2px;
  }

  .appearance-theme__opt:focus-within {
    outline: 2px dashed Highlight;
    outline-offset: 2px;
  }
}
</style>
