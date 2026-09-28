<script setup lang="ts">
import { computed, inject, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { MODEL_CAPACITY_KEY, capacityKey, parseCapacity, type CapacityValues } from '@/composables/setup/useModelCapacityForm'
import SetupThinkingSelect from './SetupThinkingSelect.vue'
import SetupModelCapacityFields from './SetupModelCapacityFields.vue'

const props = defineProps<{ provider: string; model: string; scope?: string; inline?: boolean; menu?: boolean; disabled?: boolean; initialOpen?: boolean; hideTrigger?: boolean; thinking?: string; thinkingScope?: 'global' | 'tier' | 'proposer' | 'aggregator' | 'inheritedAggregator' | 'preset'; thinkingReadonly?: boolean }>()
const emit = defineEmits<{ updateThinking: [value: string] }>()
const { t } = useI18n()
const form = inject(MODEL_CAPACITY_KEY, null)
const id = useId()
const target = computed(() => ({ provider: props.provider, model: props.model }))
const key = computed(() => capacityKey(target.value))
const row = computed(() => form?.rows.get(key.value))
const scope = computed(() => props.scope || 'modelStrategy')
const open = ref(false)
const root = ref<HTMLElement | null>(null)
const localThinking = ref('')
const thinkingLabel = computed(() => props.thinkingScope === 'global' ? t('setup.thinking.globalLabel') : t('setup.provider.thinkingLabel'))
const local = ref<CapacityValues>({ contextWindow: '', maxOutputTokens: '' })
const values = computed(() => props.inline ? form?.values(target.value) || local.value : local.value)
const valid = computed(() => {
  try { Object.values(values.value).forEach(parseCapacity); return true } catch { return false }
})
const pending = computed(() => form?.pending.has(key.value))
const failed = computed(() => form?.failed.has(key.value))
// Thinking is independent of capacity metadata. Missing metadata must not
// block a thinking-only edit or create a capacity override from empty fields.
const canComplete = computed(() => !props.disabled && valid.value && (
  row.value ? !pending.value : props.thinking !== undefined && !props.thinkingReadonly
))
function close() { if (!props.disabled) open.value = false }
useDialogA11y(root, open, close)
watch([target, () => form?.supported.value, () => form?.rows.size, () => form?.generation.value], () => {
  form?.ensure(target.value)
}, { immediate: true })
watch(key, () => { open.value = false })
function show() {
  form?.ensure(target.value)
  local.value = form?.values(target.value) || { contextWindow: '', maxOutputTokens: '' }
  localThinking.value = props.thinking ?? ''
  open.value = true
}
watch(() => props.initialOpen, value => { if (value) show() }, { immediate: true })
watch(row, (next, previous) => { if (next && !previous && open.value) local.value = form!.values(target.value) })
function change(next: CapacityValues) {
  if (props.inline) form?.update(target.value, next, scope.value)
  else local.value = next
}
function complete() {
  if (!canComplete.value) return
  form?.update(target.value, local.value, scope.value)
  if (props.thinking !== undefined && !props.thinkingReadonly && localThinking.value !== props.thinking) emit('updateThinking', localThinking.value)
  open.value = false
}
</script>

<template>
  <template v-if="form && provider && model">
    <details v-if="inline" class="model-capacity-disclosure">
      <summary class="control-row control-row--divider">
        {{ t('setup.capacity.title') }}
      </summary>
      <p v-if="!form.supported.value" class="control-section__desc">{{ t('setup.capacity.upgrade') }}</p>
      <SetupModelCapacityFields v-else-if="row" :row="row" :model-value="values" :disabled="disabled" :hide-identity="thinking !== undefined && !inline" @update:model-value="change" />
      <p v-else class="control-section__desc" role="status">{{ t(failed ? 'setup.capacity.loadFailed' : 'shared.loading') }}</p>
      <button v-if="failed" type="button" class="btn btn--ghost" @click="form.ensure(target)">{{ t('setup.capacity.retry') }}</button>
    </details>
    <template v-else>
      <button v-if="!hideTrigger"
        type="button" :class="menu ? 'model-capacity-menu' : 'btn btn--icon btn--ghost model-capacity-trigger'"
        :title="t('setup.capacity.title')" :aria-label="t('setup.capacity.forModel', { model })"
        :disabled="disabled" @click="show"
      ><Icon name="gear" :size="14" /><span v-if="menu">{{ t('setup.capacity.title') }}</span></button>
      <Teleport to="body">
        <div v-if="open" class="model-capacity-overlay" @click.self="close">
          <section ref="root" class="model-capacity-dialog" role="dialog" aria-modal="true" :aria-labelledby="id">
            <header class="model-capacity-dialog__head">
              <h4 :id="id">{{ t('setup.capacity.title') }}</h4>
              <button type="button" class="btn btn--icon btn--ghost" :aria-label="t('common.close')" :disabled="disabled" @click="close"><Icon name="x" :size="16" /></button>
            </header>
            <div class="model-capacity-dialog__body">
              <p v-if="thinking !== undefined">{{ provider }} · {{ model }}</p>
              <section v-if="thinking !== undefined" class="model-capacity-thinking">
                <label class="control-row">
                  <span class="control-row__label-block">
                    <span class="control-row__label">{{ thinkingLabel }}</span>
                    <span class="control-row__desc">{{ t(`setup.thinking.${thinkingScope || 'global'}Desc`) }}</span>
                  </span>
                  <span v-if="thinkingReadonly">{{ thinking || t('setup.provider.thinkingDefault') }}</span>
                  <SetupThinkingSelect v-else v-model="localThinking" :label="thinkingLabel" :disabled="disabled" data-testid="model-thinking-level" />
                </label>
              </section>
              <h5 v-if="thinking !== undefined">{{ t('setup.thinking.sharedSettings') }}</h5>
              <p v-if="!form.supported.value" class="control-section__desc">{{ t('setup.capacity.upgrade') }}</p>
              <SetupModelCapacityFields v-else-if="row" :row="row" :model-value="values" :disabled="disabled" :hide-identity="thinking !== undefined && !inline" @update:model-value="change" />
              <p v-else role="status">{{ t(failed ? 'setup.capacity.loadFailed' : 'shared.loading') }}</p>
              <button v-if="failed" type="button" class="btn" @click="form.ensure(target)">{{ t('setup.capacity.retry') }}</button>
            </div>
            <footer class="model-capacity-dialog__footer">
              <span class="control-row__desc">{{ t('setup.capacity.draftHint') }}</span>
              <button type="button" class="btn" :disabled="disabled" @click="close">{{ t('common.cancel') }}</button>
              <button type="button" class="btn btn--primary" :disabled="!canComplete" @click="complete">{{ t('setup.capacity.done') }}</button>
            </footer>
          </section>
        </div>
      </Teleport>
    </template>
  </template>
</template>

<style scoped>
.model-capacity-thinking :deep(select) { width: 160px; min-width: 160px; }
.model-capacity-thinking .control-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: var(--sp-3); }
@media (max-width: 480px) { .model-capacity-thinking .control-row { grid-template-columns: minmax(0, 1fr); } .model-capacity-thinking :deep(select) { width: 100%; min-width: 0; } }
.model-capacity-thinking { padding-bottom: var(--sp-3); border-bottom: 1px solid var(--border); }
.model-capacity-dialog__body h5 { margin: var(--sp-3) 0; font-size: var(--fs-sm); color: var(--text-muted); }
.model-capacity-disclosure { min-width: 0; width: 100%; }
.model-capacity-trigger { flex-shrink: 0; }
.model-capacity-menu { display: flex; align-items: center; gap: var(--sp-2); border: 0; background: transparent; color: var(--text); padding: var(--sp-2) var(--sp-3); font: inherit; cursor: pointer; text-align: start; width: 100%; }
.model-capacity-overlay { position: fixed; inset: 0; z-index: 460; display: flex; align-items: center; justify-content: center; padding: var(--sp-4); background: color-mix(in srgb, var(--scrim) 88%, transparent); }
.model-capacity-dialog { width: min(520px, 100%); max-height: calc(100dvh - 32px); display: flex; flex-direction: column; overflow: hidden; background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-modal); box-shadow: var(--shadow-xl); }
.model-capacity-dialog__head, .model-capacity-dialog__footer { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-4); }
.model-capacity-dialog__head { justify-content: space-between; border-bottom: 1px solid var(--border); }
.model-capacity-dialog__head h4 { margin: 0; font-size: var(--fs-md); }
.model-capacity-dialog__body { overflow-y: auto; padding: var(--sp-4); }
.model-capacity-dialog__footer { border-top: 1px solid var(--border); flex-wrap: wrap; justify-content: flex-end; }
.model-capacity-dialog__footer > span { flex: 1 1 140px; }
@media (max-width: 640px) { .model-capacity-overlay { align-items: flex-end; } }
</style>
