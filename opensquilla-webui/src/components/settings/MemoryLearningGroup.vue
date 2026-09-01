<script setup lang="ts">
import { computed, inject, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import { useMemoryLearningSettings } from '@/composables/setup/useMemoryLearningSettings'
import { APP_SETTINGS_KEY } from '@/modules/appSettings'
import { OBSERVABILITY_KEY } from '@/modules/observability'

// Settings › Advanced "memory & self-learning" pair: dream (the dependency)
// on top, router self-learning below, joined by a dependency elbow whose copy
// states the asymmetric linkage — enabling self-learning pulls dream up,
// disabling dream is respected and merely pauses training. Deliberately the
// same visual weight as the neighbouring Labs rows: this is an advanced
// opt-in, not a promoted feature.

const { t } = useI18n()
const appSettings = inject(APP_SETTINGS_KEY)
if (!appSettings) throw new Error('AppSettings was not provided')
const observability = inject(OBSERVABILITY_KEY)
if (!observability) throw new Error('Observability was not provided')
const ml = useMemoryLearningSettings(appSettings, observability)

onMounted(() => { void ml.load() })

const depState = computed<'off' | 'on' | 'linked' | 'paused'>(() => {
  if (ml.trainingPaused.value) return 'paused'
  if (!ml.selfLearningEnabled.value) return 'off'
  return ml.dreamLinkedOn.value ? 'linked' : 'on'
})

const depText = computed(() => {
  switch (depState.value) {
    case 'paused': return t('setup.memoryLearning.depPaused')
    case 'linked': return t('setup.memoryLearning.depLinked')
    default: return t('setup.memoryLearning.depDefault')
  }
})

const status = computed(() => ml.status.value)

const modelLine = computed(() => {
  const active = status.value?.activeModel
  if (active?.kind === 'learned') {
    return {
      dot: 'learned',
      text: t('setup.memoryLearning.modelLearned', { version: active.version || '' }),
      side: active.promotedAt ? shortDate(active.promotedAt) : '',
    }
  }
  return { dot: 'base', text: t('setup.memoryLearning.modelBaseline'), side: '' }
})

const samplesLine = computed(() => {
  const s = status.value?.samples
  if (!s) return null
  const high = Number(s.highValue ?? 0)
  const need = Math.max(1, Number(s.requiredHighValue ?? 0))
  return {
    text: `${high} / ${need}`,
    pct: Math.max(0, Math.min(100, Math.round((high / need) * 100))),
    total: t('setup.memoryLearning.samplesTotal', { total: Number(s.total ?? 0) }),
  }
})

// Gate reason codes arrive verbatim from the status RPC (a client-i18n
// contract); anything unknown falls back to the raw code.
const GATE_REASON_KEYS = new Set([
  'ready', 'disabled', 'no_data', 'agent_active', 'cooldown',
  'insufficient_data', 'insufficient_class_diversity',
])

const lastActionLine = computed(() => {
  const g = status.value?.gate
  if (!g) return null
  if (ml.trainingPaused.value) {
    return { dot: 'wait', text: t('setup.memoryLearning.gatePausedDream'), side: '' }
  }
  const reason = String(g.reason || '')
  const text = GATE_REASON_KEYS.has(reason)
    ? t(`setup.memoryLearning.gateReason.${reason}`)
    : reason
  return {
    dot: g.wouldTrain ? 'learned' : 'wait',
    text,
    side: g.lastAttemptAt ? shortDate(g.lastAttemptAt) : '',
  }
})

const detachNotice = computed(() => status.value?.lastReceipt?.kind === 'detached')

function shortDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function onDreamChange(on: boolean) { void ml.setDream(on) }
function onSelfLearningChange(on: boolean) { void ml.setSelfLearning(on) }
</script>

<template>
  <div class="ml-pair">
    <div class="ml-pair__head">{{ t('setup.memoryLearning.groupTitle') }}</div>

    <label class="control-row ml-pair__row">
      <div class="control-row__label-block">
        <span class="control-row__label">
          {{ t('setup.memoryLearning.dreamLabel') }}
          <span class="ml-tok">{{ t('setup.memoryLearning.tokenBadge') }}</span>
        </span>
        <span class="control-row__desc">{{ t('setup.memoryLearning.dreamDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          name="memory_dream_enabled"
          :checked="ml.dreamEnabled.value"
          :busy="ml.busy.value"
          :aria-label="t('setup.memoryLearning.dreamLabel')"
          :class="{ 'ml-switch--linked': ml.dreamLinkedOn.value && ml.dreamEnabled.value }"
          @change="onDreamChange"
        />
      </div>
    </label>

    <label class="control-row ml-pair__row ml-pair__row--last">
      <div class="control-row__label-block">
        <span class="control-row__label">
          {{ t('setup.memoryLearning.selfLearningLabel') }}
          <span class="labs-exp">{{ t('setup.advanced.experimental') }}</span>
        </span>
        <span class="control-row__desc">{{ t('setup.memoryLearning.selfLearningDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          name="router_self_learning_enabled"
          :checked="ml.selfLearningEnabled.value"
          :busy="ml.busy.value"
          :aria-label="t('setup.memoryLearning.selfLearningLabel')"
          @change="onSelfLearningChange"
        />
      </div>
    </label>

    <div class="ml-dep" :class="{ 'ml-dep--alert': depState === 'paused' }">
      <span class="ml-dep__elbow" aria-hidden="true"></span>
      <span>{{ depText }}</span>
    </div>

    <div v-if="ml.selfLearningEnabled.value && status" class="ml-status" data-testid="selflearning-status">
      <div class="ml-status__row">
        <span class="ml-status__k">{{ t('setup.memoryLearning.statusModel') }}</span>
        <span class="ml-status__v">
          <span class="ml-dot" :class="`ml-dot--${modelLine.dot}`" aria-hidden="true"></span>
          {{ modelLine.text }}
        </span>
        <span class="ml-status__side">{{ modelLine.side }}</span>
      </div>
      <div v-if="samplesLine" class="ml-status__row">
        <span class="ml-status__k">{{ t('setup.memoryLearning.statusSamples') }}</span>
        <span class="ml-status__v">
          {{ samplesLine.text }}
          <span class="ml-meter" aria-hidden="true"><i :style="{ width: `${samplesLine.pct}%` }"></i></span>
          <span class="ml-status__aside">{{ samplesLine.total }}</span>
        </span>
        <span class="ml-status__side"></span>
      </div>
      <div v-if="lastActionLine" class="ml-status__row">
        <span class="ml-status__k">{{ t('setup.memoryLearning.statusLastAction') }}</span>
        <span class="ml-status__v">
          <span class="ml-dot" :class="`ml-dot--${lastActionLine.dot}`" aria-hidden="true"></span>
          {{ lastActionLine.text }}
        </span>
        <span class="ml-status__side">{{ lastActionLine.side }}</span>
      </div>
      <div v-if="detachNotice" class="ml-status__note">
        <span class="ml-status__note-ico" aria-hidden="true">&#8635;</span>
        <span>{{ t('setup.memoryLearning.detachedNote') }}</span>
      </div>
    </div>

    <div v-if="ml.restartRequired.value" class="ml-dep ml-dep--alert">
      <span class="ml-dep__elbow" aria-hidden="true"></span>
      <span>{{ t('setup.memoryLearning.restartRequired') }}</span>
    </div>
  </div>
</template>

<style scoped>
.ml-pair {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  margin: var(--sp-3) 0;
  overflow: hidden;
}

.ml-pair__head {
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  color: var(--text-dim);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.1em;
  padding: 8px 14px 6px;
  text-transform: uppercase;
}

.ml-pair__row {
  padding-left: 14px;
  padding-right: 14px;
}

.ml-pair__row--last {
  border-bottom: none;
}

.ml-tok {
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-dim);
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.02em;
  margin-left: var(--sp-1);
  padding: 1px 7px;
}

.ml-dep {
  align-items: flex-start;
  color: var(--text-dim);
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  line-height: 1.55;
  padding: 0 14px 12px 14px;
}

.ml-dep__elbow {
  border-bottom: 1.5px solid color-mix(in srgb, var(--warn) 55%, var(--border));
  border-bottom-left-radius: var(--radius-sm);
  border-left: 1.5px solid color-mix(in srgb, var(--warn) 55%, var(--border));
  flex: none;
  height: 16px;
  margin-left: 6px;
  width: 14px;
}

.ml-dep--alert {
  color: var(--warn);
}

.ml-dep--alert .ml-dep__elbow {
  border-color: var(--warn);
}

.ml-status {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: var(--fs-xs);
  margin: 0 14px 14px;
  overflow: hidden;
}

.ml-status__row {
  align-items: baseline;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 55%, transparent);
  display: grid;
  gap: var(--sp-2);
  grid-template-columns: minmax(72px, auto) 1fr auto;
  padding: 8px 14px;
}

.ml-status__row:last-child {
  border-bottom: none;
}

.ml-status__k {
  color: var(--text-dim);
}

.ml-status__v {
  color: var(--text);
}

.ml-status__side,
.ml-status__aside {
  color: var(--text-dim);
  font-size: 11.5px;
}

.ml-dot {
  border-radius: 50%;
  display: inline-block;
  height: 7px;
  margin-right: 7px;
  vertical-align: 1px;
  width: 7px;
}

.ml-dot--base { background: var(--text-dim); }
.ml-dot--learned { background: var(--ok); }
.ml-dot--wait { background: var(--warn); }

.ml-meter {
  background: var(--bg-hover);
  border-radius: var(--radius-xs);
  display: inline-block;
  height: 4px;
  margin: 0 8px;
  position: relative;
  vertical-align: 2px;
  width: 90px;
}

.ml-meter i {
  background: var(--accent);
  border-radius: var(--radius-xs);
  inset: 0 auto 0 0;
  position: absolute;
}

.ml-status__note {
  background: color-mix(in srgb, var(--accent) 5%, transparent);
  border-top: 1px solid color-mix(in srgb, var(--accent) 18%, var(--border));
  color: var(--text-dim);
  display: flex;
  gap: var(--sp-2);
  line-height: 1.55;
  padding: 9px 14px;
}

.ml-status__note-ico {
  color: var(--accent);
  flex: none;
}

/* Linked-on: dream switched on as a side effect of enabling self-learning —
   desaturated track distinguishes it from a user-cast ON until touched.
   (The bare ControlSwitch renders as the <input> itself, so the class binding
   lands directly on it.) */
:deep(input.control-switch.ml-switch--linked:checked) {
  opacity: 0.75;
}

.labs-exp {
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border));
  border-radius: var(--radius-full);
  color: var(--accent);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.03em;
  margin-left: var(--sp-1);
  padding: 1px 6px;
  text-transform: uppercase;
}
</style>
