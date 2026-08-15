<script setup lang="ts">
// Desktop Pet settings — talks to the Electron shell's pet bridge (window.pet),
// which only exists in the desktop app (this section is desktopOnly). All
// preferences persist in the shell's pet-config.json via the existing IPC.
import { onMounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

// The pet bridge is exposed by the shell preload; absent in plain browser tabs.
const pet = (window as unknown as { pet?: Record<string, unknown> }).pet
const available = Boolean(pet && typeof pet.getConfig === 'function')

interface PetConfig {
  mode?: string
  muted?: boolean
  anticsEnabled?: boolean
  online?: boolean
}

const config = shallowRef<PetConfig | null>(null)
const loading = ref(true)
const busy = ref('')

async function refresh(): Promise<void> {
  try {
    const c = (await (pet as any).getConfig?.()) as PetConfig | null
    config.value = c
  } catch {
    config.value = null
  } finally {
    loading.value = false
  }
}

function send(method: string, key: string, ...args: unknown[]): void {
  if (!pet) return
  busy.value = key
  try {
    (pet as any)[method]?.(...args)
  } catch {}
  // The shell pushes an updated config back; poll briefly to reflect it.
  window.setTimeout(() => { busy.value = ''; void refresh() }, 400)
}

function toggle(method: string, key: string): void {
  send(method, key)
}

onMounted(() => { void refresh() })
</script>

<template>
  <section class="control-section" data-testid="pet-settings">
    <div v-if="!available" class="pet-settings__missing">
      {{ t('settings.pet.desktopOnly') }}
    </div>
    <template v-else>
      <div v-if="loading" class="pet-settings__loading">
        {{ t('settings.pet.loading') }}
      </div>
      <template v-else>
        <div class="control-row">
          <div class="control-row__label-block">
            <label class="control-row__label">{{ t('settings.pet.visibilityLabel') }}</label>
            <span class="control-row__desc">{{ t('settings.pet.visibilityDesc') }}</span>
          </div>
          <div class="control-row__control pet-settings__actions">
            <button
              class="pet-settings__button"
              :disabled="busy === 'show'"
              @click="send('setMode', 'show', 'pet')"
            >
              {{ t('settings.pet.show') }}
            </button>
            <button
              class="pet-settings__button"
              :disabled="busy === 'hide'"
              @click="send('closePet', 'hide')"
            >
              {{ t('settings.pet.hide') }}
            </button>
          </div>
        </div>

        <div class="control-row">
          <div class="control-row__label-block">
            <label class="control-row__label" for="pet-mischief-switch">
              {{ t('settings.pet.mischiefLabel') }}
            </label>
            <span class="control-row__desc">{{ t('settings.pet.mischiefDesc') }}</span>
          </div>
          <div class="control-row__control">
            <ControlSwitch
              id="pet-mischief-switch"
              :checked="Boolean(config?.anticsEnabled)"
              :busy="busy === 'antics'"
              @change="toggle('anticsToggle', 'antics')"
            />
          </div>
        </div>

        <div class="control-row">
          <div class="control-row__label-block">
            <label class="control-row__label" for="pet-mute-switch">
              {{ t('settings.pet.muteLabel') }}
            </label>
            <span class="control-row__desc">{{ t('settings.pet.muteDesc') }}</span>
          </div>
          <div class="control-row__control">
            <ControlSwitch
              id="pet-mute-switch"
              :checked="!config?.muted"
              :busy="busy === 'mute'"
              @change="toggle('toggleMute', 'mute')"
            />
          </div>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.pet-settings__missing,
.pet-settings__loading {
  color: var(--text-muted);
  padding: var(--sp-3, 12px) 0;
}
.pet-settings__actions {
  display: flex;
  gap: var(--sp-2, 8px);
}
.pet-settings__button {
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: var(--bg-elevated);
  cursor: pointer;
  color: var(--text);
}
.pet-settings__button:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
