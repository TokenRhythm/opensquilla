<template>
  <main class="not-found control-stage">
    <section class="not-found__card" role="status">
      <span class="not-found__code" aria-hidden="true">404</span>
      <h1 class="not-found__title">{{ t('notFound.title') }}</h1>
      <p class="not-found__body">{{ t('notFound.body') }}</p>
      <div class="not-found__actions">
        <button type="button" class="btn btn--primary" @click="go('/chat')">
          {{ t('notFound.goToChat') }}
        </button>
        <button type="button" class="btn btn--ghost" @click="go('/sessions')">
          {{ t('notFound.sessions') }}
        </button>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

const { t } = useI18n()
const router = useRouter()

function go(path: string) {
  router.push(path).catch(() => {})
}
</script>

<style scoped>
.not-found {
  align-items: center;
  display: flex;
  justify-content: center;
  min-height: 60vh;
}

.not-found__card {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  max-width: 520px;
  padding: var(--sp-8);
  text-align: center;
}

.not-found__code {
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: var(--fs-2xl);
  font-weight: 700;
}

.not-found__title {
  color: var(--text);
  font-size: var(--fs-xl);
  margin: 0;
}

.not-found__body {
  color: var(--text-muted);
  margin: 0;
}

.not-found__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: center;
}
</style>
