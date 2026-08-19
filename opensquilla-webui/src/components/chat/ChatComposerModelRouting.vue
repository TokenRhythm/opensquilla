<template>
  <section
    ref="rootRef"
    tabindex="-1"
    class="composer-model-routing"
    role="dialog"
    :aria-label="t('chat.composer.sessionModelRouting')"
    :aria-busy="busy ? 'true' : 'false'"
    @keydown.esc.stop="$emit('close')"
  >
    <div class="composer-model-routing__head">
      <span>{{ t('chat.composer.sessionModelRouting') }}</span>
      <button type="button" class="composer-model-routing__close" :aria-label="t('chat.closeComposerSettings')" @click="$emit('close')">
        <Icon name="x" :size="14" />
      </button>
    </div>

    <p class="composer-model-routing__scope">{{ t('chat.composer.modelRoutingSessionScope') }}</p>

    <div class="composer-model-routing__list" role="radiogroup" :aria-label="t('chat.composer.sessionModelRouting')">
      <button
        v-for="option in modelRoutingOptions"
        :key="option.value"
        type="button"
        class="composer-model-routing__option"
        :class="[`composer-model-routing__option--${option.value}`, { 'is-active': selectedMode === option.value }]"
        :aria-disabled="busy ? 'true' : 'false'"
        role="radio"
        :aria-checked="selectedMode === option.value ? 'true' : 'false'"
        @click="selectMode(option.value)"
      >
        <span class="composer-model-routing__option-main">
          <span class="composer-model-routing__option-title">
            <span class="composer-model-routing__option-mark" aria-hidden="true"></span>
            <span class="composer-model-routing__option-label">{{ option.label }}</span>
          </span>
          <Icon v-if="selectedMode === option.value" name="check" :size="14" />
        </span>
        <span class="composer-model-routing__option-desc">{{ option.description }}</span>
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ModelRoutingMode } from '@/types/modelRouting'
import { normalizeModelRoutingMode } from '@/types/modelRouting'

const { t } = useI18n()

const props = defineProps<{
  modelRoutingMode: ModelRoutingMode
  busy: boolean
}>()

const emit = defineEmits<{
  close: []
  setSessionRoutingMode: [mode: ModelRoutingMode]
}>()

const modelRoutingOptions = computed(() => [
  {
    value: 'off',
    label: t('chat.composer.modelRoutingOff'),
    description: t('chat.composer.modelRoutingOffDesc'),
  },
  {
    value: 'squilla_router',
    label: t('chat.composer.modelRoutingSquillaRouter'),
    description: t('chat.composer.modelRoutingSquillaRouterDesc'),
  },
  {
    value: 'llm_ensemble',
    label: t('chat.composer.modelRoutingEnsemble'),
    description: t('chat.composer.modelRoutingEnsembleDesc'),
  },
] satisfies Array<{ value: ModelRoutingMode; label: string; description: string }>)

const selectedMode = computed(() => normalizeModelRoutingMode(props.modelRoutingMode))

function selectMode(mode: ModelRoutingMode) {
  if (props.busy) return
  emit('setSessionRoutingMode', mode)
}

const rootRef = ref<HTMLElement | null>(null)
onMounted(() => rootRef.value?.focus())
</script>

<style scoped>
.composer-model-routing {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  width: min(360px, calc(100vw - 48px));
  padding: 0.75rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xl);
  z-index: 30;
}

.composer-model-routing__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.625rem;
  font-size: 0.8125rem;
  font-weight: 700;
  color: var(--text);
}

.composer-model-routing__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.composer-model-routing__close:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.composer-model-routing__list {
  display: grid;
  gap: 0.375rem;
}

.composer-model-routing__scope {
  margin: -0.1rem 0 0.625rem;
  color: var(--text-muted);
  font-size: 0.75rem;
  line-height: 1.35;
}

.composer-model-routing__option {
  display: grid;
  gap: 0.25rem;
  width: 100%;
  min-height: 58px;
  padding: 0.625rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
  position: relative;
}

.composer-model-routing__option:hover {
  border-color: color-mix(in srgb, var(--accent) 18%, var(--border));
  background: color-mix(in srgb, var(--accent) 3%, var(--bg-surface));
}

.composer-model-routing__option:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.composer-model-routing__option.is-active {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  background: color-mix(in srgb, var(--accent) 5%, var(--bg-surface));
}

.composer-model-routing__option[aria-disabled="true"] {
  cursor: wait;
}

.composer-model-routing__option-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.composer-model-routing__option-title {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  min-width: 0;
}

.composer-model-routing__option-mark {
  --routing-option-mark: var(--text-dim);
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: var(--radius-full);
  background: var(--routing-option-mark);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--routing-option-mark) 28%, transparent);
}

.composer-model-routing__option--squilla_router .composer-model-routing__option-mark {
  --routing-option-mark: color-mix(in srgb, var(--accent) 72%, var(--text-muted));
  width: 18px;
  height: 8px;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}

.composer-model-routing__option--squilla_router .composer-model-routing__option-mark::before,
.composer-model-routing__option--squilla_router .composer-model-routing__option-mark::after {
  content: "";
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  background: var(--routing-option-mark);
}

.composer-model-routing__option--squilla_router .composer-model-routing__option-mark::before {
  left: 0;
  width: 7px;
  height: 7px;
  border-radius: var(--radius-full);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--routing-option-mark) 22%, transparent);
}

.composer-model-routing__option--squilla_router .composer-model-routing__option-mark::after {
  left: 8px;
  width: 10px;
  height: 2px;
  border-radius: var(--radius-full);
  opacity: 0.65;
}

.composer-model-routing__option--llm_ensemble .composer-model-routing__option-mark {
  --routing-option-mark: color-mix(in srgb, var(--accent) 72%, var(--text-muted));
  width: 20px;
  height: 8px;
  background: linear-gradient(
    90deg,
    transparent 2px,
    color-mix(in srgb, var(--routing-option-mark) 45%, transparent) 2px,
    color-mix(in srgb, var(--routing-option-mark) 45%, transparent) 18px,
    transparent 18px
  ) center / 100% 2px no-repeat;
  box-shadow: none;
}

.composer-model-routing__option--llm_ensemble .composer-model-routing__option-mark::before,
.composer-model-routing__option--llm_ensemble .composer-model-routing__option-mark::after {
  content: "";
  position: absolute;
  top: 0;
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
  background: var(--routing-option-mark);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 22%, transparent);
}

.composer-model-routing__option--llm_ensemble .composer-model-routing__option-mark::before {
  left: 0;
}

.composer-model-routing__option--llm_ensemble .composer-model-routing__option-mark::after {
  right: 0;
}

.composer-model-routing__option-label {
  font-size: 0.8125rem;
  font-weight: 700;
}

.composer-model-routing__option-desc {
  color: var(--text-muted);
  font-size: 0.75rem;
  line-height: 1.35;
}

@media (max-width: 520px) {
  .composer-model-routing {
    left: -2.75rem;
    width: calc(100vw - 32px);
  }
}
</style>
