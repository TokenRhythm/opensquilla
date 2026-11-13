<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import type { DiscoveredModel } from '@/composables/setup/useSetupProviderForm'
import type { EnsembleCandidateView, EnsembleCustomLineupView, EnsembleFixedProfileView } from '@/composables/setup/useSetupEnsembleForm'
import { useJevSettings } from '@/composables/settings/useJevSettings'

const props = defineProps<{
  ensemble?: { scheme: string; fixedProfile: EnsembleFixedProfileView | null; custom: EnsembleCustomLineupView }
  ensembleSaved?: boolean
}>()
const emit = defineEmits<{ 'dirty-change': [value: boolean]; 'busy-change': [value: boolean]; 'open-model-strategy': [] }>()
const { t } = useI18n()
const { draft, loaded, loading, busy, dirty, errorKey, messageKey, unsupported, load, save, discard } = useJevSettings()
const delays = computed(() => [...new Set([5, 10, 15, 30, 60, draft.value.auto_switch_seconds])].sort((a, b) => a - b))
const ratios = computed(() => [...new Set([0.5, 0.65, 0.8, 0.9, 0.95, draft.value.context_restart_ratio])].sort((a, b) => a - b))
const configuredPlan = computed(() => {
  const plan = props.ensemble
  if (!plan) return null
  const proposers = (plan.scheme === 'preset' ? plan.fixedProfile?.proposers : plan.custom.proposers)?.filter(row => row.enabled) || []
  const aggregator = plan.scheme === 'preset' ? plan.fixedProfile?.aggregator
    : plan.custom.aggregator || (plan.custom.inheritedAggregatorModel ? {
      provider: plan.custom.inheritedAggregatorProvider, model: plan.custom.inheritedAggregatorModel,
    } : null)
  return proposers.length >= 2 && aggregator ? { proposers, aggregator } : null
})
const planStatus = computed(() => !configuredPlan.value ? 'settings.jev.ensembleNotConfigured'
  : !props.ensembleSaved ? 'settings.jev.ensembleUnsaved'
    : configuredPlan.value.proposers.some((row: EnsembleCandidateView) => row.credential?.available === false)
      || ('credential' in configuredPlan.value.aggregator && configuredPlan.value.aggregator.credential?.available === false)
      ? 'settings.jev.ensembleUnavailable' : 'settings.jev.ensembleConfigured')
const nativeModels: DiscoveredModel[] = [{
  id: 'NeoHorse-Jev-4B', name: 'NeoHorse-Jev-4B', contextWindow: null,
  maxOutputTokens: null, capabilities: [], pricing: null, capabilitySource: 'provider',
}]
watch(dirty, value => emit('dirty-change', value), { immediate: true })
watch(busy, value => emit('busy-change', value), { immediate: true })
onMounted(() => { void load() })
defineExpose({ save, discard })
</script>

<template>
  <section class="danbao-settings" data-testid="jev-settings">
    <header class="danbao-settings__hero">
      <span class="danbao-settings__mascot" aria-hidden="true">🐣</span>
      <div><h3>{{ t('settings.jev.title') }}</h3><p>{{ t('settings.jev.description') }}</p></div>
    </header>
    <p class="danbao-settings__scope">{{ t('settings.jev.globalScope') }}</p>
    <div v-if="loading" class="danbao-settings__loading" role="status"><LoadingSpinner />{{ t('shared.loading') }}</div>
    <div v-if="errorKey" class="danbao-settings__error" role="alert">
      <p>{{ t(errorKey) }}</p>
      <button v-if="!busy && !unsupported" type="button" class="btn" @click="load">{{ t('settings.jev.reload') }}</button>
    </div>
    <template v-if="loaded">
      <div class="danbao-settings__card">
        <div class="danbao-settings__row">
          <div><label for="settings-jev-enabled">{{ t('chat.jev.enable') }}</label><p>{{ t('settings.jev.enableHint') }}</p></div>
          <input id="settings-jev-enabled" v-model="draft.enabled" name="jev_enabled" type="checkbox" role="switch" :disabled="busy">
        </div>
      </div>
      <fieldset class="danbao-settings__card" :disabled="busy || !draft.enabled">
        <legend>{{ t('chat.jev.mode') }}</legend>
        <div class="danbao-settings__modes" role="group" :aria-label="t('chat.jev.mode')">
          <button v-for="mode in (['suggest', 'trust'] as const)" :key="mode" type="button" :aria-pressed="draft.mode === mode" @click="draft.mode = mode">
            {{ t(`chat.jev.${mode}`) }}
          </button>
        </div>
        <p class="danbao-settings__mode-description">{{ t(draft.mode === 'trust' ? 'settings.jev.trustDescription' : 'settings.jev.suggestDescription') }}</p>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-wait">{{ t('settings.jev.waitLabel') }}</label><p>{{ t(draft.mode === 'trust' ? 'settings.jev.trustWaitHint' : 'settings.jev.suggestWaitHint') }}</p></div>
          <select id="settings-jev-wait" v-model.number="draft.auto_switch_seconds" name="jev_wait">
            <option v-for="seconds in delays" :key="seconds" :value="seconds">{{ t('chat.jev.autoSwitchDelayValue', { seconds }) }}</option>
          </select>
        </div>
      </fieldset>
      <fieldset class="danbao-settings__card" :disabled="busy || !draft.enabled">
        <legend>{{ t('settings.jev.assessmentTitle') }}</legend>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-interval">{{ t('chat.jev.loopInterval') }}</label><p>{{ t('settings.jev.intervalHint') }}</p></div>
          <select id="settings-jev-interval" v-model.number="draft.loop_interval" name="jev_interval">
            <option v-for="interval in [3, 5]" :key="interval" :value="interval">{{ t('chat.jev.loopIntervalValue', { count: interval }) }}</option>
          </select>
        </div>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-budget">{{ t('chat.jev.budget') }}</label><p>{{ t('settings.jev.budgetHint') }}</p></div>
          <select id="settings-jev-budget" v-model.number="draft.max_prompts_per_hour" name="jev_budget">
            <option v-for="budget in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]" :key="budget" :value="budget">{{ t('chat.jev.budgetValue', { count: budget }) }}</option>
          </select>
        </div>
      </fieldset>
      <fieldset class="danbao-settings__card" :disabled="busy || !draft.enabled">
        <legend>{{ t('settings.jev.ensembleTitle') }}</legend>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-ensemble">{{ t('settings.jev.ensembleEnable') }}</label><p>{{ t('settings.jev.ensembleHint') }}</p></div>
          <input id="settings-jev-ensemble" v-model="draft.ensemble_enabled" name="jev_ensemble" type="checkbox" role="switch">
        </div>
        <div class="danbao-settings__plan">
          <p role="status">{{ t(planStatus) }}</p>
          <template v-if="configuredPlan">
            <p>{{ t('chat.jev.ensembleProposers') }} {{ configuredPlan.proposers.map(row => `${row.model} · ${row.provider}`).join(' / ') }}</p>
            <p>{{ t('chat.jev.ensembleAggregator') }} {{ configuredPlan.aggregator.model }} · {{ configuredPlan.aggregator.provider }}</p>
          </template>
          <button type="button" class="danbao-settings__link" @click="emit('open-model-strategy')">{{ t('settings.jev.configureEnsemble') }}</button>
        </div>
      </fieldset>
      <fieldset class="danbao-settings__card" :disabled="busy || !draft.enabled">
        <legend>{{ t('settings.jev.sessionTitle') }}</legend>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-session">{{ t('settings.jev.sessionEnable') }}</label><p id="settings-jev-session-rule">{{ t('settings.jev.sessionHint') }}</p></div>
          <input id="settings-jev-session" v-model="draft.session_restart_enabled" name="jev_session" type="checkbox" role="switch">
        </div>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-context-turns">{{ t('settings.jev.contextTurns') }}</label><p id="settings-jev-context-turns-hint">{{ t('settings.jev.contextTurnsHint') }}</p></div>
          <input id="settings-jev-context-turns" v-model.number="draft.context_restart_min_turns" class="danbao-settings__number" name="jev_context_turns" aria-describedby="settings-jev-session-rule settings-jev-context-turns-hint" type="number" min="2" max="1000" step="1" :disabled="!draft.session_restart_enabled">
        </div>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-context-ratio">{{ t('settings.jev.contextRatio') }}</label><p id="settings-jev-context-threshold-hint">{{ t('settings.jev.contextThresholdHint') }}</p></div>
          <select id="settings-jev-context-ratio" v-model.number="draft.context_restart_ratio" name="jev_context_ratio" aria-describedby="settings-jev-session-rule settings-jev-context-threshold-hint" :disabled="!draft.session_restart_enabled">
            <option v-for="ratio in ratios" :key="ratio" :value="ratio">{{ Math.round(ratio * 100) }}%</option>
          </select>
        </div>
        <div class="danbao-settings__row">
          <div><label for="settings-jev-context-tokens">{{ t('settings.jev.contextTokens') }}</label></div>
          <input id="settings-jev-context-tokens" v-model.number="draft.context_restart_min_tokens" class="danbao-settings__number" name="jev_context_tokens" aria-describedby="settings-jev-session-rule settings-jev-context-threshold-hint" type="number" min="1024" max="2000000" step="1" :disabled="!draft.session_restart_enabled">
        </div>
      </fieldset>
      <section class="danbao-settings__card">
        <h4>{{ t('chat.jev.decisionModelLabel') }} <span class="danbao-settings__provider">TokenRhythm</span></h4>
        <SetupModelCombobox
          :field="{ name: 'jev-settings-model', label: t('chat.jev.decisionModelLabel'), placeholder: t('chat.jev.selectModel') }"
          value="NeoHorse-Jev-4B" :models="nativeModels" model-source="catalog"
          :disabled="busy || !draft.enabled" cell commit-on-select :allow-custom-value="false"
        />
        <p class="danbao-settings__note">{{ t('chat.jev.nativeModelHint') }}</p>
      </section>
      <p class="danbao-settings__note">{{ t('settings.jev.dataHint') }}</p>
      <p v-if="messageKey && !dirty" class="danbao-settings__saved" role="status">{{ t(messageKey) }}</p>
    </template>
  </section>
</template>

<style scoped>
.danbao-settings { display: flex; flex-direction: column; gap: var(--sp-4); min-width: 0; }
.danbao-settings[hidden] { display: none; }
.danbao-settings__hero { display: flex; align-items: center; gap: var(--sp-4); padding: var(--sp-5); border: 1px solid color-mix(in srgb, var(--accent) 25%, var(--border)); border-radius: var(--radius-card); background: color-mix(in srgb, var(--accent) 8%, var(--bg-surface)); }
.danbao-settings__mascot { display: grid; place-items: center; flex: 0 0 64px; height: 64px; border-radius: var(--radius-pill); background: color-mix(in srgb, var(--accent) 14%, var(--bg-surface)); font-size: 40px; }
.danbao-settings h3 { margin: 0 0 var(--sp-1); font-size: var(--fs-lg); }
.danbao-settings h4 { margin: 0 0 var(--sp-3); font-size: var(--fs-sm); font-weight: 600; }
.danbao-settings__hero p, .danbao-settings__scope, .danbao-settings__note { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.65; }
.danbao-settings__scope { font-size: var(--fs-xs); }
.danbao-settings__card { min-width: 0; margin: 0; padding: var(--sp-4); border: 1px solid var(--border); border-radius: var(--radius-card); background: var(--bg-surface); }
.danbao-settings__card legend { padding: 0 var(--sp-2); font-size: var(--fs-sm); font-weight: 600; color: var(--text); }
.danbao-settings__card:disabled { opacity: 0.65; }
.danbao-settings__row { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4); min-width: 0; }
.danbao-settings__row + .danbao-settings__row { margin-top: var(--sp-4); padding-top: var(--sp-4); border-top: 1px solid var(--hairline); }
.danbao-settings__row > div { flex: 1; min-width: 0; }
.danbao-settings__row label { font-size: var(--fs-sm); font-weight: 500; }
.danbao-settings__row p { margin: var(--sp-1) 0 0; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.6; }
.danbao-settings__row select { flex: 0 0 auto; max-width: 48%; min-height: 38px; padding: var(--sp-2); background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-control); font: inherit; font-size: var(--fs-xs); color: var(--text); }
.danbao-settings__row input { accent-color: var(--accent); width: 18px; height: 18px; flex-shrink: 0; }
.danbao-settings__plan { margin-top: var(--sp-3); padding-top: var(--sp-3); border-top: 1px solid var(--hairline); color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.6; overflow-wrap: anywhere; }
.danbao-settings__plan p { margin: var(--sp-1) 0; }
.danbao-settings__link { margin-top: var(--sp-2); border: 0; padding: 0; background: transparent; color: var(--accent); font: inherit; cursor: pointer; text-decoration: underline; }
.danbao-settings__row .danbao-settings__number { width: 125px; max-width: 48%; height: 38px; box-sizing: border-box; padding: var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-elevated); color: var(--text); font: inherit; font-size: var(--fs-xs); }
.danbao-settings__modes { display: flex; gap: var(--sp-1); width: fit-content; padding: var(--sp-1); background: var(--bg-elevated); border-radius: var(--radius-control); }
.danbao-settings__modes button { min-width: 80px; padding: var(--sp-2) var(--sp-4); border: 1px solid transparent; border-radius: var(--radius-control); background: transparent; color: var(--text-muted); font: inherit; font-size: var(--fs-sm); cursor: pointer; }
.danbao-settings__modes button[aria-pressed="true"] { border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); background: color-mix(in srgb, var(--accent) 13%, var(--bg-surface)); color: var(--accent); }
.danbao-settings__modes button:disabled { cursor: default; }
.danbao-settings__mode-description { margin: var(--sp-3) 0 var(--sp-4); font-size: var(--fs-sm); line-height: 1.7; color: var(--text-muted); }
.danbao-settings__provider { margin-left: var(--sp-2); font-weight: 400; color: var(--text-muted); font-size: var(--fs-xs); }
.danbao-settings__note { margin-top: var(--sp-2); font-size: var(--fs-xs); }
.danbao-settings__error { color: var(--danger); font-size: var(--fs-sm); }
.danbao-settings__error p { margin: 0 0 var(--sp-2); }
.danbao-settings__saved { margin: 0; color: var(--ok); font-size: var(--fs-sm); }
.danbao-settings__loading { display: flex; align-items: center; gap: var(--sp-2); color: var(--text-muted); }
.danbao-settings button:focus-visible, .danbao-settings input:focus-visible, .danbao-settings select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (max-width: 560px) { .danbao-settings__hero { padding: var(--sp-4); } .danbao-settings__mascot { flex-basis: 48px; height: 48px; font-size: 32px; } .danbao-settings__row { align-items: flex-start; gap: var(--sp-3); } }
</style>
