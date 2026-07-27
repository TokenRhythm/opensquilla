<template>
  <article
    class="plan-card"
    :class="{ 'plan-card--superseded': !plan.current }"
    :data-plan-id="plan.planId"
    :data-plan-revision-id="plan.revisionId"
    :aria-labelledby="titleId"
  >
    <header class="plan-card__header">
      <div class="plan-card__identity">
        <span class="plan-card__icon" aria-hidden="true">
          <Icon name="listChecks" :size="17" />
        </span>
        <span class="plan-card__eyebrow">{{ t('chat.plan.label') }}</span>
        <span class="plan-card__revision">{{ t('chat.plan.revision', { id: plan.revisionId }) }}</span>
      </div>
      <div class="plan-card__header-actions">
        <span
          class="plan-card__state"
          :class="{ 'plan-card__state--current': plan.current }"
        >
          {{ plan.current ? t('chat.plan.current') : t('chat.plan.superseded') }}
        </span>
        <button
          v-if="isCollapsible"
          type="button"
          class="plan-card__disclosure"
          :aria-label="expanded ? t('chat.plan.collapse') : t('chat.plan.expand')"
          :title="expanded ? t('chat.plan.collapse') : t('chat.plan.expand')"
          :aria-expanded="expanded"
          :aria-controls="bodyId"
          @click="toggleBody"
        >
          <Icon
            class="plan-card__disclosure-icon"
            :name="expanded ? 'collapse' : 'expand'"
            :size="15"
            aria-hidden="true"
          />
        </button>
      </div>
    </header>

    <h3 :id="titleId" class="plan-card__title">{{ plan.title }}</h3>

    <div
      v-if="hasBody"
      :id="bodyId"
      ref="bodyElement"
      class="plan-card__body"
      :class="{
        'plan-card__body--clipped': bodyClipped,
        'plan-card__body--expanded': expanded,
        'plan-card__body--expanding': bodyMotion === 'expanding',
        'plan-card__body--collapsing': bodyMotion === 'collapsing',
      }"
      :style="bodyStyle"
      :inert="bodyClipped ? true : undefined"
      @transitionend="finishBodyMotion"
    >
      <div ref="bodyContentElement" class="plan-card__body-content">
        <!-- eslint-disable-next-line vue/no-v-html -- useChatTextRendering sanitizes this HTML. -->
        <div
          v-if="renderedMarkdown"
          class="plan-card__markdown msg-ai-text"
          v-html="renderedMarkdown"
        />

        <section
          v-if="plan.steps.length"
          class="plan-card__steps"
          :aria-labelledby="stepsTitleId"
        >
          <h4 :id="stepsTitleId" class="plan-card__steps-title">
            {{ t('chat.plan.steps') }}
          </h4>
          <ol class="plan-card__step-list">
            <li
              v-for="step in plan.steps"
              :key="step.stepId"
              class="plan-card__step"
              :data-step-id="step.stepId"
            >
              <span class="plan-card__step-title">{{ step.title }}</span>
              <span v-if="step.details" class="plan-card__step-details">{{ step.details }}</span>
            </li>
          </ol>
        </section>
      </div>
      <span
        v-if="bodyClipped"
        class="plan-card__body-fade"
        aria-hidden="true"
      />
    </div>

    <footer v-if="plan.current" class="plan-card__actions">
      <button
        class="btn btn--primary"
        type="button"
        :disabled="actionsDisabled"
        @click="emitAction('implement-current')"
      >
        {{ actionLabel('implement-current', 'chat.plan.implementCurrent') }}
      </button>
      <button
        class="btn"
        type="button"
        :disabled="actionsDisabled"
        @click="emitAction('implement-new')"
      >
        {{ actionLabel('implement-new', 'chat.plan.implementNew') }}
      </button>
      <button
        class="btn btn--ghost"
        type="button"
        :disabled="actionsDisabled"
        @click="emitAction('replan')"
      >
        {{ actionLabel('replan', 'chat.plan.replan') }}
      </button>
    </footer>
  </article>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  useId,
  watch,
  type CSSProperties,
} from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useMediaQuery } from '@/composables/chat/useMediaQuery'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import type {
  PlanCardAction,
  PlanCardActionTarget,
  PlanRevisionSnapshot,
} from '@/types/plans'
import {
  readPlanDisclosureExpansion,
  writePlanDisclosureExpansion,
} from '@/utils/chat/planDisclosureState'

const props = withDefaults(defineProps<{
  plan: PlanRevisionSnapshot
  disabled?: boolean
  pendingAction?: PlanCardAction | null
}>(), {
  disabled: false,
  pendingAction: null,
})

const emit = defineEmits<{
  'implement-current': [target: PlanCardActionTarget]
  'implement-new': [target: PlanCardActionTarget]
  replan: [target: PlanCardActionTarget]
}>()

const { t } = useI18n()
const { renderMarkdown } = useChatTextRendering()
const componentId = useId()
const titleId = `plan-card-title-${componentId}`
const stepsTitleId = `plan-card-steps-${componentId}`
const bodyId = `plan-card-body-${componentId}`
const bodyElement = ref<HTMLElement | null>(null)
const bodyContentElement = ref<HTMLElement | null>(null)
const measured = ref(false)
const isCollapsible = ref(false)
const bodyHeight = ref('')
const bodyMotion = ref<'expanding' | 'collapsing' | null>(null)
const prefersReducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
const expanded = ref(readPlanDisclosureExpansion(
  props.plan.planId,
  props.plan.revisionId,
))
let bodyResizeObserver: ResizeObserver | null = null
let bodyAnimationFrame = 0
let bodyAnimationToken = 0

const renderedMarkdown = computed(() => {
  const markdown = props.plan.markdown.trim()
  if (!markdown) return ''
  // The shared renderer permits inert GFM task-list inputs in ordinary chat.
  // A PlanCard is a proposal, not an execution checklist, so remove those
  // already-sanitized inputs and leave their labels as plain list content.
  return renderMarkdown(markdown).replace(/<input\b[^>]*>/gi, '')
})
const hasBody = computed(() => Boolean(renderedMarkdown.value || props.plan.steps.length))
const likelyOverflow = computed(() => {
  const stepCharacters = props.plan.steps.reduce(
    (total, step) => total + step.title.length + (step.details?.length || 0),
    0,
  )
  return props.plan.markdown.length + stepCharacters > 520 || props.plan.steps.length > 3
})
const bodyClipped = computed(() =>
  hasBody.value
  && !expanded.value
  && (!measured.value || isCollapsible.value),
)
const bodyStyle = computed<CSSProperties>(() =>
  bodyHeight.value ? { height: bodyHeight.value } : {},
)
const actionsDisabled = computed(() => props.disabled || props.pendingAction !== null)

function collapsedBodyHeight(): number {
  const element = bodyElement.value
  if (!element) return 220
  const token = getComputedStyle(element).getPropertyValue('--plan-card-collapsed-height')
  const parsed = Number.parseFloat(token)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 220
}

function measureBodyOverflow() {
  const content = bodyContentElement.value
  if (!content || !hasBody.value) {
    measured.value = true
    isCollapsible.value = false
    return
  }
  const contentHeight = content.scrollHeight
  isCollapsible.value = contentHeight > 0
    ? contentHeight > collapsedBodyHeight() + 1
    : likelyOverflow.value
  measured.value = true
  if (!isCollapsible.value && bodyMotion.value === null) {
    bodyHeight.value = ''
  }
}

function cancelBodyAnimationFrame() {
  if (!bodyAnimationFrame) return
  cancelAnimationFrame(bodyAnimationFrame)
  bodyAnimationFrame = 0
}

async function toggleBody() {
  if (!isCollapsible.value) return
  const element = bodyElement.value
  const content = bodyContentElement.value
  if (!element || !content) return

  cancelBodyAnimationFrame()
  const token = ++bodyAnimationToken
  const nextExpanded = !expanded.value
  const currentHeight = element.getBoundingClientRect().height
  expanded.value = nextExpanded
  writePlanDisclosureExpansion(props.plan.planId, props.plan.revisionId, nextExpanded)

  if (prefersReducedMotion.value) {
    bodyMotion.value = null
    bodyHeight.value = ''
    return
  }

  bodyMotion.value = nextExpanded ? 'expanding' : 'collapsing'
  bodyHeight.value = `${currentHeight}px`
  await nextTick()
  if (token !== bodyAnimationToken) return

  bodyAnimationFrame = requestAnimationFrame(() => {
    bodyAnimationFrame = 0
    if (token !== bodyAnimationToken) return
    const targetHeight = nextExpanded
      ? content.scrollHeight
      : Math.min(content.scrollHeight, collapsedBodyHeight())
    bodyHeight.value = `${targetHeight}px`
  })
}

function finishBodyMotion(event: TransitionEvent) {
  if (event.target !== bodyElement.value || event.propertyName !== 'height') return
  bodyMotion.value = null
  bodyHeight.value = ''
  measureBodyOverflow()
}

function emitAction(action: PlanCardAction) {
  if (actionsDisabled.value) return
  const target = {
    planId: props.plan.planId,
    revisionId: props.plan.revisionId,
  }
  if (action === 'implement-current') {
    emit('implement-current', target)
  } else if (action === 'implement-new') {
    emit('implement-new', target)
  } else {
    emit('replan', target)
  }
}

function actionLabel(action: PlanCardAction, key: string): string {
  if (props.pendingAction === action) return String(t('chat.plan.working'))
  return String(t(key))
}

onMounted(async () => {
  await nextTick()
  measureBodyOverflow()
  if (typeof ResizeObserver !== 'undefined' && bodyContentElement.value) {
    bodyResizeObserver = new ResizeObserver(measureBodyOverflow)
    bodyResizeObserver.observe(bodyContentElement.value)
  }
})

watch(
  () => [props.plan.planId, props.plan.revisionId],
  async () => {
    cancelBodyAnimationFrame()
    bodyAnimationToken += 1
    bodyMotion.value = null
    bodyHeight.value = ''
    measured.value = false
    expanded.value = readPlanDisclosureExpansion(
      props.plan.planId,
      props.plan.revisionId,
    )
    await nextTick()
    measureBodyOverflow()
  },
)

onBeforeUnmount(() => {
  cancelBodyAnimationFrame()
  bodyResizeObserver?.disconnect()
  bodyResizeObserver = null
})
</script>

<style scoped>
.plan-card {
  --plan-card-collapsed-height: 220px;

  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: var(--sp-3) auto;
  padding: var(--sp-4);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: var(--radius-lg);
  background:
    linear-gradient(
      180deg,
      color-mix(in srgb, var(--bg-surface) 96%, var(--accent) 4%),
      var(--bg-surface)
    );
  box-shadow: var(--shadow-sm);
  color: var(--text);
  flex-shrink: 0;
  animation: plan-card-enter var(--dur-enter) var(--ease-out) both;
}

.plan-card--superseded {
  border-color: var(--border);
  background: var(--bg-surface);
}

.plan-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
}

.plan-card__identity {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: var(--sp-2);
}

.plan-card__header-actions {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: var(--sp-1);
}

.plan-card__icon {
  display: inline-flex;
  color: var(--accent);
}

.plan-card__eyebrow {
  color: var(--accent);
  font-size: var(--fs-xs);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.plan-card__revision {
  overflow: hidden;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plan-card__state {
  flex: 0 0 auto;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.plan-card__state--current {
  border-color: color-mix(in srgb, var(--accent) 34%, var(--border));
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  color: var(--accent);
}

.plan-card__title {
  margin: var(--sp-3) 0 var(--sp-2);
  font-size: var(--fs-lg);
  line-height: 1.35;
}

.plan-card__body {
  position: relative;
  overflow: visible;
}

.plan-card__body--clipped,
.plan-card__body--expanding,
.plan-card__body--collapsing {
  overflow: clip;
}

.plan-card__body--clipped:not(.plan-card__body--expanding) {
  height: var(--plan-card-collapsed-height);
}

.plan-card__body--expanding {
  transition: height var(--dur-enter) var(--ease-out);
}

.plan-card__body--collapsing {
  transition: height var(--dur-base) var(--ease-in);
}

.plan-card__body-fade {
  position: absolute;
  right: 0;
  bottom: 0;
  left: 0;
  height: 52px;
  pointer-events: none;
  background: linear-gradient(transparent, var(--bg-surface));
}

.plan-card__markdown {
  margin-top: var(--sp-2);
}

.plan-card__markdown :deep(ul),
.plan-card__markdown :deep(ol) {
  box-sizing: border-box;
  max-inline-size: 100%;
  margin-block: var(--sp-2);
  margin-inline: 0;
  padding-inline-start: 1.75rem;
  list-style-position: outside;
}

.plan-card__markdown :deep(li) {
  min-inline-size: 0;
  padding-inline-start: var(--sp-1);
  overflow-wrap: anywhere;
}

.plan-card__markdown :deep(li::marker) {
  color: color-mix(in srgb, var(--text-muted) 82%, var(--accent));
}

.plan-card__markdown :deep(li > ul),
.plan-card__markdown :deep(li > ol) {
  margin-block: var(--sp-1);
  padding-inline-start: 1.5rem;
}

.plan-card__steps {
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border);
}

.plan-card__steps-title {
  margin: 0 0 var(--sp-2);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.plan-card__step-list {
  display: grid;
  gap: var(--sp-2);
  margin: 0;
  padding-inline-start: 1.5rem;
}

.plan-card__step {
  min-inline-size: 0;
  padding-inline-start: var(--sp-1);
  overflow-wrap: anywhere;
}

.plan-card__step::marker {
  color: var(--accent);
  font-family: var(--font-mono);
  font-weight: 600;
}

.plan-card__step-title,
.plan-card__step-details {
  display: block;
}

.plan-card__step-title {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  line-height: 1.45;
}

.plan-card__step-details {
  margin-top: 2px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
}

.plan-card__disclosure {
  display: inline-grid;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  place-items: center;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition:
    background var(--dur-fast) var(--ease-standard),
    color var(--dur-fast) var(--ease-standard);
}

.plan-card__disclosure:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.plan-card__disclosure:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring-inset);
}

.plan-card__disclosure-icon {
  transition:
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.plan-card__disclosure:active .plan-card__disclosure-icon {
  transform: scale(0.9);
}

.plan-card__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
  padding-top: var(--sp-3);
  border-top: 1px solid var(--border);
}

@keyframes plan-card-enter {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 640px) {
  .plan-card {
    --plan-card-collapsed-height: 180px;

    width: calc(100% - 24px);
    padding: var(--sp-3);
  }

  .plan-card__header {
    align-items: flex-start;
  }

  .plan-card__revision {
    display: none;
  }

  .plan-card__markdown :deep(ul),
  .plan-card__markdown :deep(ol) {
    padding-inline-start: 1.5rem;
  }

  .plan-card__markdown :deep(li > ul),
  .plan-card__markdown :deep(li > ol) {
    padding-inline-start: 1.25rem;
  }

  .plan-card__disclosure {
    width: 44px;
    height: 44px;
  }

  .plan-card__actions .btn {
    flex: 1 1 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .plan-card {
    animation: none;
  }

  .plan-card__body--expanding,
  .plan-card__body--collapsing,
  .plan-card__disclosure,
  .plan-card__disclosure-icon {
    transition: none;
  }
}
</style>
