<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'

const { t } = useI18n()

interface BehaviorPanelContract {
  autoSessionTitles: boolean
  commitMessageEnabled: boolean
  commitMessageInstructions: string
  statusText: string
}

defineProps<{
  panel: BehaviorPanelContract
  embedded?: boolean
}>()

const emit = defineEmits<{
  updateAutoSessionTitles: [enabled: boolean]
  updateCommitMessageEnabled: [enabled: boolean]
  updateCommitMessageInstructions: [value: string]
}>()
</script>

<template>
  <section class="control-section" :class="{ 'control-section--embedded': embedded }">
    <div v-if="!embedded" class="control-section__head">
      <h3 class="control-section__title">{{ t('setup.behavior.title') }}</h3>
      <p class="control-section__desc">{{ panel.statusText }}</p>
    </div>
    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.behavior.autoTitlesLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.behavior.autoTitlesDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.autoSessionTitles"
          name="setup_auto_session_titles"
          :aria-label="t('setup.behavior.autoTitlesLabel')"
          @change="(value) => emit('updateAutoSessionTitles', value)"
        />
      </div>
    </label>
    <!-- The other auto-written text in the app: the commit message the
         workspace review panel drafts. Its switch and its rule are one block,
         because the rule is what the switch governs; with drafting off the
         staged patch never reaches a model. The rule lives here rather than in
         the review panel, so one place owns what a message should say. -->
    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.behavior.commitMessageLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.behavior.commitMessageDesc') }}</span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          :checked="panel.commitMessageEnabled"
          name="setup_commit_message_enabled"
          :aria-label="t('setup.behavior.commitMessageLabel')"
          @change="(value) => emit('updateCommitMessageEnabled', value)"
        />
      </div>
    </label>
    <label class="control-row control-row--stack">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ t('setup.behavior.commitMessageRuleLabel') }}</span>
        <span class="control-row__desc">{{ t('setup.behavior.commitMessageRuleDesc') }}</span>
      </div>
      <div class="control-row__control">
        <textarea
          class="control-input commit-message-rule"
          rows="3"
          data-testid="setup-commit-message-rule"
          :value="panel.commitMessageInstructions"
          :placeholder="t('setup.behavior.commitMessageRulePlaceholder')"
          :aria-label="t('setup.behavior.commitMessageRuleLabel')"
          @input="emit(
            'updateCommitMessageInstructions',
            ($event.target as HTMLTextAreaElement).value,
          )"
        ></textarea>
      </div>
    </label>
  </section>
</template>

<style scoped>
.control-section--embedded { display: contents; }

/* Layout only: the surface and focus treatment come from the shared field
   rules, so this matches every other field in the app. */
.commit-message-rule {
  resize: vertical;
}
</style>
