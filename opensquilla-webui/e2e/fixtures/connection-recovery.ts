import { computed, createApp, h, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import ChatSystemStatus from '@/components/chat/ChatSystemStatus.vue'
import ChatSessionRecoveryStatus from '@/components/chat/ChatSessionRecoveryStatus.vue'
import ChatComposer from '@/components/chat/ChatComposer.vue'
import PendingQueue from '@/components/chat/PendingQueue.vue'
import { CONNECTION_NOTICE_DELAY_MS, useDelayedNotice } from '@/composables/useDelayedNotice'
import { useChatRecoveryNotice } from '@/composables/chat/useChatRecoveryNotice'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'
import { getManifest } from '@/themes/registry'
import i18n, { loadLocaleMessages } from '@/i18n'
import '@/assets/base.css'
import '@/themes/tokens'
import '@/styles/control-visual-system.css'
import '@/styles/apple-modern.css'

// Actual components and notice timing; synthetic transport, no Gateway or profile writes.
const query = new URLSearchParams(location.search)
const locale = query.get('lang') === 'en' ? 'en' : 'zh-Hans'
await loadLocaleMessages(locale)
i18n.global.locale.value = locale
const manifest = getManifest(query.get('theme') || 'light')!
document.documentElement.dataset.theme = manifest.id
await Promise.all([manifest.world?.styles?.(), ...(manifest.world?.fonts?.map(font => font.load()) || [])])

createApp({
  setup() {
    const state = ref<'connected' | 'connecting' | 'disconnected'>('connected')
    const blocked = ref(false)
    const draft = ref('检查一下这个修改，完成后告诉我结果。')
    const label = computed(() => i18n.global.t(`chrome.connectionState.${state.value}`))
    const active = computed(() => state.value !== 'connected')
    const visible = useDelayedNotice(active, blocked, undefined, CONNECTION_NOTICE_DELAY_MS)
    const recovery = computed<ChatSessionRecoveryState | null>(() => active.value ? 'live-connecting' : null)
    const chatVisible = useChatRecoveryNotice(recovery, undefined, blocked)
    const notices = ref(0)
    const chatNotices = ref(0)
    const sends = ref(0)
    const settingsOpened = ref(false)
    const queue = [{ pendingUiId:'pending-quiet', text:'完成后运行一次验收', pendingDeliveryIdentity:'fixture-owner' }]
    let timer: ReturnType<typeof setTimeout> | undefined
    watch(visible, value => { if (value) notices.value++ }, { flush:'post' })
    watch(chatVisible, value => { if (value) chatNotices.value++ }, { flush:'post' })
    function recover() { clearTimeout(timer); blocked.value=false; state.value='connected' }
    async function interrupt(duration?:number, fatal=false) {
      recover()
      await nextTick()
      notices.value=0; chatNotices.value=0; settingsOpened.value=false
      state.value='disconnected'; blocked.value=fatal
      if (duration) timer=setTimeout(recover,duration)
    }
    onBeforeUnmount(() => clearTimeout(timer))
    return () => h('main',{class:'connection-fixture'},[
      h('h2','连接恢复 · 正式组件验收'),
      h('p','模拟连接变化；不连接真实 Gateway，不修改用户设置。'),
      h('div',{class:'fixture-controls'},[
        h('button',{onClick:()=>interrupt(1_000)},'短暂重连（1 秒）'),
        h('button',{onClick:()=>interrupt(3_000)},'短暂重连（3 秒）'),
        h('button',{onClick:()=>interrupt()},'持续断线'),
        h('button',{onClick:()=>interrupt(undefined,true)},'恢复被拒绝'),
        h('button',{onClick:recover},'恢复连接'),
      ]),
      h('section',{class:'fixture-window'},[
        h('header',{class:'topbar topbar--chat'},[
          h('span','OpenSquilla · 连接恢复验收'),
          h(ChatSystemStatus,{layout:'wide',connectionState:state.value,connectionLabel:label.value,
            connectionNoticeVisible:visible.value,approvalCount:0,canManageConnection:true,
            onOpenConnection:()=>settingsOpened.value=true}),
        ]),
        h('div',{class:'fixture-chat'},[
          h('p','正在检查项目中的修改。连接短暂恢复期间，可以继续阅读和编辑草稿。'),
          chatVisible.value && recovery.value ? h(ChatSessionRecoveryStatus,{
            state:recovery.value,transportState:state.value,action:'retry-live',onRetry:recover,
          }):null,
          h(PendingQueue,{items:queue,maxPending:8,offline:active.value,deliveryIdentity:'fixture-owner',followupAvailable:true}),
        ]),
        h('div',{class:'fixture-composer'},[h(ChatComposer,{
          modelValue:draft.value,'onUpdate:modelValue':(value:string)=>draft.value=value,
          attachments:[],selectedSkills:[],busySendMode:'queue',hasSendContent:!!draft.value,
          isStreaming:false,canStop:false,isNewLanding:false,placeholder:i18n.global.t('chat.placeholder'),
          sendButtonTitle:i18n.global.t('chat.send'),sendBlockedMessage:active.value?i18n.global.t('chat.liveSendBlockedConnecting'):'',
          showSendBlockedTooltip:active.value,
          runMode:'safe',allowedRunModes:['safe','full'],runModeLocked:false,runModeLockMessage:'',
          sessionRoutingMode:'llm_ensemble',sessionRoutingBusy:false,routerVisualEffectsEnabled:false,
          voiceBusy:false,voiceRecording:false,voiceReady:true,onSend:()=>sends.value++,
        })]),
      ]),
      h('output',{'data-testid':'evidence'},`实际连接：${state.value}；顶栏提示出现：${notices.value} 次；聊天提示出现：${chatNotices.value} 次；发送：${sends.value} 次；连接入口：${settingsOpened.value?'已打开':'未打开'}`),
    ])
  },
}).use(i18n).mount('#app')
