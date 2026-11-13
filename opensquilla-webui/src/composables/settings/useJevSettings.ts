import { computed, inject, onScopeDispose, ref } from 'vue'
import { APP_SETTINGS_KEY, AppSettingsError, type AppSettings, type SettingsValue } from '@/modules/appSettings'

export interface JevSettingsDraft {
  enabled: boolean
  mode: 'suggest' | 'trust'
  max_prompts_per_hour: number
  loop_interval: 3 | 5
  auto_switch_seconds: number
  ensemble_enabled: boolean
  session_restart_enabled: boolean
  context_restart_ratio: number
  context_restart_min_tokens: number
  context_restart_min_turns: number
}

const DEFAULTS: JevSettingsDraft = {
  enabled: false, mode: 'suggest', max_prompts_per_hour: 3,
  loop_interval: 5, auto_switch_seconds: 10,
  ensemble_enabled: true, session_restart_enabled: true,
  context_restart_ratio: 0.8, context_restart_min_tokens: 32000, context_restart_min_turns: 20,
}
const FIELDS = Object.keys(DEFAULTS) as (keyof JevSettingsDraft)[]

function normalize(value: SettingsValue | null): JevSettingsDraft {
  const raw = value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  const delay = Number(raw.auto_switch_seconds)
  const budget = typeof raw.max_prompts_per_hour === 'number' ? raw.max_prompts_per_hour : NaN
  return {
    enabled: raw.enabled === true,
    ensemble_enabled: raw.ensemble_enabled !== false,
    session_restart_enabled: raw.session_restart_enabled !== false,
    context_restart_ratio: typeof raw.context_restart_ratio === 'number'
      && raw.context_restart_ratio >= 0.5 && raw.context_restart_ratio <= 0.95 ? raw.context_restart_ratio : 0.8,
    context_restart_min_tokens: typeof raw.context_restart_min_tokens === 'number'
      && Number.isInteger(raw.context_restart_min_tokens) && raw.context_restart_min_tokens >= 1024
      && raw.context_restart_min_tokens <= 2_000_000 ? raw.context_restart_min_tokens : 32000,
    context_restart_min_turns: typeof raw.context_restart_min_turns === 'number'
      && Number.isInteger(raw.context_restart_min_turns) && raw.context_restart_min_turns >= 2
      && raw.context_restart_min_turns <= 1000 ? raw.context_restart_min_turns : 20,
    mode: raw.mode === 'trust' ? 'trust' : 'suggest',
    max_prompts_per_hour: Number.isInteger(budget) ? Math.min(10, Math.max(0, budget)) : 3,
    loop_interval: raw.loop_interval === 3 ? 3 : 5,
    auto_switch_seconds: Number.isInteger(delay) && delay > 0 && delay <= 60 ? delay : 10,
  }
}

export function useJevSettings(dependencies?: { settings: AppSettings | null }) {
  const settings = dependencies?.settings ?? inject(APP_SETTINGS_KEY, null)
  const baseline = ref<JevSettingsDraft | null>(null)
  const draft = ref<JevSettingsDraft>({ ...DEFAULTS })
  const loading = ref(false)
  const busy = ref(false)
  const errorKey = ref('')
  const messageKey = ref('')
  const unsupported = ref(false)
  let disposed = false
  let readGeneration = 0
  const controller = new AbortController()
  const loaded = computed(() => baseline.value !== null)
  const dirty = computed(() => !!baseline.value
    && FIELDS.some(field => draft.value[field] !== baseline.value![field]))

  async function load(): Promise<void> {
    if (busy.value || disposed) return
    const generation = ++readGeneration
    loading.value = true
    errorKey.value = ''
    messageKey.value = ''
    try {
      if (!settings) throw new AppSettingsError('unsupported', 'Settings unavailable')
      const value = await settings.read('jev_router', { signal: controller.signal })
      if (disposed || generation !== readGeneration) return
      baseline.value = normalize(value)
      draft.value = { ...baseline.value }
      unsupported.value = false
    } catch (error) {
      if (disposed || generation !== readGeneration) return
      unsupported.value = error instanceof AppSettingsError && error.code === 'unsupported'
      errorKey.value = unsupported.value ? 'settings.jev.unsupported' : 'settings.jev.loadFailed'
    } finally {
      if (generation === readGeneration) loading.value = false
    }
  }

  async function save(): Promise<boolean> {
    if (disposed || busy.value || !settings || !baseline.value) return false
    if (!dirty.value) return true
    const next = { ...draft.value }
    if (!Number.isInteger(next.max_prompts_per_hour) || next.max_prompts_per_hour < 0
      || next.max_prompts_per_hour > 10 || ![3, 5].includes(next.loop_interval)
      || !Number.isInteger(next.auto_switch_seconds) || next.auto_switch_seconds < 1
      || next.auto_switch_seconds > 60 || !['suggest', 'trust'].includes(next.mode)
      || typeof next.ensemble_enabled !== 'boolean' || typeof next.session_restart_enabled !== 'boolean'
      || !Number.isFinite(next.context_restart_ratio) || next.context_restart_ratio < 0.5
      || next.context_restart_ratio > 0.95 || !Number.isInteger(next.context_restart_min_tokens)
      || next.context_restart_min_tokens < 1024 || next.context_restart_min_tokens > 2_000_000
      || !Number.isInteger(next.context_restart_min_turns) || next.context_restart_min_turns < 2
      || next.context_restart_min_turns > 1000) {
      errorKey.value = 'settings.jev.invalid'
      return false
    }
    const changes = FIELDS.filter(field => next[field] !== baseline.value![field])
      .map(field => ({ path: `jev_router.${field}`, value: next[field] }))
    busy.value = true
    errorKey.value = ''
    messageKey.value = ''
    try {
      await settings.patch(changes, { signal: controller.signal })
      if (disposed) return false
      baseline.value = next
      messageKey.value = 'settings.jev.saved'
      return true
    } catch (error) {
      if (!disposed) errorKey.value = error instanceof AppSettingsError && error.code === 'conflict'
        ? 'settings.jev.conflict' : 'settings.jev.saveFailed'
      return false
    } finally {
      busy.value = false
    }
  }

  function discard() {
    if (busy.value || !baseline.value) return
    draft.value = { ...baseline.value }
    errorKey.value = ''
    messageKey.value = ''
  }

  onScopeDispose(() => {
    disposed = true
    readGeneration += 1
    controller.abort()
  })
  return { draft, loaded, loading, busy, dirty, errorKey, messageKey, unsupported, load, save, discard }
}
