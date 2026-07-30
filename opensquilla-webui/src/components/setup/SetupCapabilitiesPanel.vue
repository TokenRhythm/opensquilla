<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import SetupNeedList from '@/components/SetupNeedList.vue'
import SetupCommandBlock from '@/components/setup/SetupCommandBlock.vue'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

interface ProviderOption {
  providerId: string
  label: string
}

interface ProviderSpec {
  providerId?: string
  envKey?: string
  defaultBaseUrl?: string
  [key: string]: unknown
}

interface CapabilitiesPanelContract {
  form: {
    searchProvider: string
    searchMaxResults: number
    searchApiKey: string
    searchApiKeyEnv: string
    searchProxy: string
    searchUseEnvProxy: boolean
    searchFallbackPolicy: string
    searchDiagnostics: boolean
    memoryProvider: string
    memoryModel: string
    memoryApiKey: string
    memoryApiKeyEnv: string
    memoryBaseUrl: string
    memoryOnnxDir: string
    imageProvider: string
    imagePrimary: string
    imageApiKey: string
    imageKeyConfigured: boolean
    imageApiKeyEnv: string
    imageBaseUrl: string
    imageEnabled: boolean
    imageSize: string
    imageOutputFormat: string
    imageFallbacks: string
    memoryAutoCapture: boolean
    audioEnabled: boolean
    audioApiKey: string
    audioApiKeyEnv: string
    audioBaseUrl: string
    audioTtsVoice: string
    audioTtsModel: string
    audioLanguageCode: string
  }
  options: {
    searchProviders: ProviderOption[]
    memoryProviders: ProviderOption[]
    imageProviders: ProviderOption[]
    imageSpec: ProviderSpec | null
  }
  state: {
    searchRequiresKey: boolean
    searchEnvPlaceholder: string
    searchAdvancedOpen: boolean
    searchNeeds: string[]
    searchEnvCommand: string
    searchStatusText: string
    memoryLocalControlEnabled: boolean
    memoryRemoteControlEnabled: boolean
    memoryApiKeyEnabled: boolean
    memoryRemoteOptionsOpen: boolean
    memoryRemoteOptionsSummary: string
    memoryModelPlaceholder: string
    memoryBasePlaceholder: string
    memoryOnnxPlaceholder: string
    memoryApiKeyLabel: string
    memoryApiKeyPlaceholder: string
    memoryEnvPlaceholder: string
    memoryNeeds: string[]
    memoryStatusText: string
    memoryEnvCommand: string
    imageNeeds: string[]
    imageStatusText: string
    imageEnvCommand: string
    capabilityBadgeTone: (name: string) => string
    capabilityBadgeLabel: (name: string) => string
    capabilitySaveButtonClass: (name: string) => string
    audioStatusText: string
    audioBadgeTone: string
    audioBadgeLabel: string
    audioKeyPlaceholder: string
  }
}

defineProps<{
  panel: CapabilitiesPanelContract
}>()

const emit = defineEmits<{
  updateField: [group: 'search' | 'memory' | 'image' | 'audio', key: string, value: string | number | boolean]
  searchProviderChange: []
  memoryProviderChange: []
  imageProviderChange: [providerId: string]
  saveSearch: []
  saveMemory: []
  saveImage: []
  saveAudio: []
  copy: [command: string]
}>()

function onSearchProviderSelect(event: Event) {
  emit('updateField', 'search', 'provider', (event.target as HTMLSelectElement).value)
  emit('searchProviderChange')
}

function onMemoryProviderSelect(event: Event) {
  emit('updateField', 'memory', 'provider', (event.target as HTMLSelectElement).value)
  emit('memoryProviderChange')
}

function onImageProviderSelect(event: Event) {
  emit('imageProviderChange', (event.target as HTMLSelectElement).value)
}
</script>

<template>
  <div class="setup-capabilities">
    <!-- Web search -->
    <section class="control-section">
      <div class="control-section__head">
        <h3 class="control-section__title">{{ t('setup.search.title') }}</h3>
        <span class="control-pill control-section__status" :class="panel.state.capabilityBadgeTone('search')">{{ panel.state.capabilityBadgeLabel('search') }}</span>
        <p class="control-section__desc">{{ panel.state.searchStatusText }}</p>
      </div>
      <SetupCommandBlock
        v-if="panel.state.searchEnvCommand"
        class="setup-warning__command setup-mini__env-command"
        :command="panel.state.searchEnvCommand"
        :copy-label="t('setup.search.copyKeyCommand')"
        @copy="emit('copy', $event)"
      />
      <SetupNeedList :items="panel.state.searchNeeds" :label="t('setup.search.needs')" />
      <label class="control-row control-row--stack">
        <div class="control-row__label-block">
          <span class="control-row__label">{{ t('setup.search.credentialProvider') }}</span>
          <span class="control-row__desc">{{ t('setup.search.multiProviderHint') }}</span>
        </div>
        <div class="control-row__control">
          <select class="control-input" :value="panel.form.searchProvider" name="setup_search_provider" @change="onSearchProviderSelect">
            <option v-for="p in panel.options.searchProviders" :key="p.providerId" :value="p.providerId">{{ p.label }}</option>
          </select>
        </div>
      </label>
      <label class="control-row">
        <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.search.defaultResults') }}</span></div>
        <div class="control-row__control">
          <input class="control-input control-input--narrow" :value="panel.form.searchMaxResults" name="setup_search_max_results" type="number" min="1" max="20" step="1" inputmode="numeric" @input="emit('updateField', 'search', 'maxResults', Number(($event.target as HTMLInputElement).value))">
        </div>
      </label>
      <template v-if="panel.state.searchRequiresKey">
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.apiKey') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.searchApiKey" name="setup_search_api_key" type="password" :placeholder="t('setup.common.leaveBlankKeep')" @input="emit('updateField', 'search', 'apiKey', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.apiKeyEnv') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.searchApiKeyEnv" name="setup_search_api_key_env" :placeholder="panel.state.searchEnvPlaceholder" @input="emit('updateField', 'search', 'apiKeyEnv', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
      </template>
      <details :open="!!panel.state.searchAdvancedOpen">
        <summary class="control-row control-row--divider">{{ t('setup.search.advanced') }}</summary>
        <label class="control-row control-row--stack">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.search.httpProxy') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.searchProxy" name="setup_search_proxy" placeholder="http://127.0.0.1:7890" @input="emit('updateField', 'search', 'proxy', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.search.useEnvProxy') }}</span></div>
          <div class="control-row__control">
            <ControlSwitch :checked="panel.form.searchUseEnvProxy" name="setup_search_use_env_proxy" @change="(v) => emit('updateField', 'search', 'useEnvProxy', v)" />
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.search.fallbackPolicy') }}</span></div>
          <div class="control-row__control">
            <select class="control-input" :value="panel.form.searchFallbackPolicy" name="setup_search_fallback_policy" @change="emit('updateField', 'search', 'fallbackPolicy', ($event.target as HTMLSelectElement).value)">
              <option value="off">{{ t('setup.search.fallbackOff') }}</option>
              <option value="network">{{ t('setup.search.fallbackNetwork') }}</option>
            </select>
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.search.diagnostics') }}</span></div>
          <div class="control-row__control">
            <ControlSwitch :checked="panel.form.searchDiagnostics" name="setup_search_diagnostics" @change="(v) => emit('updateField', 'search', 'diagnostics', v)" />
          </div>
        </label>
      </details>
      <div class="control-section__actions">
        <button :class="panel.state.capabilitySaveButtonClass('search')" @click="emit('saveSearch')">{{ t('setup.search.save') }}</button>
      </div>
    </section>

    <!-- Memory embedding -->
    <section class="control-section">
      <div class="control-section__head">
        <h3 class="control-section__title">{{ t('setup.memory.title') }}</h3>
        <span class="control-pill control-section__status" :class="panel.state.capabilityBadgeTone('memory_embedding')">{{ panel.state.capabilityBadgeLabel('memory_embedding') }}</span>
        <p class="control-section__desc">{{ panel.state.memoryStatusText }}</p>
      </div>
      <SetupCommandBlock
        v-if="panel.state.memoryEnvCommand"
        class="setup-warning__command setup-mini__env-command"
        :command="panel.state.memoryEnvCommand"
        :copy-label="t('setup.memory.copyKeyCommand')"
        @copy="emit('copy', $event)"
      />
      <SetupNeedList :items="panel.state.memoryNeeds" :label="t('setup.memory.needs')" />
      <label class="control-row">
        <div class="control-row__label-block">
          <span class="control-row__label">{{ t('setup.memory.autoCaptureLabel') }}</span>
          <span class="control-row__desc">{{ t('setup.memory.autoCaptureDesc') }}</span>
        </div>
        <div class="control-row__control">
          <ControlSwitch :checked="panel.form.memoryAutoCapture" name="setup_memory_auto_capture" @change="(v) => emit('updateField', 'memory', 'autoCapture', v)" />
        </div>
      </label>
      <label class="control-row">
        <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.provider') }}</span></div>
        <div class="control-row__control">
          <select class="control-input" :value="panel.form.memoryProvider" name="setup_memory_provider" @change="onMemoryProviderSelect">
            <option v-for="p in panel.options.memoryProviders" :key="p.providerId" :value="p.providerId">{{ p.label }}</option>
          </select>
        </div>
      </label>
      <label v-if="panel.state.memoryLocalControlEnabled" class="control-row control-row--stack">
        <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.memory.onnxDir') }}</span></div>
        <div class="control-row__control">
          <input class="control-input" :value="panel.form.memoryOnnxDir" name="setup_memory_onnx_dir" :placeholder="panel.state.memoryOnnxPlaceholder" @input="emit('updateField', 'memory', 'onnxDir', ($event.target as HTMLInputElement).value)">
        </div>
      </label>
      <details v-if="panel.state.memoryRemoteControlEnabled || panel.state.memoryApiKeyEnabled" :open="panel.state.memoryRemoteOptionsOpen">
        <summary class="control-row control-row--divider">{{ panel.state.memoryRemoteOptionsSummary }}</summary>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.model') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.memoryModel" name="setup_memory_model" :placeholder="panel.state.memoryModelPlaceholder" :disabled="!panel.state.memoryRemoteControlEnabled" @input="emit('updateField', 'memory', 'model', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ panel.state.memoryApiKeyLabel }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.memoryApiKey" name="setup_memory_api_key" type="password" :placeholder="panel.state.memoryApiKeyPlaceholder" :disabled="!panel.state.memoryApiKeyEnabled" @input="emit('updateField', 'memory', 'apiKey', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.apiKeyEnv') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.memoryApiKeyEnv" name="setup_memory_api_key_env" :placeholder="panel.state.memoryEnvPlaceholder" :disabled="!panel.state.memoryApiKeyEnabled" @input="emit('updateField', 'memory', 'apiKeyEnv', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row control-row--stack">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.baseUrl') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.memoryBaseUrl" name="setup_memory_base_url" :placeholder="panel.state.memoryBasePlaceholder" :disabled="!panel.state.memoryRemoteControlEnabled" @input="emit('updateField', 'memory', 'baseUrl', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
      </details>
      <div class="control-section__actions">
        <button :class="panel.state.capabilitySaveButtonClass('memory_embedding')" @click="emit('saveMemory')">{{ t('setup.memory.save') }}</button>
      </div>
    </section>

    <!-- Image generation -->
    <section class="control-section">
      <div class="control-section__head">
        <h3 class="control-section__title">{{ t('setup.image.title') }}</h3>
        <span class="control-pill control-section__status" :class="panel.state.capabilityBadgeTone('image_generation')">{{ panel.state.capabilityBadgeLabel('image_generation') }}</span>
        <p class="control-section__desc">{{ panel.state.imageStatusText }}</p>
      </div>
      <SetupCommandBlock
        v-if="panel.state.imageEnvCommand"
        class="setup-warning__command setup-mini__env-command"
        :command="panel.state.imageEnvCommand"
        :copy-label="t('setup.image.copyKeyCommand')"
        @copy="emit('copy', $event)"
      />
      <SetupNeedList :items="panel.state.imageNeeds" :label="t('setup.image.needs')" />
      <label class="control-row">
        <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.image.enabled') }}</span></div>
        <div class="control-row__control">
          <ControlSwitch :checked="panel.form.imageEnabled" name="setup_image_enabled" @change="(v) => emit('updateField', 'image', 'enabled', v)" />
        </div>
      </label>
      <template v-if="panel.form.imageEnabled">
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.provider') }}</span></div>
          <div class="control-row__control">
            <select class="control-input" :value="panel.form.imageProvider" name="setup_image_provider" @change="onImageProviderSelect">
              <option v-for="p in panel.options.imageProviders" :key="p.providerId" :value="p.providerId">{{ p.label }}</option>
            </select>
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.image.primaryModel') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.imagePrimary" name="setup_image_primary" @input="emit('updateField', 'image', 'primary', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.common.apiKey') }}</span>
            <!-- Saved-key state for the write-only field: the key itself is never rendered. -->
            <span class="control-row__desc">{{ panel.form.imageKeyConfigured ? t('setup.capabilities.imageKeyConfigured') : t('setup.capabilities.imageKeyMissing') }}</span>
          </div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.imageApiKey" name="setup_image_api_key" type="password" :placeholder="panel.form.imageKeyConfigured ? t('setup.capabilities.imageKeyPlaceholderKeep') : t('setup.capabilities.imageKeyPlaceholderNew')" @input="emit('updateField', 'image', 'apiKey', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.apiKeyEnv') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.imageApiKeyEnv" name="setup_image_api_key_env" :placeholder="panel.options.imageSpec?.envKey || 'OPENROUTER_API_KEY'" @input="emit('updateField', 'image', 'apiKeyEnv', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row control-row--stack">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.baseUrl') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.imageBaseUrl" name="setup_image_base_url" :placeholder="panel.options.imageSpec?.defaultBaseUrl || 'https://api.example.com/v1'" @input="emit('updateField', 'image', 'baseUrl', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.image.size') }}</span></div>
          <div class="control-row__control">
            <select class="control-input" :value="panel.form.imageSize" name="setup_image_size" @change="emit('updateField', 'image', 'size', ($event.target as HTMLSelectElement).value)">
              <option value="1024x1024">1024×1024</option>
              <option value="1536x1024">1536×1024</option>
              <option value="1024x1536">1024×1536</option>
            </select>
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.image.outputFormat') }}</span></div>
          <div class="control-row__control">
            <select class="control-input" :value="panel.form.imageOutputFormat" name="setup_image_output_format" @change="emit('updateField', 'image', 'outputFormat', ($event.target as HTMLSelectElement).value)">
              <option value="png">PNG</option>
              <option value="jpeg">JPEG</option>
              <option value="webp">WebP</option>
            </select>
          </div>
        </label>
        <label class="control-row control-row--stack">
          <div class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.image.fallbacks') }}</span>
            <span class="control-row__desc">{{ t('setup.image.fallbacksHint') }}</span>
          </div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.imageFallbacks" name="setup_image_fallbacks" placeholder="openai/gpt-image-1, openrouter/google/gemini-3.1-flash-image-preview" @input="emit('updateField', 'image', 'fallbacks', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
      </template>
      <div class="control-section__actions">
        <button :class="panel.state.capabilitySaveButtonClass('image_generation')" @click="emit('saveImage')">{{ t('setup.image.save') }}</button>
      </div>
    </section>

    <!-- Audio -->
    <section class="control-section">
      <div class="control-section__head">
        <h3 class="control-section__title">{{ t('setup.audio.title') }}</h3>
        <span class="control-pill control-section__status" :class="panel.state.audioBadgeTone">{{ panel.state.audioBadgeLabel }}</span>
        <p class="control-section__desc">{{ panel.state.audioStatusText }}</p>
      </div>
      <label class="control-row">
        <div class="control-row__label-block">
          <span class="control-row__label">{{ t('setup.audio.enableLabel') }}</span>
          <span class="control-row__desc">{{ t('setup.audio.enableDesc') }}</span>
        </div>
        <div class="control-row__control">
          <ControlSwitch :checked="panel.form.audioEnabled" name="setup_audio_enabled" @change="(v) => emit('updateField', 'audio', 'enabled', v)" />
        </div>
      </label>
      <template v-if="panel.form.audioEnabled">
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.apiKey') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.audioApiKey" name="setup_audio_api_key" type="password" :placeholder="panel.state.audioKeyPlaceholder" @input="emit('updateField', 'audio', 'apiKey', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.apiKeyEnv') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.audioApiKeyEnv" name="setup_audio_api_key_env" placeholder="ELEVENLABS_API_KEY" @input="emit('updateField', 'audio', 'apiKeyEnv', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.audio.ttsVoice') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.audioTtsVoice" name="setup_audio_tts_voice" :placeholder="t('setup.common.leaveBlankKeep')" @input="emit('updateField', 'audio', 'ttsVoice', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.audio.ttsModel') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.audioTtsModel" name="setup_audio_tts_model" :placeholder="t('setup.common.leaveBlankKeep')" @input="emit('updateField', 'audio', 'ttsModel', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.common.baseUrl') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.audioBaseUrl" name="setup_audio_base_url" :placeholder="t('setup.common.leaveBlankKeep')" @input="emit('updateField', 'audio', 'baseUrl', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
        <label class="control-row">
          <div class="control-row__label-block"><span class="control-row__label">{{ t('setup.audio.languageCode') }}</span></div>
          <div class="control-row__control">
            <input class="control-input" :value="panel.form.audioLanguageCode" name="setup_audio_language_code" placeholder="zh-CN, en-US…" @input="emit('updateField', 'audio', 'languageCode', ($event.target as HTMLInputElement).value)">
          </div>
        </label>
      </template>
      <div class="control-section__actions">
        <button class="btn" @click="emit('saveAudio')">{{ t('setup.audio.save') }}</button>
      </div>
    </section>
  </div>
</template>
