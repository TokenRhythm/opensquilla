<template>
  <section class="sandbox-settings" aria-labelledby="sandbox-settings-title">
    <header class="sandbox-settings__header">
      <div>
        <h3 id="sandbox-settings-title">{{ t('settings.sandbox.title') }}</h3>
        <p>{{ t('settings.sandbox.subtitle') }}</p>
      </div>
      <span
        v-if="capability || capabilityLoading || capabilityCheckFailed"
        class="sandbox-settings__status"
        :class="{ 'is-ready': capability?.available }"
      >
        {{ capabilityLoading
          ? t('shared.loading')
          : capability?.available
            ? t('settings.sandbox.available')
            : t('settings.sandbox.unavailable') }}
      </span>
    </header>

    <div v-if="loading" class="sandbox-settings__state" role="status">
      {{ t('shared.loading') }}
    </div>
    <div v-else-if="loadError" class="sandbox-settings__state" role="alert">
      <span>{{ loadError }}</span>
      <button type="button" class="btn" @click="load">{{ t('settings.sandbox.actions.retry') }}</button>
    </div>

    <template v-else-if="draft">
      <article v-if="activeView === 'overview'" class="sandbox-overview" data-testid="sandbox-overview">
        <section class="sandbox-mode-picker" aria-labelledby="sandbox-mode-title">
          <div>
            <h4 id="sandbox-mode-title">{{ t('settings.sandbox.mode.title') }}</h4>
            <p>{{ t('settings.sandbox.mode.description') }}</p>
          </div>
          <div class="sandbox-segmented" data-testid="sandbox-default-mode">
            <button
              type="button"
              :class="{ 'is-selected': defaultRunMode === 'safe' }"
              :disabled="(!capability?.available && !canRequestSandboxSetup) || sandboxSetupPending"
              data-testid="sandbox-safe-mode"
              @click="selectSafeMode"
            >
              {{ t('settings.sandbox.mode.safe') }}
            </button>
            <button
              type="button"
              :class="{ 'is-selected': defaultRunMode === 'full' }"
              data-testid="sandbox-full-mode"
              @click="void selectFullMode()"
            >
              {{ t('settings.sandbox.mode.full') }}
            </button>
          </div>
        </section>
        <p
          v-if="sandboxSetupOutcomeMessage"
          class="sandbox-setup-result"
          data-testid="sandbox-setup-result"
          role="status"
        >
          {{ sandboxSetupOutcomeMessage }}
        </p>

        <nav class="sandbox-list" :aria-label="t('settings.sandbox.title')">
          <button type="button" class="sandbox-list__row" data-testid="sandbox-open-files" @click="activeView = 'files'">
            <span class="sandbox-list__icon" aria-hidden="true"><FileIcon /></span>
            <span><strong>{{ t('settings.sandbox.files.title') }}</strong><small>{{ fileSummary }}</small></span>
            <span class="sandbox-list__chevron" aria-hidden="true">›</span>
          </button>
          <button type="button" class="sandbox-list__row" data-testid="sandbox-open-commands" @click="activeView = 'commands'">
            <span class="sandbox-list__icon" aria-hidden="true"><CommandIcon /></span>
            <span><strong>{{ t('settings.sandbox.commands.title') }}</strong><small>{{ commandSummary }}</small></span>
            <span class="sandbox-list__chevron" aria-hidden="true">›</span>
          </button>
          <button type="button" class="sandbox-list__row" data-testid="sandbox-open-network" @click="activeView = 'network'">
            <span class="sandbox-list__icon" aria-hidden="true"><NetworkIcon /></span>
            <span><strong>{{ t('settings.sandbox.network.title') }}</strong><small>{{ networkSummary }}</small></span>
            <span class="sandbox-list__chevron" aria-hidden="true">›</span>
          </button>
          <button type="button" class="sandbox-list__row" data-testid="sandbox-open-runtimes" @click="activeView = 'runtimes'">
            <span class="sandbox-list__icon" aria-hidden="true"><RuntimeIcon /></span>
            <span><strong>{{ t('settings.sandbox.runtimes.title') }}</strong><small>{{ runtimeSummary }}</small></span>
            <span class="sandbox-list__chevron" aria-hidden="true">›</span>
          </button>
        </nav>

      </article>

      <header v-else class="sandbox-detail-header" data-testid="sandbox-detail">
        <button type="button" class="sandbox-back" data-testid="sandbox-detail-back" @click="activeView = 'overview'">
          <span aria-hidden="true">‹</span>{{ t('settings.sandbox.back') }}
        </button>
        <div>
          <h4>{{ activeViewTitle }}</h4>
          <p>{{ activeViewDescription }}</p>
        </div>
        <span v-if="activeView === 'files'" class="sandbox-card__tag sandbox-detail-control">
          {{ t('settings.sandbox.files.readsAllowed') }}
        </span>
        <label v-else-if="activeView === 'runtimes'" class="sandbox-switch sandbox-detail-control">
          <input v-model="draft.runtimes.enabled" type="checkbox" @change="void flushSectionSave('runtimes')" />
          <span aria-hidden="true"></span>
        </label>
      </header>

      <article v-if="activeView === 'files'" class="sandbox-card">
        <div class="sandbox-rule-list" data-testid="builtin-file-rules">
          <div v-for="path in builtinDenyWritePaths" :key="path" class="sandbox-rule">
            <code>{{ path }}</code>
            <span>{{ t('settings.sandbox.builtin') }}</span>
          </div>
          <div
            v-for="(_path, index) in draft.files.customDenyWritePaths"
            :key="`custom-${index}`"
            class="sandbox-rule sandbox-rule--editable"
          >
            <input
              v-model="draft.files.customDenyWritePaths[index]"
              :aria-label="t('settings.sandbox.files.customPath')"
              @input="scheduleSectionSave('files')"
              @blur="void flushSectionSave('files')"
            />
            <button type="button" class="btn btn--ghost" @click="removeAt(draft.files.customDenyWritePaths, index, 'files')">
              {{ t('settings.sandbox.actions.remove') }}
            </button>
          </div>
        </div>
        <div class="sandbox-inline-form">
          <input
            v-model="newFilePath"
            :placeholder="t('settings.sandbox.files.pathPlaceholder')"
            @keydown.enter.prevent="addTextRule(draft.files.customDenyWritePaths, newFilePath, value => { newFilePath = value }, 'files')"
          />
          <button
            type="button"
            class="btn"
            @click="addTextRule(draft.files.customDenyWritePaths, newFilePath, value => { newFilePath = value }, 'files')"
          >
            {{ t('settings.sandbox.actions.add') }}
          </button>
        </div>

        <div class="sandbox-option">
          <div>
            <strong>{{ t('settings.sandbox.files.backupTitle') }}</strong>
            <p>{{ t('settings.sandbox.files.backupDescription') }}</p>
          </div>
          <label class="sandbox-switch">
            <input v-model="draft.files.recursiveDeleteBackupEnabled" type="checkbox" @change="void flushSectionSave('files')" />
            <span aria-hidden="true"></span>
          </label>
        </div>
        <label class="sandbox-field sandbox-field--compact">
          <span>{{ t('settings.sandbox.files.quota') }}</span>
          <input
            v-model.number="backupQuotaGiB"
            data-testid="sandbox-backup-quota"
            type="number"
            min="0.1"
            step="0.5"
            @input="scheduleSectionSave('files')"
            @blur="void flushSectionSave('files')"
          />
          <span>GiB</span>
        </label>
        <p class="sandbox-warning">{{ t('settings.sandbox.files.recursiveWarning') }}</p>
      </article>

      <article v-if="activeView === 'commands'" class="sandbox-card">
        <label class="sandbox-field">
          <span>{{ t('settings.sandbox.commands.systemTools') }}</span>
          <select v-model="draft.commands.systemTools" @change="void flushSectionSave('commands')">
            <option value="auto">{{ t('settings.sandbox.commands.systemToolsAuto') }}</option>
            <option value="prompt">{{ t('settings.sandbox.commands.systemToolsPrompt') }}</option>
            <option value="disabled">{{ t('settings.sandbox.commands.systemToolsDisabled') }}</option>
          </select>
        </label>

        <RuleEditor
          v-model="approvalPrefix"
          :title="t('settings.sandbox.commands.approvalPrefixes')"
          :placeholder="t('settings.sandbox.commands.prefixPlaceholder')"
          :rules="draft.commands.requireApprovalPrefixes"
          @add="addPrefix(draft.commands.requireApprovalPrefixes, approvalPrefix, value => { approvalPrefix = value }, 'commands')"
          @remove="removeAt(draft.commands.requireApprovalPrefixes, $event, 'commands')"
        />
        <RuleEditor
          v-model="autoPrefix"
          :title="t('settings.sandbox.commands.autoPrefixes')"
          :placeholder="t('settings.sandbox.commands.prefixPlaceholder')"
          :rules="draft.commands.autoAllowPrefixes"
          @add="addPrefix(draft.commands.autoAllowPrefixes, autoPrefix, value => { autoPrefix = value }, 'commands')"
          @remove="removeAt(draft.commands.autoAllowPrefixes, $event, 'commands')"
        />
      </article>

      <article v-if="activeView === 'network'" class="sandbox-card">
        <div class="sandbox-option">
          <div>
            <strong>{{ t('settings.sandbox.network.blockAll') }}</strong>
            <p>{{ t('settings.sandbox.network.blockAllDescription') }}</p>
          </div>
          <label class="sandbox-switch">
            <input v-model="draft.network.blockAllNetwork" type="checkbox" @change="void flushSectionSave('network')" />
            <span aria-hidden="true"></span>
          </label>
        </div>
        <TextRuleEditor
          v-model="allowDomain"
          :title="t('settings.sandbox.network.allowDomains')"
          placeholder="api.example.com"
          :rules="draft.network.allowDomains"
          @add="addTextRule(draft.network.allowDomains, allowDomain, value => { allowDomain = value }, 'network')"
          @remove="removeAt(draft.network.allowDomains, $event, 'network')"
        />
        <TextRuleEditor
          v-model="denyDomain"
          :title="t('settings.sandbox.network.denyDomains')"
          placeholder="telemetry.example.com"
          :rules="draft.network.denyDomains"
          @add="addTextRule(draft.network.denyDomains, denyDomain, value => { denyDomain = value }, 'network')"
          @remove="removeAt(draft.network.denyDomains, $event, 'network')"
        />
      </article>

      <article v-if="activeView === 'runtimes'" class="sandbox-card">
        <div class="sandbox-runtime-grid">
          <label><span>Python <small>{{ runtimeVersions.python?.version ?? '—' }}</small></span><input v-model="draft.runtimes.python" type="checkbox" :disabled="!draft.runtimes.enabled" @change="void flushSectionSave('runtimes')" /></label>
          <label><span>Node.js <small>{{ runtimeVersions.node?.version ?? '—' }}</small></span><input v-model="draft.runtimes.node" type="checkbox" :disabled="!draft.runtimes.enabled" @change="void flushSectionSave('runtimes')" /></label>
          <label><span>Git Bash <small>{{ runtimeVersions.gitBash?.version ?? '—' }}</small></span><input v-model="draft.runtimes.gitBash" type="checkbox" :disabled="!draft.runtimes.enabled || !runtimeVersions.gitBash" @change="void flushSectionSave('runtimes')" /></label>
        </div>
        <p v-if="runtimeTarget" class="sandbox-detail">{{ t('settings.sandbox.runtimes.target') }}: <code>{{ runtimeTarget }}</code></p>
      </article>

    </template>

    <SandboxSetupDialog
      :open="sandboxSetupConfirmOpen"
      :pending="sandboxSetupPending"
      :outcome="sandboxSetupOutcome"
      @cancel="cancelSandboxSetup"
      @background="runSandboxSetupInBackground"
      @confirm="void continueSandboxSetup()"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { storeToRefs } from 'pinia'

import SandboxSetupDialog from '@/components/sandbox/SandboxSetupDialog.vue'
import {
  useSandboxSettings,
  type SandboxPolicySection,
} from '@/composables/settings/useSandboxSettings'
import { useSandboxSetupStore } from '@/stores/sandboxSetup'

const { t } = useI18n()
const {
  loading,
  capabilityLoading,
  capabilityCheckFailed,
  loadError,
  capability,
  canRequestSandboxSetup,
  draft,
  builtinDenyWritePaths,
  runtimeTarget,
  runtimeVersions,
  defaultRunMode,
  load,
  setDefaultRunMode,
  adoptSavedDefaultRunMode,
  scheduleSectionSave,
  flushSectionSave,
} = useSandboxSettings()
const sandboxSetupStore = useSandboxSetupStore()
const {
  ensuring: sandboxSetupPending,
  outcome: sandboxSetupOutcome,
  intendedMode: sandboxSetupIntendedMode,
} = storeToRefs(sandboxSetupStore)

const newFilePath = ref('')
const approvalPrefix = ref('')
const autoPrefix = ref('')
const allowDomain = ref('')
const denyDomain = ref('')
type SandboxView = 'overview' | 'files' | 'commands' | 'network' | 'runtimes'
const activeView = ref<SandboxView>('overview')
const sandboxSetupConfirmOpen = ref(false)

const sandboxSetupOutcomeMessage = computed(() => {
  if (sandboxSetupOutcome.value === 'cancelled') return t('settings.sandbox.setup.cancelled')
  if (sandboxSetupOutcome.value === 'failed') return t('settings.sandbox.setup.failed')
  if (sandboxSetupOutcome.value === 'verification_failed') {
    return t('settings.sandbox.setup.verificationFailed')
  }
  return ''
})

function selectSafeMode(): void {
  sandboxSetupStore.resetOutcome()
  sandboxSetupStore.noteRunModeSelection('safe')
  if (capability.value?.available) {
    void setDefaultRunMode('safe')
    return
  }
  if (canRequestSandboxSetup.value) sandboxSetupConfirmOpen.value = true
}

function selectFullMode(): Promise<boolean> {
  sandboxSetupStore.noteRunModeSelection('full')
  return setDefaultRunMode('full')
}

function cancelSandboxSetup(): void {
  if (sandboxSetupPending.value) return
  sandboxSetupConfirmOpen.value = false
}

async function continueSandboxSetup(): Promise<void> {
  if (sandboxSetupPending.value) return
  const ready = await sandboxSetupStore.startSafeSetup()
  if (ready) {
    sandboxSetupConfirmOpen.value = false
    if (sandboxSetupIntendedMode.value === 'safe') adoptSavedDefaultRunMode('safe')
    await load()
  }
}

function runSandboxSetupInBackground(): void {
  sandboxSetupConfirmOpen.value = false
}

const backupQuotaGiB = computed({
  get: () => Number(((draft.value?.files.backupQuotaBytes ?? 3 * 1024 ** 3) / 1024 ** 3).toFixed(2)),
  set: (value: number) => {
    if (!draft.value || !Number.isFinite(value)) return
    draft.value.files.backupQuotaBytes = Math.max(
      Math.ceil(0.1 * 1024 ** 3),
      Math.round(value * 1024 ** 3),
    )
  },
})

const fileSummary = computed(() => {
  const count = builtinDenyWritePaths.value.length + (draft.value?.files.customDenyWritePaths.length ?? 0)
  return `${count} · ${backupQuotaGiB.value} GiB`
})

const commandSummary = computed(() => {
  const count = draft.value?.commands.requireApprovalPrefixes.length ?? 0
  return `${count} · git push`
})

const networkSummary = computed(() => {
  if (draft.value?.network.blockAllNetwork) return t('settings.sandbox.network.blockAll')
  const customRules = (draft.value?.network.allowDomains.length ?? 0) + (draft.value?.network.denyDomains.length ?? 0)
  return customRules
    ? `${customRules} ${t('settings.sandbox.network.title')}`
    : t('settings.sandbox.network.description')
})

const runtimeSummary = computed(() => {
  if (!draft.value?.runtimes.enabled) return t('settings.sandbox.commands.systemToolsDisabled')
  return [
    draft.value.runtimes.python && 'Python',
    draft.value.runtimes.node && 'Node.js',
    draft.value.runtimes.gitBash && 'Git Bash',
  ].filter(Boolean).join(' · ')
})

const activeViewTitle = computed(() => ({
  overview: t('settings.sandbox.title'),
  files: t('settings.sandbox.files.title'),
  commands: t('settings.sandbox.commands.title'),
  network: t('settings.sandbox.network.title'),
  runtimes: t('settings.sandbox.runtimes.title'),
})[activeView.value])

const activeViewDescription = computed(() => ({
  overview: t('settings.sandbox.subtitle'),
  files: t('settings.sandbox.files.description'),
  commands: t('settings.sandbox.commands.description'),
  network: t('settings.sandbox.network.description'),
  runtimes: t('settings.sandbox.runtimes.description'),
})[activeView.value])

function createLineIcon(paths: string[]) {
  return defineComponent({
    setup() {
      return () => h('svg', {
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        'stroke-width': '1.8',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
        'aria-hidden': 'true',
      }, paths.map(path => h('path', { d: path })))
    },
  })
}

const FileIcon = createLineIcon(['M4 5.5A1.5 1.5 0 0 1 5.5 4H11l2 2h5.5A1.5 1.5 0 0 1 20 7.5v10A1.5 1.5 0 0 1 18.5 19h-13A1.5 1.5 0 0 1 4 17.5z', 'M15 12v4', 'M13 14h4'])
const CommandIcon = createLineIcon(['M5 7l4 5-4 5', 'M11 17h8'])
const NetworkIcon = createLineIcon(['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M3 12h18', 'M12 3c2.5 2.5 3.5 5.5 3.5 9s-1 6.5-3.5 9c-2.5-2.5-3.5-5.5-3.5-9s1-6.5 3.5-9z'])
const RuntimeIcon = createLineIcon(['M8 3h8', 'M9 3v5l-4 8a3 3 0 0 0 2.7 4h8.6a3 3 0 0 0 2.7-4l-4-8V3', 'M7.5 15h9'])

function removeAt<T>(values: T[], index: number, section: SandboxPolicySection): void {
  values.splice(index, 1)
  void flushSectionSave(section)
}

function addTextRule(
  values: string[],
  raw: string,
  clear: (value: string) => void,
  section: SandboxPolicySection,
): void {
  const value = raw.trim()
  if (value && !values.includes(value)) {
    values.push(value)
    void flushSectionSave(section)
  }
  clear('')
}

function addPrefix(
  values: string[][],
  raw: string,
  clear: (value: string) => void,
  section: SandboxPolicySection,
): void {
  const prefix = raw.trim().split(/\s+/).filter(Boolean)
  if (prefix.length && !values.some(value => JSON.stringify(value) === JSON.stringify(prefix))) {
    values.push(prefix)
    void flushSectionSave(section)
  }
  clear('')
}

const RuleEditor = defineComponent({
  props: {
    modelValue: { type: String, required: true },
    title: { type: String, required: true },
    placeholder: { type: String, required: true },
    rules: { type: Array as () => string[][], required: true },
  },
  emits: ['update:modelValue', 'add', 'remove'],
  setup(props, { emit }) {
    return () => h('div', { class: 'sandbox-editor' }, [
      h('strong', props.title),
      ...props.rules.map((rule, index) => h('div', { class: 'sandbox-rule sandbox-rule--editable' }, [
        h('code', rule.join(' ')),
        h('button', { type: 'button', class: 'btn btn--ghost', onClick: () => emit('remove', index) }, t('settings.sandbox.actions.remove')),
      ])),
      h('div', { class: 'sandbox-inline-form' }, [
        h('input', {
          value: props.modelValue,
          placeholder: props.placeholder,
          onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLInputElement).value),
          onKeydown: (event: KeyboardEvent) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              emit('add')
            }
          },
        }),
        h('button', { type: 'button', class: 'btn', onClick: () => emit('add') }, t('settings.sandbox.actions.add')),
      ]),
    ])
  },
})

const TextRuleEditor = defineComponent({
  props: {
    modelValue: { type: String, required: true },
    title: { type: String, required: true },
    placeholder: { type: String, required: true },
    rules: { type: Array as () => string[], required: true },
  },
  emits: ['update:modelValue', 'add', 'remove'],
  setup(props, { emit }) {
    return () => h('div', { class: 'sandbox-editor' }, [
      h('strong', props.title),
      ...props.rules.map((rule, index) => h('div', { class: 'sandbox-rule sandbox-rule--editable' }, [
        h('code', rule),
        h('button', { type: 'button', class: 'btn btn--ghost', onClick: () => emit('remove', index) }, t('settings.sandbox.actions.remove')),
      ])),
      h('div', { class: 'sandbox-inline-form' }, [
        h('input', {
          value: props.modelValue,
          placeholder: props.placeholder,
          onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLInputElement).value),
          onKeydown: (event: KeyboardEvent) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              emit('add')
            }
          },
        }),
        h('button', { type: 'button', class: 'btn', onClick: () => emit('add') }, t('settings.sandbox.actions.add')),
      ]),
    ])
  },
})

onMounted(() => void load())
</script>

<style scoped>
.sandbox-settings {
  display: grid;
  gap: 1.25rem;
  max-width: 840px;
  margin: 0 auto;
  padding: 0.25rem 0 2rem;
}

.sandbox-settings__header,
.sandbox-card__head,
.sandbox-option,
.sandbox-token-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.sandbox-settings__header {
  min-height: 52px;
}

.sandbox-settings h3,
.sandbox-settings h4,
.sandbox-settings p {
  margin: 0;
}

.sandbox-settings__header p:last-child,
.sandbox-card__head p,
.sandbox-option p,
.sandbox-detail {
  margin-top: 0.3rem;
  color: var(--text-muted);
  font-size: 0.78rem;
  line-height: 1.45;
}

.sandbox-settings__header p:last-child {
  max-width: 620px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sandbox-runtime-grid small {
  color: var(--text-muted);
  font-weight: 400;
}

.sandbox-reset-warning {
  justify-self: start;
}

.sandbox-settings__status,
.sandbox-card__tag,
.sandbox-rule > span {
  flex: 0 0 auto;
  padding: 0.2rem 0.55rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  font-size: 0.7rem;
}

.sandbox-settings__status.is-ready {
  border-color: color-mix(in srgb, var(--ok) 45%, var(--border));
  color: var(--ok);
}

.sandbox-setup-result {
  padding: 0 0.2rem;
  color: var(--text-muted);
  font-size: 0.76rem;
  line-height: 1.45;
}

.sandbox-settings__state,
.sandbox-card {
  padding: 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
}

.sandbox-overview {
  display: grid;
  gap: 1.05rem;
}

.sandbox-mode-picker,
.sandbox-advanced-row {
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--bg-surface) 96%, var(--bg-hover));
  box-shadow: 0 1px 2px color-mix(in srgb, var(--text) 4%, transparent);
}

.sandbox-mode-picker {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1.05rem 1.15rem;
}

.sandbox-mode-picker p {
  max-width: 520px;
  margin-top: 0.25rem;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 0.76rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sandbox-segmented {
  display: grid;
  grid-template-columns: 1fr 1fr;
  flex: 0 0 auto;
  gap: 2px;
  min-width: 210px;
  padding: 3px;
  border: 1px solid color-mix(in srgb, var(--border) 75%, transparent);
  border-radius: var(--radius-md);
  background: var(--bg-hover);
}

.sandbox-segmented button {
  min-height: 34px;
  padding: 0 0.8rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: 0.78rem;
  font-weight: 600;
}

.sandbox-segmented button.is-selected {
  background: var(--bg-surface);
  box-shadow: 0 1px 3px color-mix(in srgb, var(--text) 13%, transparent);
  color: var(--text);
}

.sandbox-segmented button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.sandbox-list {
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  box-shadow: 0 1px 2px color-mix(in srgb, var(--text) 4%, transparent);
}

.sandbox-list__row,
.sandbox-advanced-row {
  width: 100%;
  border: 0;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: start;
}

.sandbox-list__row {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.8rem;
  min-height: 66px;
  padding: 0.7rem 1rem;
  background: transparent;
}

.sandbox-list__row:not(:last-child) {
  border-bottom: 1px solid color-mix(in srgb, var(--border) 76%, transparent);
}

.sandbox-list__row:hover,
.sandbox-advanced-row:hover {
  background: color-mix(in srgb, var(--bg-hover) 70%, transparent);
}

.sandbox-list__row > span:nth-child(2),
.sandbox-advanced-row > span:first-child {
  display: grid;
  min-width: 0;
  gap: 0.18rem;
}

.sandbox-list__row small,
.sandbox-advanced-row small {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 0.73rem;
  font-weight: 400;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sandbox-list__icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-hover));
  color: var(--accent);
}

.sandbox-list__icon svg {
  width: 19px;
  height: 19px;
}

.sandbox-list__chevron {
  color: var(--text-muted);
  font-size: 1.45rem;
  font-weight: 300;
}

.sandbox-advanced-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 1rem;
  min-height: 58px;
  padding: 0.75rem 1rem;
}

.sandbox-detail-header {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr) 100px;
  align-items: center;
  min-height: 54px;
}

.sandbox-detail-header > div {
  text-align: center;
}

.sandbox-detail-header p {
  margin-top: 0.25rem;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 0.74rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sandbox-detail-control {
  justify-self: end;
}

.sandbox-back {
  display: inline-flex;
  align-items: center;
  justify-self: start;
  gap: 0.15rem;
  min-height: 40px;
  padding: 0 0.6rem 0 0.25rem;
  border: 0;
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  font-size: 0.8rem;
}

.sandbox-back span {
  font-size: 1.55rem;
  line-height: 1;
}

.sandbox-advanced-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding-bottom: 0.9rem;
  border-bottom: 1px solid var(--border);
}

.sandbox-advanced-status p {
  margin-top: 0.2rem;
  color: var(--text-muted);
  font-size: 0.74rem;
}

.sandbox-card {
  display: grid;
  gap: 0.9rem;
}

.sandbox-rule-list,
.sandbox-token-list,
.sandbox-lan-rules,
.sandbox-editor {
  display: grid;
  gap: 0.45rem;
}

.sandbox-rule {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  min-height: 36px;
  padding: 0.4rem 0.55rem;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
}

.sandbox-rule code,
.sandbox-token-secret code {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--text);
  font-size: 0.74rem;
}

.sandbox-rule > code,
.sandbox-rule > input {
  flex: 1;
}

.sandbox-inline-form,
.sandbox-token-create,
.sandbox-token-secret,
.sandbox-field,
.sandbox-runtime-grid {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.sandbox-inline-form input,
.sandbox-token-create > input,
.sandbox-rule input,
.sandbox-field select {
  flex: 1;
  min-width: 0;
}

.sandbox-switch {
  position: relative;
  display: inline-flex;
}

.sandbox-switch input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.sandbox-switch span {
  width: 38px;
  height: 22px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  cursor: pointer;
}

.sandbox-switch span::after {
  display: block;
  width: 16px;
  height: 16px;
  margin: 2px;
  border-radius: var(--radius-full);
  background: var(--text-muted);
  content: '';
  transition: transform var(--dur-fast) var(--ease-standard);
}

.sandbox-switch input:checked + span {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 20%, var(--bg-surface));
}

.sandbox-switch input:checked + span::after {
  background: var(--accent);
  transform: translateX(16px);
}

.sandbox-field {
  justify-content: flex-start;
}

.sandbox-field--compact input {
  width: 100px;
}

.sandbox-warning {
  padding: 0.65rem 0.75rem;
  border-inline-start: 3px solid var(--warn);
  background: color-mix(in srgb, var(--warn) 8%, transparent);
  color: var(--text-muted);
  font-size: 0.76rem;
  line-height: 1.45;
}

.sandbox-runtime-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.sandbox-runtime-grid label {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.7rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.sandbox-lan-rules p {
  color: var(--text-muted);
  font-size: 0.78rem;
}

.sandbox-lan-rules strong {
  margin-inline-end: 0.4rem;
  color: var(--text);
}

.sandbox-token-create {
  flex-wrap: wrap;
}

.sandbox-token-secret {
  align-items: flex-start;
  padding: 0.75rem;
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-surface));
}

.sandbox-token-secret code {
  flex: 1;
  user-select: all;
}

.sandbox-token-row > div {
  display: grid;
  gap: 0.2rem;
}

.sandbox-token-row small {
  color: var(--text-muted);
}

@media (max-width: 720px) {
  .sandbox-settings__header,
  .sandbox-card__head,
  .sandbox-option {
    align-items: flex-start;
  }

  .sandbox-runtime-grid {
    grid-template-columns: 1fr;
  }

  .sandbox-token-create,
  .sandbox-inline-form {
    align-items: stretch;
    flex-direction: column;
  }

  .sandbox-mode-picker {
    align-items: stretch;
    flex-direction: column;
  }

  .sandbox-segmented {
    width: 100%;
  }

  .sandbox-detail-header {
    grid-template-columns: auto minmax(0, 1fr);
  }
}
</style>
