<script setup lang="ts">
// Provider-console shortcuts for the Feishu channel: the permission-manifest
// copy button and deep links rendered beside the credential fields they
// unblock. Content (URLs, JSON) is machine material from the catalog; labels
// are localized here by aid id. Links substitute the draft's App id and swap
// to the Lark console domain when the entry targets lark.
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChannelSetupAid } from '@/composables/channels/useChannelEditor'

const props = defineProps<{
  aids: ChannelSetupAid[]
  appId: string
  lark: boolean
}>()

const { t } = useI18n()

function aidHref(aid: ChannelSetupAid): string {
  let url = (aid.content || '').replace('{app_id}', props.appId)
  if (props.lark) url = url.replace('open.feishu.cn', 'open.larksuite.com')
  return url
}

const copiedAidId = ref('')
async function copyAid(aid: ChannelSetupAid) {
  try {
    await navigator.clipboard.writeText(aid.content || '')
    copiedAidId.value = aid.id
    window.setTimeout(() => {
      if (copiedAidId.value === aid.id) copiedAidId.value = ''
    }, 2000)
  } catch {
    /* clipboard unavailable — the manifest is also in the docs */
  }
}
</script>

<template>
  <section class="fsa">
    <h4 class="fsa__title">{{ t('setup.channels.aids.title') }}</h4>
    <template v-for="aid in aids" :key="aid.id">
      <div v-if="aid.kind === 'copy'" class="fsa__aid">
        <span>{{ t(`setup.channels.aids.${aid.id}`) }}</span>
        <button type="button" class="btn btn--ghost fsa__btn" @click="copyAid(aid)">
          {{ copiedAidId === aid.id ? t('setup.channels.aids.copied') : t('setup.channels.aids.copy') }}
        </button>
      </div>
      <div v-else-if="aid.kind === 'link'" class="fsa__aid">
        <a
          v-if="appId"
          class="fsa__link"
          :href="aidHref(aid)"
          target="_blank"
          rel="noreferrer noopener"
        >{{ t(`setup.channels.aids.${aid.id}`) }} ↗</a>
        <span v-else class="fsa__muted">
          {{ t(`setup.channels.aids.${aid.id}`) }} — {{ t('setup.channels.aids.needAppId') }}
        </span>
      </div>
    </template>
  </section>
</template>

<style scoped>
.fsa { display: grid; gap: var(--sp-2); }
.fsa__title {
  border-bottom: 1px solid var(--border);
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  margin: 0 0 var(--sp-1);
  padding-bottom: var(--sp-2);
}
.fsa__aid {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-sm);
  gap: 10px;
  justify-content: space-between;
}
.fsa__btn { flex: none; }
.fsa__link { color: var(--accent); text-decoration: none; }
.fsa__link:hover { text-decoration: underline; }
.fsa__muted { color: var(--text-dim); font-size: var(--fs-sm); }
</style>
