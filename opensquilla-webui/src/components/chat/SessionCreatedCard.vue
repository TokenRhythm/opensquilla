<template>
  <div
    class="session-created-card"
    data-testid="session-created-card"
    :data-session-key="sessionKey"
    :data-session-state="availability"
  >
    <span class="session-created-card__identity">
      <Icon name="chat" :size="17" />
      <span>{{ displayTitle }}</span>
    </span>
    <button
      type="button"
      class="session-created-card__open"
      :aria-label="availability === 'missing'
        ? t('chat.sessionCreated.deleted')
        : t('chat.sessionCreated.open')"
      :disabled="availability !== 'available'"
      @click="openSession"
    >
      {{ availability === 'missing'
        ? t('chat.sessionCreated.deleted')
        : t('chat.sessionCreated.open') }}
      <Icon v-if="availability !== 'missing'" name="chevronRight" :size="15" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

const props = defineProps<{
  sessionKey: string
  title?: string
  resolveSessionAvailability?: (sessionKey: string) => Promise<boolean>
}>()

const emit = defineEmits<{
  open: [sessionKey: string]
}>()

const { t } = useI18n()
const availability = ref<'checking' | 'available' | 'missing'>('available')

const displayTitle = computed(() => {
  const title = props.title?.trim()
  if (title) return title
  const segments = props.sessionKey.split(':')
  const suffix = segments[segments.length - 1]?.slice(-8) || props.sessionKey.slice(-8)
  return t('chat.sessionCreated.fallbackTitle', { suffix })
})

async function openSession() {
  if (availability.value !== 'available') return
  const sessionKey = props.sessionKey
  const resolveAvailability = props.resolveSessionAvailability
  if (!resolveAvailability) {
    emit('open', sessionKey)
    return
  }
  availability.value = 'checking'
  try {
    const available = await resolveAvailability(sessionKey)
    if (props.sessionKey !== sessionKey) return
    availability.value = available ? 'available' : 'missing'
    if (available) emit('open', sessionKey)
  } catch {
    // Preserve legacy navigation on transient failures. Only an authoritative
    // not-found result disables the historical card.
    if (props.sessionKey !== sessionKey) return
    availability.value = 'available'
    emit('open', sessionKey)
  }
}
</script>

<style scoped>
.session-created-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  gap: var(--sp-3);
  margin: var(--sp-2) 0;
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
}

.session-created-card__identity,
.session-created-card__open {
  display: inline-flex;
  align-items: center;
}

.session-created-card__identity {
  min-width: 0;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.session-created-card__identity :deep(.icon) {
  color: var(--text-muted);
}

.session-created-card__identity > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-created-card__open {
  flex: 0 0 auto;
  gap: var(--sp-1);
  padding: var(--sp-1) var(--sp-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
}

.session-created-card__open:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.session-created-card__open:disabled {
  cursor: default;
  opacity: var(--state-disabled-opacity);
}

.session-created-card__open:disabled:hover {
  background: transparent;
  color: var(--text-muted);
}

.session-created-card__open:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}

@media (max-width: 540px) {
  .session-created-card {
    padding: var(--sp-3);
  }
}
</style>
