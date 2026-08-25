<template>
  <div
    ref="composerEl"
    class="chat-composer"
    :class="{
      'chat-composer--new-landing': isNewLanding,
      'chat-composer--collapsed': collapsed && promptAnnotations.length === 0,
      'chat-composer--floating': floating,
      'chat-composer--docked': !floating,
    }"
  >
    <div class="chat-composer-inner">
      <div v-if="attachments.length > 0" class="chat-collapse-region">
        <div class="chat-attachments">
          <div
            v-for="(att, i) in attachments"
            :key="att.local_id"
            class="attachment-chip"
            :class="{ 'attachment-chip--busy': isAttachmentBusy(att), 'attachment-chip--failed': att.kind === 'failed' }"
            :data-mime="att.mime || ''"
            :title="attachmentTitle(att)"
          >
            <span class="attachment-chip__icon" aria-hidden="true">
              <span v-if="isAttachmentBusy(att)" class="spinner attachment-chip__spinner" />
              <Icon v-else-if="att.kind === 'failed'" name="info" :size="15" />
              <img v-else-if="isImageDisplayAttachment(att) && att.dataUrl" class="attachment-chip__thumb" :src="att.dataUrl" alt="" />
              <Icon v-else :name="attachmentIcon(att)" :size="15" />
            </span>
            <span class="attachment-chip__name">{{ att.name }}</span>
            <span class="attachment-chip__meta">{{ attachmentMeta(att) }}</span>
            <button v-if="att.kind === 'failed' && att.file" class="attachment-action" :title="t('chat.retryUpload')" :aria-label="t('chat.retryUpload')" @click="emit('retryAttachment', i)">
              <Icon name="refresh" :size="12" />
            </button>
            <button class="attachment-action attachment-remove" :title="t('chat.remove')" :aria-label="t('chat.remove')" @click="emit('removeAttachment', i)">
              <Icon name="x" :size="12" />
            </button>
          </div>
        </div>
      </div>
      <div v-if="promptAnnotations.length > 0" class="chat-collapse-region">
        <div
          class="chat-prompt-annotations"
          :aria-label="t('chat.promptAnnotations.draftLabel')"
          aria-live="polite"
          data-testid="composer-prompt-annotations"
        >
          <div class="chat-prompt-annotations__header" data-testid="composer-prompt-annotations-label">
            <Icon name="chat" :size="13" aria-hidden="true" />
            <span>{{ t('chat.promptAnnotations.label') }} · {{ promptAnnotations.length }}</span>
          </div>
          <div
            v-for="annotation in promptAnnotations"
            :key="annotation.annotationId"
            class="chat-prompt-annotation-chip"
            :data-annotation-id="annotation.annotationId"
            role="group"
          >
            <span class="chat-prompt-annotation-chip__rail" aria-hidden="true" />
            <form
              v-if="editingAnnotationId === annotation.annotationId"
              class="chat-prompt-annotation-chip__editor"
              @submit.prevent="saveAnnotationEdit(annotation.annotationId)"
            >
              <input
                ref="annotationInputEl"
                v-model="annotationDraftBody"
                type="text"
                :maxlength="promptAnnotationMaxBodyLength"
                :aria-label="t('chat.promptAnnotations.editLabel')"
                @keydown.esc.prevent="cancelAnnotationEdit"
              />
              <button
                type="submit"
                :disabled="annotationDraftBody.trim().length === 0 || annotationDraftTooLong"
              >
                {{ t('common.save') }}
              </button>
              <button type="button" @click="cancelAnnotationEdit">
                {{ t('common.cancel') }}
              </button>
            </form>
            <template v-else>
              <button
                type="button"
                class="chat-prompt-annotation-chip__main"
                :title="annotation.body"
                @click="emit('jumpPromptAnnotation', annotation.annotationId)"
              >
                <span class="chat-prompt-annotation-chip__target">
                  {{ promptAnnotationTargetLabel(annotation, t) }}
                </span>
                <span class="chat-prompt-annotation-chip__text">
                  {{ annotation.body || t('chat.promptAnnotations.emptyDraft') }}
                </span>
              </button>
              <button
                type="button"
                class="attachment-action"
                :aria-label="t('chat.promptAnnotations.editLabel')"
                :title="t('chat.promptAnnotations.editLabel')"
                @click="beginAnnotationEdit(annotation)"
              >
                <Icon name="edit" :size="12" />
              </button>
              <button
                type="button"
                class="attachment-action attachment-remove"
                :aria-label="t('chat.promptAnnotations.removeLabel')"
                :title="t('chat.promptAnnotations.removeLabel')"
                @click="emit('discardPromptAnnotation', annotation.annotationId)"
              >
                <Icon name="x" :size="12" />
              </button>
            </template>
          </div>
        </div>
      </div>
      <div class="chat-input-panel">
        <div v-if="replanActive" class="chat-collapse-region">
          <div
            class="chat-replan-draft"
            role="status"
            aria-live="polite"
          >
            <span class="chat-replan-draft__icon" aria-hidden="true">
              <Icon name="pencil" :size="14" />
            </span>
            <span class="chat-replan-draft__copy">
              <strong>{{ t('chat.plan.revising') }}</strong>
              {{ t('chat.plan.reviseDraftHint') }}
            </span>
            <button
              type="button"
              class="chat-replan-draft__cancel"
              @click="emit('cancelReplan')"
            >
              {{ t('common.cancel') }}
            </button>
          </div>
        </div>
        <div class="chat-input-wrap">
          <textarea
            ref="textareaEl"
            v-model="inputText"
            class="chat-textarea"
            rows="1"
            :placeholder="placeholder"
            :disabled="inputDisabled"
            maxlength="100000"
            :aria-label="t('chat.messageToSend')"
            :aria-describedby="sendBlockedMessage ? 'chat-composer-send-status' : undefined"
            @beforeinput="emit('expand'); emit('beforeinput', $event)"
            @input="onTextareaInput"
            @keydown="emit('keydown', $event)"
            @compositionstart="emit('compositionChange', true)"
            @compositionend="emit('compositionChange', false)"
            @pointerdown="emit('expand')"
            @focus="emit('expand')"
          />
        </div>
        <div class="chat-collapse-region chat-collapse-region--footer">
          <div class="chat-input-footer">
          <div class="chat-input-actions chat-input-actions--left">
            <div ref="addMenuAnchorEl" class="chat-settings-anchor">
              <button
                class="btn btn--icon btn--ghost chat-plus-btn"
                :class="{ 'is-active': addMenuOpen }"
                :title="t('chat.add')"
                :aria-label="t('chat.add')"
                aria-haspopup="menu"
                :aria-expanded="addMenuOpen ? 'true' : 'false'"
                @click="toggleAddMenu"
              >
                <Icon name="plus" :size="18" />
              </button>
              <ChatComposerAddMenu
                v-if="addMenuOpen"
                :avoid-element="addMenuAvoidElement"
                :attachments-disabled="replanActive"
                :goal-mode-active="goalDraftArmed"
                :goal-mode-available="goalModeAvailable === true"
                :goal-mode-busy="goalModeBusy === true"
                :goal-mode-existing="goalModeExisting === true"
                :plan-mode-active="collaborationMode === 'plan'"
                :plan-mode-available="planModeAvailable === true"
                :plan-mode-busy="planModeBusy === true || planModeDisabled === true"
                @activate-goal-mode="emit('armGoal')"
                @activate-plan-mode="emit('setCollaborationMode', 'plan')"
                @attach-files="fileInputEl?.click()"
                @close="addMenuOpen = false"
              />
            </div>
            <div
              v-if="showProjectContext && projectWorkspace"
              class="chat-project-chip"
              :data-status="projectWorkspaceStatus || 'ready'"
              :title="projectWorkspace.path"
            >
              <Icon name="folder" :size="14" />
              <span class="chat-project-chip__name">{{ projectWorkspace.name }}</span>
              <span v-if="projectStatusMessage" class="chat-project-chip__status">
                {{ projectStatusMessage }}
              </span>
              <button
                v-if="canCloseProject"
                type="button"
                :aria-label="t('workspaces.closeProjectDraft')"
                :title="t('workspaces.closeProjectDraft')"
                @click="emit('closeProject')"
              >
                <Icon name="x" :size="12" />
              </button>
            </div>
            <button
              v-if="
                canChooseProject
                && isNewLanding
                && !projectWorkspace
                && (!projectWorkspaceStatus || projectWorkspaceStatus === 'none')
              "
              type="button"
              class="chat-project-choose"
              @click="emit('chooseProject')"
            >
              <Icon name="folder" :size="15" />
              <span>{{ t('workspaces.chooseProject') }}</span>
              <Icon class="chat-project-choose__chevron" name="chevronDown" :size="12" />
            </button>
            <button
              v-if="codingModeEnabled"
              type="button"
              class="chat-coding-mode-chip"
              :title="t('chat.codingMode.disableLabel')"
              :aria-label="t('chat.codingMode.disableLabel')"
              :aria-busy="codingModeSettingsBusy ? 'true' : 'false'"
              :disabled="codingModeSettingsBusy"
              @click="emit('setCodingModeEnabled', false)"
            >
              <span>{{ t('chat.codingMode.activeLabel') }}</span>
              <Icon name="x" :size="12" aria-hidden="true" />
            </button>
            <div
              v-if="sessionRoutingAvailable"
              ref="modelRoutingAnchorEl"
              class="chat-settings-anchor"
            >
              <button
                class="btn btn--icon btn--ghost chat-model-routing-btn"
                :class="[
                  `chat-model-routing-btn--${sessionRoutingMode}`,
                  { 'is-active': modelRoutingOpen || sessionRoutingMode !== 'off' },
                ]"
                :title="t('chat.composer.sessionModelRouting')"
                :aria-label="t('chat.composer.sessionModelRouting')"
                :aria-expanded="modelRoutingOpen ? 'true' : 'false'"
                :aria-disabled="sessionRoutingControlBlocked ? 'true' : 'false'"
                @click="toggleModelRouting"
              >
                <Icon name="router" :size="17" />
                <span
                  v-if="showRouterNewBadge"
                  class="chat-model-routing-btn__new"
                  aria-hidden="true"
                >{{ t('chat.composer.badgeNew') }}</span>
              </button>
              <ChatComposerModelRouting
                v-if="modelRoutingOpen"
                :model-routing-mode="sessionRoutingMode"
                :busy="sessionRoutingBusy || sessionRoutingControlBlocked"
                @close="modelRoutingOpen = false"
                @set-session-routing-mode="emit('setSessionRoutingMode', $event)"
              />
            </div>
            <div ref="runModeAnchorEl" class="chat-settings-anchor chat-run-mode-anchor">
              <button
                class="btn btn--icon btn--ghost chat-run-mode-btn"
                :class="[`chat-run-mode-btn--${runMode}`, {
                  'is-active': runModeOpen,
                  'is-locked': runModeLocked,
                }]"
                :title="runModeLocked ? undefined : t('chat.composer.runMode')"
                :aria-label="t('chat.composer.runMode')"
                :aria-expanded="runModeOpen ? 'true' : 'false'"
                :aria-disabled="runModeLocked ? 'true' : 'false'"
                :aria-describedby="runModeLocked ? 'chat-run-mode-lock-tip' : undefined"
                :disabled="runModeLocked"
                @click="toggleRunMode"
              >
                <Icon name="shield" :size="17" />
              </button>
              <span
                v-if="runModeLocked"
                id="chat-run-mode-lock-tip"
                class="chat-run-mode-lock-tip"
                role="tooltip"
              >{{ runModeLockMessage }}</span>
              <ChatComposerRunMode
                v-if="runModeOpen"
                :run-mode="runMode"
                :allowed-run-modes="allowedRunModes"
                :safe-setup-available="safeSetupAvailable"
                @close="runModeOpen = false"
                @set-run-mode="emit('setRunMode', $event)"
              />
            </div>
            <div ref="moreActionsAnchorEl" class="chat-settings-anchor">
              <button
                class="btn btn--icon btn--ghost chat-more-actions-btn"
                :class="{ 'is-active': moreActionsOpen }"
                :title="t('chrome.more')"
                :aria-label="t('chrome.more')"
                aria-haspopup="menu"
                :aria-expanded="moreActionsOpen ? 'true' : 'false'"
                @click="toggleMoreActions"
              >
                <Icon name="moreHorizontal" :size="18" />
              </button>
              <div
                v-if="moreActionsOpen"
                class="chat-more-actions-menu"
                role="menu"
                :aria-label="t('chrome.more')"
              >
                <button
                  type="button"
                  role="menuitem"
                  :class="{ 'is-active': voiceRecording, 'chat-mic--needs-setup': !voiceReady }"
                  :title="voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint')"
                  :aria-label="voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint')"
                  :disabled="voiceBusy"
                  @click="triggerVoice"
                >
                  <Icon name="microphone" :size="16" />
                  <span>{{ voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint') }}</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  :aria-label="t('chat.exportMarkdown')"
                  @click="exportConversation"
                >
                  <Icon name="download" :size="16" />
                  <span>{{ t('chat.exportMarkdown') }}</span>
                </button>
                <button
                  v-if="promptCacheKeepaliveAvailable"
                  type="button"
                  role="menuitem"
                  data-testid="chat-composer-action-keepalive"
                  :title="promptCacheKeepaliveSessionReady
                    ? t('chat.promptCacheKeepalive.action')
                    : t('chat.promptCacheKeepalive.unavailableHint')"
                  :aria-label="promptCacheKeepaliveAriaLabel"
                  :disabled="!promptCacheKeepaliveSessionReady"
                  @click="openPromptCacheKeepalive"
                >
                  <Icon name="clock" :size="16" />
                  <span class="chat-more-actions-menu__copy">
                    <span>{{ t('chat.promptCacheKeepalive.action') }}</span>
                    <small v-if="!promptCacheKeepaliveSessionReady">
                      {{ t('chat.promptCacheKeepalive.unavailableHint') }}
                    </small>
                    <small
                      v-else-if="promptCacheKeepaliveStatusText"
                      class="chat-more-actions-menu__keepalive-status"
                      :data-state="promptCacheKeepaliveStatus?.state"
                    >
                      <span class="chat-more-actions-menu__status-dot" aria-hidden="true" />
                      {{ promptCacheKeepaliveStatusText }}
                    </small>
                  </span>
                </button>
              </div>
            </div>
          </div>
          <ChatComposerGoalMode
            :active="goalDraftArmed"
            @disarm="emit('disarmGoal')"
          />
          <ChatComposerPlanMode
            :available="planModeAvailable === true"
            :mode="collaborationMode || 'default'"
            :busy="planModeBusy === true"
            :disabled="planModeDisabled === true"
            :applies-next-turn="planModeAppliesNextTurn === true"
            @set-mode="emit('setCollaborationMode', $event)"
          />
          <div class="chat-input-actions chat-input-actions--right">
            <Transition name="composer-ctl" mode="out-in">
              <button
                v-if="canStop"
                key="stop"
                class="btn btn--icon btn--danger chat-send-btn"
                :title="stopTargetsPlanRun
                  ? t('chat.planRun.stopExecutionEsc')
                  : t('chat.stopResponseEsc')"
                :aria-label="stopTargetsPlanRun
                  ? t('chat.planRun.stopExecution')
                  : t('chat.stopResponse')"
                @click="emit('stop')"
              >
                <Icon name="stop" :size="16" />
              </button>
              <button
                v-else
                key="send"
                class="btn btn--icon btn--primary chat-send-btn"
                :class="{ 'is-ready': hasSendContent && !sendBlockedMessage && !inputDisabled }"
                :title="sendBlockedMessage
                  || (sessionRoutingBusy ? t('chat.composer.routingUpdateBlocked') : sendButtonTitle)"
                :aria-label="replanActive ? t('chat.plan.reviseSend') : t('chat.send')"
                :aria-describedby="sendBlockedMessage ? 'chat-composer-send-status' : undefined"
                :aria-busy="sessionRoutingBusy ? 'true' : 'false'"
                :disabled="Boolean(sendBlockedMessage) || sessionRoutingBusy || inputDisabled"
                @click="emit('send')"
              >
                <Icon name="arrowUp" :size="17" />
              </button>
            </Transition>
          </div>
          </div>
        </div>
      </div>
      <div v-if="sendBlockedMessage" class="chat-collapse-region">
        <p
          id="chat-composer-send-status"
          class="chat-composer-send-status"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >{{ sendBlockedMessage }}</p>
      </div>
      <div class="chat-collapse-region chat-collapse-region--disclaimer">
        <p class="chat-ai-disclaimer" role="note">{{ t('chat.aiDisclaimer') }}</p>
      </div>
    </div>
    <input
      ref="fileInputEl"
      type="file"
      multiple
      class="hidden"
      @change="emit('fileChange', $event)"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { IconName } from '@/utils/icons'
import ChatComposerAddMenu from '@/components/chat/ChatComposerAddMenu.vue'
import ChatComposerGoalMode from '@/components/chat/ChatComposerGoalMode.vue'
import ChatComposerModelRouting from '@/components/chat/ChatComposerModelRouting.vue'
import ChatComposerPlanMode from '@/components/chat/ChatComposerPlanMode.vue'
import ChatComposerRunMode from '@/components/chat/ChatComposerRunMode.vue'
import type { Attachment } from '@/types/chat'
import type { ModelRoutingMode } from '@/types/modelRouting'
import type { SandboxRunMode } from '@/types/sandbox'
import type { CollaborationMode } from '@/types/plans'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'
import type { PromptAnnotation } from '@/types/promptAnnotations'
import { promptAnnotationTargetLabel } from '@/utils/chat/promptAnnotationPresentation'
import {
  PROMPT_ANNOTATION_MAX_BODY_LENGTH,
  promptAnnotationBodyWithinLimit,
} from '@/types/promptAnnotations'
import { isAttachmentBusy, isImageDisplayAttachment } from '@/utils/chat/attachments'

interface ChatComposerExpose {
  composerElement: () => HTMLElement | null
  canCollapse: () => boolean
  focusTextarea: () => void
  isTextareaFocused: () => boolean
  resizeTextarea: () => void
}

const props = withDefaults(defineProps<{
  attachments: Attachment[]
  busySendMode: 'queue' | 'steer'
  hasSendContent: boolean
  isStreaming: boolean
  canStop: boolean
  stopTargetsPlanRun?: boolean
  isNewLanding: boolean
  placeholder: string
  sendButtonTitle: string
  sendBlockedMessage?: string
  inputDisabled?: boolean
  runMode: SandboxRunMode
  allowedRunModes: SandboxRunMode[]
  safeSetupAvailable?: boolean
  runModeLocked: boolean
  runModeLockMessage: string
  sessionRoutingMode: ModelRoutingMode
  sessionRoutingBusy: boolean
  sessionRoutingControlBlocked?: boolean
  sessionRoutingAvailable?: boolean
  codingModeEnabled?: boolean
  codingModeSettingsBusy?: boolean
  addMenuAvoidElement?: HTMLElement | null
  goalDraftArmed?: boolean
  goalModeAvailable?: boolean
  goalModeBusy?: boolean
  goalModeExisting?: boolean
  voiceBusy: boolean
  voiceRecording: boolean
  voiceReady: boolean
  projectWorkspace?: { id: string; name: string; path: string } | null
  projectWorkspaceStatus?: 'none' | 'resolving' | 'ready' | 'unavailable' | 'removed' | 'unknown' | 'error'
  projectStatusMessage?: string
  promptAnnotations?: readonly PromptAnnotation[]
  canCloseProject?: boolean
  canChooseProject?: boolean
  planModeAvailable?: boolean
  collaborationMode?: CollaborationMode
  planModeBusy?: boolean
  planModeDisabled?: boolean
  planModeAppliesNextTurn?: boolean
  replanActive?: boolean
  promptCacheKeepaliveAvailable?: boolean
  promptCacheKeepaliveSessionReady?: boolean
  promptCacheKeepaliveStatus?: PromptCacheKeepaliveStatus | null
  /** Collapsed to a single-line input (floating-composer retract). */
  collapsed?: boolean
  /** Floating-composer toggle: false docks the panel (solid surface, no
      glass) even though the composer still renders in the normal layout. */
  floating?: boolean
}>(), {
  canChooseProject: true,
  codingModeEnabled: false,
  codingModeSettingsBusy: false,
  sessionRoutingAvailable: true,
  sessionRoutingControlBlocked: false,
  goalDraftArmed: false,
  inputDisabled: false,
  safeSetupAvailable: false,
  floating: false,
  promptAnnotations: () => [],
})

const emit = defineEmits<{
  beforeinput: [event: InputEvent]
  compositionChange: [value: boolean]
  fileChange: [event: Event]
  input: [event: Event]
  keydown: [event: KeyboardEvent]
  removeAttachment: [index: number]
  retryAttachment: [index: number]
  send: []
  setBusySendMode: [mode: 'queue' | 'steer']
  setRunMode: [mode: SandboxRunMode]
  setSessionRoutingMode: [mode: ModelRoutingMode]
  setCodingModeEnabled: [enabled: boolean]
  setCollaborationMode: [mode: CollaborationMode]
  armGoal: []
  disarmGoal: []
  cancelReplan: []
  voiceInput: []
  voiceSetup: []
  exportMarkdown: []
  stop: []
  chooseProject: []
  closeProject: []
  updatePromptAnnotation: [annotationId: string, body: string]
  discardPromptAnnotation: [annotationId: string]
  jumpPromptAnnotation: [annotationId: string]
  openPromptCacheKeepalive: []
  refreshPromptCacheKeepalive: []
  /** Request the parent to restore the full (expanded) composer. */
  expand: []
}>()

const { t } = useI18n()

const inputText = defineModel<string>({ required: true })
const composerEl = ref<HTMLElement | null>(null)
const textareaEl = ref<HTMLTextAreaElement | null>(null)

function onTextareaInput(event: Event) {
  // vModelText skips model updates while the element's internal IME
  // composition flag is set (`if (e.target.composing) return` in
  // @vue/runtime-dom). On Windows a paste can land while that flag is
  // stale after a composition round-trip, leaving the model — and the
  // send button's readiness — out of sync with what the textarea shows.
  //
  // A paste handler cannot repair this: in real browsers the paste event
  // fires BEFORE the default insertion mutates the DOM, so any handler
  // (or nextTick scheduled from it) still reads the empty value. The
  // input event with inputType "insertFromPaste" fires AFTER the browser
  // has written the pasted text, so syncing the model from the DOM at
  // that stage restores readiness even when vModelText skipped the
  // update. This is a no-op whenever v-model already picked up the
  // change.
  if (event instanceof InputEvent && event.inputType === 'insertFromPaste') {
    const field = event.currentTarget
    if (field instanceof HTMLTextAreaElement
      && field === textareaEl.value
      && inputText.value !== field.value) {
      inputText.value = field.value
    }
  }
  // Keep parent input consumers (auto-resize, slash commands, draft state)
  // downstream of the reconciliation so they observe the pasted model.
  emit('input', event)
}

const fileInputEl = ref<HTMLInputElement | null>(null)
const addMenuOpen = ref(false)
const modelRoutingOpen = ref(false)
const moreActionsOpen = ref(false)
const editingAnnotationId = ref('')
const annotationDraftBody = ref('')
const annotationInputEl = ref<HTMLInputElement[] | null>(null)
const promptAnnotationMaxBodyLength = PROMPT_ANNOTATION_MAX_BODY_LENGTH
const annotationDraftTooLong = computed(() => (
  !promptAnnotationBodyWithinLimit(annotationDraftBody.value)
))
const showProjectContext = computed(() =>
  Boolean(props.projectWorkspace && (props.canCloseProject || props.projectStatusMessage)),
)

function beginAnnotationEdit(annotation: PromptAnnotation) {
  editingAnnotationId.value = annotation.annotationId
  annotationDraftBody.value = annotation.body
  void nextTick(() => {
    const inputs = annotationInputEl.value
    const input = Array.isArray(inputs) ? inputs[inputs.length - 1] : inputs
    input?.focus()
    input?.select()
  })
}

function cancelAnnotationEdit() {
  editingAnnotationId.value = ''
  annotationDraftBody.value = ''
}

function saveAnnotationEdit(annotationId: string) {
  const body = annotationDraftBody.value.trim()
  if (!body || !promptAnnotationBodyWithinLimit(body)) return
  emit('updatePromptAnnotation', annotationId, body)
  cancelAnnotationEdit()
}
const promptCacheKeepaliveStatusText = computed(() => {
  const status = props.promptCacheKeepaliveStatus
  if (!status || status.state === 'off') return ''
  return t(`chat.promptCacheKeepalive.states.${status.state}`)
})
const promptCacheKeepaliveAriaLabel = computed(() => {
  const action = t('chat.promptCacheKeepalive.action')
  return promptCacheKeepaliveStatusText.value
    ? `${action}. ${promptCacheKeepaliveStatusText.value}`
    : action
})

// "NEW" badge on the routing control — the single-model AI router is now the
// default, so flag it until the user first opens the control, then never again.
const ROUTER_NEW_BADGE_KEY = 'opensquilla.composer.routerNewBadgeSeen'
const routerNewBadgeSeen = ref(false)
try {
  routerNewBadgeSeen.value = localStorage.getItem(ROUTER_NEW_BADGE_KEY) === '1'
} catch { /* localStorage unavailable */ }
const showRouterNewBadge = computed(() => !routerNewBadgeSeen.value)
function dismissRouterNewBadge() {
  if (routerNewBadgeSeen.value) return
  routerNewBadgeSeen.value = true
  try {
    localStorage.setItem(ROUTER_NEW_BADGE_KEY, '1')
  } catch { /* localStorage unavailable */ }
}
const runModeOpen = ref(false)
const addMenuAnchorEl = ref<HTMLElement | null>(null)
const modelRoutingAnchorEl = ref<HTMLElement | null>(null)
const runModeAnchorEl = ref<HTMLElement | null>(null)
const moreActionsAnchorEl = ref<HTMLElement | null>(null)

const anyPopoverOpen = computed(() =>
  addMenuOpen.value
  || modelRoutingOpen.value
  || runModeOpen.value
  || moreActionsOpen.value,
)

function eventInsideRoot(event: PointerEvent, root: HTMLElement | null): boolean {
  if (!root) return false
  const path = typeof event.composedPath === 'function' ? event.composedPath() : []
  if (path.includes(root)) return true
  return event.target instanceof Node && root.contains(event.target)
}

function closeOpenPopoversFromOutside(event: PointerEvent) {
  if (addMenuOpen.value && !eventInsideRoot(event, addMenuAnchorEl.value)) {
    addMenuOpen.value = false
  }
  if (
    moreActionsOpen.value &&
    !eventInsideRoot(event, moreActionsAnchorEl.value)
  ) {
    moreActionsOpen.value = false
  }
  if (modelRoutingOpen.value && !eventInsideRoot(event, modelRoutingAnchorEl.value)) {
    modelRoutingOpen.value = false
  }
  if (runModeOpen.value && !eventInsideRoot(event, runModeAnchorEl.value)) {
    runModeOpen.value = false
  }
}

watch(anyPopoverOpen, (open) => {
  if (open) {
    document.addEventListener('pointerdown', closeOpenPopoversFromOutside, true)
  } else {
    document.removeEventListener('pointerdown', closeOpenPopoversFromOutside, true)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeOpenPopoversFromOutside, true)
})

function toggleModelRouting() {
  modelRoutingOpen.value = !modelRoutingOpen.value
  if (modelRoutingOpen.value) {
    dismissRouterNewBadge()
    addMenuOpen.value = false
    runModeOpen.value = false
    moreActionsOpen.value = false
  }
}

function toggleRunMode() {
  if (props.runModeLocked) return
  runModeOpen.value = !runModeOpen.value
  if (runModeOpen.value) {
    addMenuOpen.value = false
    modelRoutingOpen.value = false
    moreActionsOpen.value = false
  }
}

watch(() => props.runModeLocked, (locked) => {
  if (locked) runModeOpen.value = false
})

function closeAllPopovers() {
  addMenuOpen.value = false
  modelRoutingOpen.value = false
  runModeOpen.value = false
  moreActionsOpen.value = false
}

// The retract animation collapses the footer with overflow clipping; any open
// menu would be clipped/vanished mid-flight, so close menus the moment the
// composer starts collapsing.
watch(() => props.collapsed, (collapsed) => {
  if (collapsed) closeAllPopovers()
})

function toggleMoreActions() {
  moreActionsOpen.value = !moreActionsOpen.value
  if (moreActionsOpen.value) {
    addMenuOpen.value = false
    modelRoutingOpen.value = false
    runModeOpen.value = false
    if (
      props.promptCacheKeepaliveAvailable
      && props.promptCacheKeepaliveSessionReady
    ) {
      emit('refreshPromptCacheKeepalive')
    }
  }
}

function toggleAddMenu() {
  addMenuOpen.value = !addMenuOpen.value
  if (addMenuOpen.value) {
    moreActionsOpen.value = false
    modelRoutingOpen.value = false
    runModeOpen.value = false
  }
}

function triggerVoice() {
  moreActionsOpen.value = false
  if (props.voiceReady) {
    emit('voiceInput')
  } else {
    emit('voiceSetup')
  }
}

function exportConversation() {
  moreActionsOpen.value = false
  emit('exportMarkdown')
}

function openPromptCacheKeepalive() {
  if (!props.promptCacheKeepaliveSessionReady) return
  moreActionsOpen.value = false
  emit('openPromptCacheKeepalive')
}

function attachmentIcon(att: Attachment): IconName {
  return isImageDisplayAttachment(att) ? 'image' : 'fileText'
}

function attachmentMeta(att: Attachment): string {
  if (att.kind === 'failed') {
    const failed = t('chat.status.failed')
    return att.error ? `${failed} · ${att.error}` : failed
  }
  const mime = att.mime || ''
  const subtype = mime.includes('/') ? mime.split('/')[1] : mime
  const label = subtype ? subtype.toUpperCase() : t('chat.fileLabel')
  const size = typeof att.size === 'number'
    ? `${Math.max(1, Math.round(att.size / 1024))} KB`
    : ''
  return [label, size].filter(Boolean).join(' · ')
}

function attachmentTitle(att: Attachment): string {
  if (att.kind === 'failed') {
    return att.error ? `${att.name}: ${att.error}` : t('chat.toast.uploadFailed', { name: att.name })
  }
  return att.name
}

function composerElement(): HTMLElement | null {
  return composerEl.value
}

function canCollapse(): boolean {
  const activeElement = document.activeElement
  return !anyPopoverOpen.value
    && (
      !activeElement
      || activeElement === textareaEl.value
      || !composerEl.value?.contains(activeElement)
    )
}

function documentCanReceiveFocus(): boolean {
  return document.visibilityState === 'visible' && document.hasFocus()
}

function focusTextarea() {
  // Programmatic focus must never reactivate a background/minimized browser
  // window. Recheck inside nextTick because the page can lose focus between
  // scheduling and execution (GitHub issue 382).
  if (!documentCanReceiveFocus()) return
  nextTick(() => {
    if (documentCanReceiveFocus()) textareaEl.value?.focus()
  })
}

function isTextareaFocused(): boolean {
  return document.activeElement === textareaEl.value
}

function resizeTextarea() {
  nextTick(() => {
    const ta = textareaEl.value
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'
  })
}

defineExpose<ChatComposerExpose>({
  composerElement,
  canCollapse,
  focusTextarea,
  isTextareaFocused,
  resizeTextarea,
})
</script>

<style scoped>
.hidden {
  display: none !important;
}

.chat-composer {
  padding: 0.75rem 1.5rem 1.875rem;
  border-top: 0;
  background: var(--bg-surface);
  flex-shrink: 0;
}

.chat-composer--floating {
  /* No bottom-bar band: leave breathing room around the glass card while the
     disabled preference retains the established docked surface exactly. */
  padding: 0.5rem 1.5rem 1.25rem;
  background: transparent;
}

.chat-composer--new-landing {
  width: min(calc(100% - 48px), 820px);
  margin: 0 auto;
  padding: 0;
  background: transparent;
}

.chat-composer-inner {
  width: min(100%, var(--composer-col, 820px));
  margin: 0 auto;
}

.chat-project-chip {
  flex: 0 1 auto;
  min-width: 0;
  width: fit-content;
  max-width: min(220px, 42vw);
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  padding: 3px 6px;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.chat-project-chip > .icon {
  flex: 0 0 auto;
  color: var(--text-dim);
}
.chat-project-chip__name {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 400;
}
.chat-project-chip__status {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--warning, var(--text-muted));
  font-size: var(--fs-xs);
}
.chat-project-chip button {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.chat-project-chip button:hover,
.chat-project-chip button:focus-visible {
  outline: 0;
  background: var(--bg-hover);
  color: var(--text);
}
.chat-project-chip[data-status="unavailable"],
.chat-project-chip[data-status="removed"],
.chat-project-chip[data-status="error"] {
  background: color-mix(in srgb, var(--warn) 7%, transparent);
}

.chat-coding-mode-chip {
  flex: 0 0 auto;
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 7px 3px 9px;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  color: var(--accent);
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 650;
  line-height: 1;
  cursor: pointer;
  transition:
    border-color var(--dur-fast),
    background var(--dur-fast),
    color var(--dur-fast);
}
.chat-coding-mode-chip:hover,
.chat-coding-mode-chip:focus-visible {
  outline: 0;
  border-color: color-mix(in srgb, var(--accent) 48%, transparent);
  background: color-mix(in srgb, var(--accent) 15%, transparent);
  color: var(--accent-hover);
}
.chat-coding-mode-chip:focus-visible {
  box-shadow: var(--focus-ring);
}
.chat-coding-mode-chip:disabled {
  cursor: default;
  opacity: var(--state-disabled-opacity);
}
.chat-project-choose {
  flex-shrink: 0;
  max-width: 100%;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  padding: 3px 7px;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 600;
  cursor: pointer;
  transition:
    color var(--dur-fast),
    background var(--dur-fast),
    transform var(--dur-fast);
}
.chat-project-choose > .icon:first-child { color: var(--accent); }
.chat-project-choose__chevron {
  opacity: 0.55;
  transition: opacity var(--dur-fast);
}
.chat-project-choose:hover,
.chat-project-choose:focus-visible {
  color: var(--text);
  background: color-mix(in srgb, var(--accent) 7%, transparent);
}
.chat-project-choose:hover .chat-project-choose__chevron,
.chat-project-choose:focus-visible .chat-project-choose__chevron {
  opacity: 0.9;
}

.chat-composer--new-landing .chat-composer-inner {
  width: 100%;
}

.chat-ai-disclaimer {
  margin: 0.5rem 0 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  text-align: center;
}

/* Transcript content deliberately scrolls underneath the floating dock. The
   input panel owns its glass surface, but the disclaimer sits outside that
   panel; give the note its own content-sized surface so translated/wrapped
   copy never collides visually with a message passing behind it. */
.chat-composer--floating .chat-collapse-region--disclaimer > .chat-ai-disclaimer {
  width: fit-content;
  max-width: 100%;
  margin-inline: auto;
  padding: 0.25rem 0.75rem;
  border: 1px solid color-mix(in srgb, var(--border) 68%, transparent);
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--bg-surface) 92%, transparent);
  box-shadow: 0 8px 20px -16px color-mix(in srgb, var(--text) 32%, transparent);
  -webkit-backdrop-filter: blur(16px) saturate(125%);
  backdrop-filter: blur(16px) saturate(125%);
}

@supports not ((-webkit-backdrop-filter: blur(1px)) or (backdrop-filter: blur(1px))) {
  .chat-composer--floating .chat-collapse-region--disclaimer > .chat-ai-disclaimer {
    background: var(--bg-surface);
  }
}

@media (prefers-reduced-transparency: reduce) {
  .chat-composer--floating .chat-collapse-region--disclaimer > .chat-ai-disclaimer {
    background: var(--bg-surface);
    -webkit-backdrop-filter: none;
    backdrop-filter: none;
  }
}

.chat-composer-send-status {
  margin: 0.5rem 0 0;
  color: var(--warning, var(--text-muted));
  font-size: var(--fs-sm);
  line-height: 1.5;
  text-align: center;
}

.chat-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 0.375rem;
  margin-bottom: 0.5rem;
}

.chat-prompt-annotations {
  display: grid;
  max-height: 8.75rem;
  overflow-y: auto;
  padding: 0.375rem 0.75rem 0;
}

.chat-prompt-annotations__header {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  min-height: 1.5rem;
  margin-bottom: 0.25rem;
  color: var(--text-dim);
  font-size: var(--fs-xs);
  line-height: 1.3;
}

.chat-prompt-annotation-chip {
  display: grid;
  grid-template-columns: 3px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
  min-height: 2.75rem;
  padding: 0.375rem 0;
  border-bottom: 1px solid var(--border);
  color: var(--text);
}

.chat-prompt-annotations__header + .chat-prompt-annotation-chip {
  border-top: 1px solid var(--border);
}

.chat-prompt-annotation-chip__rail {
  width: 3px;
  align-self: stretch;
  border-radius: var(--radius-full);
  background: var(--accent);
}

.chat-prompt-annotation-chip__main {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  min-width: 0;
  gap: 0.5rem;
  padding: 0.25rem 0;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.chat-prompt-annotation-chip__main:hover,
.chat-prompt-annotation-chip__main:focus-visible {
  outline: 0;
  color: var(--accent);
}

.chat-prompt-annotation-chip__target,
.chat-prompt-annotation-chip__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-prompt-annotation-chip__target {
  padding: 0.125rem 0.375rem;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.chat-prompt-annotation-chip__text {
  color: var(--text);
  font-size: var(--fs-sm);
}

.chat-prompt-annotation-chip__editor {
  display: grid;
  grid-column: 2 / -1;
  grid-template-columns: minmax(8rem, 1fr) auto auto;
  gap: 0.25rem;
}

.chat-prompt-annotation-chip__editor input {
  min-width: 0;
  padding: 0.375rem 0.5rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
  color: var(--text);
}

.chat-prompt-annotation-chip__editor button {
  padding: 0.25rem 0.5rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-muted);
  cursor: pointer;
}

.chat-prompt-annotation-chip > .attachment-action {
  width: 2rem;
  height: 2rem;
  flex-basis: 2rem;
  border-radius: var(--radius-control);
}

.chat-prompt-annotation-chip > .attachment-action:hover,
.chat-prompt-annotation-chip > .attachment-action:focus-visible {
  outline: 0;
  background: var(--bg-hover);
}

@media (max-width: 600px) {
  .chat-prompt-annotation-chip__main {
    grid-template-columns: minmax(0, 1fr);
  }

  .chat-prompt-annotation-chip__target {
    display: none;
  }

}

.attachment-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.25rem 0.5rem;
  max-width: min(100%, 360px);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  font-size: 0.8125rem;
}

.attachment-chip--busy {
  opacity: 0.7;
}

.attachment-chip--failed {
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-elevated));
}

.attachment-chip--failed .attachment-chip__icon,
.attachment-chip--failed .attachment-chip__meta {
  color: var(--danger);
}

.attachment-chip__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  color: var(--text-muted);
}

.attachment-chip__thumb {
  width: 16px;
  height: 16px;
  border-radius: var(--radius-sm);
  object-fit: cover;
}

.attachment-chip__spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--text-muted);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.attachment-chip__name {
  font-weight: 500;
  max-width: 150px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-chip__meta {
  color: var(--text-dim);
  font-size: 0.6875rem;
  min-width: 0;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 16px;
  padding: 0;
  width: 16px;
  height: 16px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--text-muted);
  font-size: 0.875rem;
}

.attachment-action:hover {
  color: var(--text);
}

.chat-input-panel {
  display: flex;
  flex-direction: column;
  min-height: 128px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xs);
  position: relative;
}

/* Glass is opt-in. ChatComposer is also reused outside ChatView, where an
   opaque panel remains the compatibility-safe default. */
.chat-composer--floating .chat-input-panel {
  background: color-mix(in srgb, var(--bg-surface) 72%, transparent);
  -webkit-backdrop-filter: blur(16px) saturate(140%);
  backdrop-filter: blur(16px) saturate(140%);
  box-shadow: var(--shadow-lg);
}

@supports not ((-webkit-backdrop-filter: blur(1px)) or (backdrop-filter: blur(1px))) {
  .chat-composer--floating .chat-input-panel {
    background: var(--bg-surface);
  }
}

@media (prefers-reduced-transparency: reduce) {
  .chat-composer--floating .chat-input-panel {
    background: var(--bg-surface);
    -webkit-backdrop-filter: none;
    backdrop-filter: none;
  }
}

.chat-replan-draft {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  border-radius: var(--radius-modal) var(--radius-modal) 0 0;
  background: color-mix(in srgb, var(--accent) 6%, transparent);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.chat-replan-draft__icon {
  display: inline-flex;
  color: var(--accent);
}

.chat-replan-draft__copy {
  min-width: 0;
}

.chat-replan-draft__copy strong {
  margin-right: var(--sp-1);
  color: var(--text);
}

.chat-replan-draft__cancel {
  padding: 2px 7px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
}

.chat-replan-draft__cancel:hover,
.chat-replan-draft__cancel:focus-visible {
  border-color: var(--border);
  background: var(--bg-hover);
  color: var(--text);
  outline: none;
}

.chat-replan-draft__cancel:focus-visible {
  box-shadow: var(--focus-ring);
}

.chat-composer--new-landing .chat-input-panel {
  min-height: 168px;
  border-color: var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-lg);
}

/* Floating-composer retract: collapse to a single-line input with nothing
   else. Every region animates (opacity/height/transform) instead of snapping,
   and the composer's own padding tightens so the bar reads as one slim line. */
.chat-composer {
  transition: padding var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1);
}

.chat-composer--floating.chat-composer--collapsed {
  padding-bottom: 0.5rem;
}

/* Floating-composer toggle off (docked layout): the panel is a solid surface
   — no glass to read the transcript through. !important beats apple-modern's
   `#app .chat .chat-input-panel:focus-within` id-scoped surface. */
.chat-composer--docked .chat-input-panel {
  background: var(--bg-surface) !important;
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
}

/* A one-row grid animates to zero without imposing an arbitrary max-height on
   attachments or status copy. Expanded content therefore keeps its natural
   height, including on narrow screens and with many attachments. */
.chat-collapse-region {
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  opacity: 1;
  overflow: hidden;
  transition:
    grid-template-rows var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    visibility 0s linear;
}

.chat-collapse-region > * {
  min-height: 0;
}

/* Composer menus are positioned above their anchors and must escape the
   animation wrapper while the footer is fully expanded. */
.chat-composer:not(.chat-composer--collapsed) .chat-collapse-region--footer {
  overflow: visible;
}

.chat-composer--collapsed .chat-collapse-region {
  grid-template-rows: minmax(0, 0fr);
  opacity: 0;
  transform: translateY(6px);
  visibility: hidden;
  pointer-events: none;
  overflow: hidden;
  transition:
    grid-template-rows var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    visibility 0s linear var(--dur-enter);
}

.chat-input-panel {
  /* !important: theme files (apple-modern) declare their own min-height and
     transition with higher specificity; the retract must win either way. */
  transition:
    min-height var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    border-color var(--dur-base) var(--ease-out),
    box-shadow var(--dur-base) var(--ease-out) !important;
}

.chat-composer--collapsed .chat-input-panel {
  min-height: 0 !important;
}

.chat-textarea {
  transition:
    min-height var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    max-height var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1),
    padding var(--dur-enter) cubic-bezier(0.4, 0, 0.2, 1);
}

.chat-composer--collapsed .chat-textarea {
  min-height: 0;
  max-height: 2.5rem;
  padding: 0.4375rem 1rem;
  overflow-y: hidden;
}

.chat-composer--new-landing .chat-input-panel:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-xl);
}

.chat-input-footer,
.chat-input-actions {
  display: flex;
  align-items: center;
}

.chat-input-footer {
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.25rem 0.625rem 0.625rem;
}

.chat-input-actions {
  gap: 0.25rem;
  min-width: 0;
}

.chat-settings-anchor {
  position: relative;
  display: inline-flex;
}

.chat-more-actions-menu {
  position: absolute;
  z-index: 20;
  bottom: calc(100% + 0.5rem);
  left: 0;
  width: max-content;
  min-width: 210px;
  padding: 0.375rem;
  border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--bg-surface) 96%, transparent);
  box-shadow: var(--shadow-lg);
  backdrop-filter: blur(16px);
}

.chat-more-actions-menu button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 36px;
  gap: 0.625rem;
  padding: 0.5rem 0.625rem;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
}

.chat-more-actions-menu button:hover,
.chat-more-actions-menu button:focus-visible,
.chat-more-actions-menu button.is-active {
  outline: 0;
  background: var(--bg-hover);
  color: var(--text);
}

.chat-more-actions-menu button:disabled {
  cursor: default;
  opacity: var(--state-disabled-opacity);
}

.chat-more-actions-menu__copy,
.chat-more-actions-menu__copy small {
  display: block;
}

.chat-more-actions-menu__copy small {
  margin-top: 2px;
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.chat-more-actions-menu__keepalive-status {
  align-items: center;
  display: flex !important;
  gap: 0.375rem;
}

.chat-more-actions-menu__status-dot {
  width: 0.375rem;
  height: 0.375rem;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--text-dim);
}

.chat-more-actions-menu__keepalive-status[data-state='scheduled']
  .chat-more-actions-menu__status-dot,
.chat-more-actions-menu__keepalive-status[data-state='probing']
  .chat-more-actions-menu__status-dot {
  background: var(--accent);
}

.chat-more-actions-menu__keepalive-status[data-state='stopped']
  .chat-more-actions-menu__status-dot {
  background: var(--danger);
}

.chat-input-actions--right {
  flex-shrink: 0;
}

.chat-input-wrap {
  flex: 1;
  min-width: 0;
  display: flex;
}

.chat-textarea {
  width: 100%;
  min-height: 68px;
  max-height: 160px;
  padding: 1rem 1rem 0.375rem;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text);
  font-size: 0.9375rem;
  line-height: 1.5;
  resize: none;
  outline: none;
  font-family: inherit;
}

.chat-composer--new-landing .chat-textarea {
  min-height: 108px;
  padding: 1.25rem 1.5rem 0.5rem;
  font-size: 1rem;
}

.chat-textarea:focus {
  border-color: transparent;
  box-shadow: none;
}

.chat-input-panel:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-sm);
}

.btn--icon {
  width: 34px;
  height: 34px;
  min-width: 34px;
  min-height: 34px;
  border-radius: var(--radius-full);
  padding: 0;
}

.chat-plus-btn {
  color: var(--text-muted);
}

.btn--ghost.is-active {
  background: color-mix(in srgb, var(--ok) 12%, var(--bg-surface));
  color: var(--ok);
}

/* Voice not configured: keep the button clickable (it routes to setup) but
   dim it so it still reads as "not active"; brighten on hover to invite it. */
.chat-mic--needs-setup {
  opacity: var(--state-disabled-opacity);
}

.chat-mic--needs-setup:hover {
  opacity: 1;
}

.chat-model-routing-btn {
  position: relative;
  border-color: transparent;
  background: transparent;
  color: var(--text-muted);
}

.chat-model-routing-btn__new {
  position: absolute;
  top: -3px;
  right: -5px;
  padding: 1px 4px;
  border-radius: 999px;
  background: var(--accent);
  color: var(--bg-surface);
  font-size: 8px;
  font-weight: 600;
  line-height: 1.25;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  pointer-events: none;
}

.chat-model-routing-btn.btn--ghost:not(:disabled):hover {
  border-color: color-mix(in srgb, var(--accent) 18%, transparent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-surface));
  color: var(--accent);
}

.chat-model-routing-btn--off.btn--ghost:not(:disabled):hover {
  border-color: color-mix(in srgb, var(--text-dim) 14%, transparent);
  background: color-mix(in srgb, var(--text-dim) 6%, var(--bg-surface));
  color: var(--text-muted);
}

.chat-model-routing-btn.btn--ghost.is-active {
  border-color: color-mix(in srgb, var(--accent) 24%, transparent);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  color: var(--accent);
}

.chat-model-routing-btn--off.btn--ghost.is-active {
  border-color: color-mix(in srgb, var(--text-dim) 18%, transparent);
  background: color-mix(in srgb, var(--text-dim) 8%, var(--bg-surface));
  color: var(--text-muted);
}

.chat-model-routing-btn--squilla_router.btn--ghost.is-active::after {
  content: "";
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 6px;
  height: 2px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 62%, transparent);
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active {
  border-color: color-mix(in srgb, var(--accent) 30%, transparent);
  background: color-mix(in srgb, var(--accent) 11%, var(--bg-surface));
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::before,
.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::after {
  content: "";
  position: absolute;
  bottom: 6px;
  width: 6px;
  height: 2px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--accent) 62%, transparent);
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::before {
  left: 10px;
}

.chat-model-routing-btn--llm_ensemble.btn--ghost.is-active::after {
  right: 10px;
}

.chat-run-mode-btn {
  --run-mode-tone: var(--text-muted);
  --run-mode-tint: transparent;
  --run-mode-border: transparent;
  --run-mode-marker: var(--text-dim);
  position: relative;
  border-color: var(--run-mode-border);
  background: var(--run-mode-tint);
  color: var(--run-mode-tone);
}

.chat-run-mode-btn::after {
  content: "";
  position: absolute;
  right: 7px;
  bottom: 7px;
  width: 6px;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--run-mode-marker);
  box-shadow: 0 0 0 2px var(--bg-surface);
}

.chat-run-mode-btn--safe {
  --run-mode-tone: var(--ok);
  --run-mode-tint: color-mix(in srgb, var(--ok) 12%, var(--bg-surface));
  --run-mode-border: color-mix(in srgb, var(--ok) 34%, transparent);
  --run-mode-marker: var(--ok);
}

.chat-run-mode-btn--full {
  --run-mode-tone: color-mix(in srgb, var(--warn) 72%, var(--text-muted));
  --run-mode-tint: color-mix(in srgb, var(--warn) 5%, var(--bg-surface));
  --run-mode-border: color-mix(in srgb, var(--warn) 18%, transparent);
  --run-mode-marker: color-mix(in srgb, var(--warn-fill) 70%, var(--text-dim));
}

.chat-run-mode-btn.btn--ghost:not(:disabled):hover,
.chat-run-mode-btn.btn--ghost.is-active {
  border-color: var(--run-mode-border);
  background: color-mix(in srgb, var(--run-mode-marker) 16%, var(--bg-surface));
  color: var(--run-mode-tone);
}

.chat-run-mode-btn.is-locked {
  opacity: 0.46;
  filter: grayscale(0.6);
  cursor: default;
}

.chat-run-mode-lock-tip {
  position: absolute;
  bottom: calc(100% + 10px);
  left: 50%;
  z-index: 220;
  width: max-content;
  max-width: min(240px, calc(100vw - 24px));
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
  color: var(--text);
  box-shadow: var(--shadow-md);
  font-size: var(--fs-xs);
  font-weight: 500;
  line-height: 1.35;
  white-space: nowrap;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transform: translate(-50%, 3px) scale(0.98);
  transform-origin: bottom center;
  transition:
    opacity var(--dur-fast) var(--ease-out),
    transform var(--dur-fast) var(--ease-out),
    visibility 0s linear var(--dur-fast);
}

.chat-run-mode-lock-tip::after {
  content: "";
  position: absolute;
  top: 100%;
  left: 50%;
  width: 7px;
  height: 7px;
  border-right: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
  transform: translate(-50%, -4px) rotate(45deg);
}

.chat-run-mode-anchor:hover > .chat-run-mode-lock-tip {
  opacity: 1;
  visibility: visible;
  transform: translate(-50%, 0) scale(1);
  transition-delay: var(--dur-base), var(--dur-base), 0s;
}

.chat-send-btn.btn--primary {
  background: var(--bg-hover);
  color: var(--text-dim);
  border-color: var(--bg-hover);
}

.chat-send-btn.btn--primary:hover {
  background: var(--bg-hover);
  border-color: var(--bg-hover);
}

.chat-send-btn.btn--primary.is-ready {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-foreground);
}

.chat-send-btn.btn--primary.is-ready:hover {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
}

/* A routing CAS is usually shorter than a visual transition. Keep the send
   button stable while native disabled semantics prevent submission. */
.chat-send-btn[aria-busy='true']:disabled {
  cursor: progress;
  opacity: 1;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Streaming-only controls (Stop, Queue/Steer) ease in/out at turn boundaries
   instead of popping into the action cluster. */
.composer-ctl-enter-active,
.composer-ctl-leave-active {
  transition: opacity var(--dur-fast) var(--ease-out),
              transform var(--dur-fast) var(--ease-out);
}
.composer-ctl-enter-from,
.composer-ctl-leave-to {
  opacity: 0;
  transform: scale(0.9);
}

@media (prefers-reduced-motion: reduce) {
  .chat-composer,
  .chat-collapse-region,
  .chat-input-panel,
  .chat-textarea {
    transition: none !important;
  }

  .chat-run-mode-lock-tip {
    transition: none;
  }

  .composer-ctl-enter-active,
  .composer-ctl-leave-active {
    transition: none;
  }
}

@media (max-width: 768px) {
  .chat-composer {
    padding: 0.5rem 0.75rem;
  }

  .chat-composer--floating {
    padding: 0.5rem 0.75rem calc(0.875rem + env(safe-area-inset-bottom, 0px));
  }

  .chat-composer--floating.chat-composer--collapsed {
    padding-bottom: calc(0.5rem + env(safe-area-inset-bottom, 0px));
  }
}

@media (hover: none) {
  .chat-run-mode-lock-tip {
    display: none;
  }
}
</style>
