<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import SetupCommandBlock from '@/components/setup/SetupCommandBlock.vue'
import { usePendingRestart } from '@/composables/usePendingRestart'
import { useToasts } from '@/composables/useToasts'
import { copyTextWithFallback } from '@/utils/browser'

// Rendered above both channel lists (Settings runtime list and /channels
// table) so the restart requirement is visible wherever channels are.
const { t } = useI18n()
const { pending, count, dismiss } = usePendingRestart()
const { pushToast } = useToasts()

async function copyCommand(command: string): Promise<void> {
  try {
    await copyTextWithFallback(command)
    pushToast(t('channelStatus.banner.copied'), { tone: 'ok' })
  } catch {
    pushToast(t('channelStatus.banner.copyFailed'), { tone: 'danger' })
  }
}
</script>

<template>
  <section v-if="count > 0" class="prb" role="status" :aria-label="t('channelStatus.banner.label')">
    <div class="prb__text">
      <Icon name="refresh" :size="16" aria-hidden="true" />
      <p>{{ t('channelStatus.banner.message', { count }) }}</p>
    </div>
    <div class="prb__channels">
      <span v-for="entry in pending" :key="entry.channel" class="prb__chip">
        <span>{{ entry.channel }}</span>
        <button
          type="button"
          class="prb__dismiss"
          :aria-label="t('channelStatus.banner.dismiss', { name: entry.channel })"
          :title="t('channelStatus.banner.dismiss', { name: entry.channel })"
          @click="dismiss(entry.channel)"
        >×</button>
      </span>
    </div>
    <SetupCommandBlock
      class="prb__command"
      command="opensquilla gateway restart"
      :copy-label="t('channelStatus.banner.copyLabel')"
      @copy="copyCommand"
    />
  </section>
</template>

<style scoped>
.prb {
  align-items: center;
  background: color-mix(in srgb, var(--info) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--info) 36%, var(--border));
  border-radius: var(--radius-md);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-3);
  padding: 9px 13px;
}
.prb__text { align-items: center; color: var(--info); display: flex; gap: 8px; min-width: 0; }
.prb__text p { color: var(--text); font-size: var(--fs-sm); margin: 0; }
.prb__channels { display: flex; flex-wrap: wrap; gap: 6px; }
.prb__chip {
  align-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  display: inline-flex;
  font-family: var(--font-mono);
  font-size: 11px;
  gap: 4px;
  max-width: 100%;
  min-width: 0;
  padding: 1px 4px 1px 9px;
}
.prb__chip > span { max-width: 22ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prb__dismiss {
  background: transparent;
  border: 0;
  border-radius: 50%;
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  line-height: 1;
  padding: 2px 5px;
}
@media (pointer: coarse) {
  .prb__dismiss { min-height: 32px; min-width: 32px; }
}
.prb__dismiss:hover { background: var(--bg-surface-2); color: var(--text); }
.prb__command { margin-left: auto; }
@media (max-width: 760px) {
  .prb__command { margin-left: 0; width: 100%; }
}
</style>
