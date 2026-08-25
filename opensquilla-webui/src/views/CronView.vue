<template>
  <div class="cron-stage control-stage">
    <header class="cron-stage__header control-stage__header cron-toolbar">
      <div class="cron-stage__title-block control-stage__title-block">
        <h1 class="cron-stage__title control-stage__title">{{ t('cronSkills.view.title') }}</h1>
        <p class="cron-stage__subtitle control-stage__subtitle">{{ t('cronSkills.view.subtitle') }}</p>
      </div>
      <div class="cron-stage__actions control-stage__actions cron-toolbar__actions">
        <div class="cron-search-wrap">
          <span class="cron-search-icon"><Icon name="search" :size="16" /></span>
          <input v-model="cronJobs.searchText.value" class="cron-search-input" type="search" :placeholder="t('cronSkills.view.searchPlaceholder')" autocomplete="off">
        </div>
        <button class="btn btn--ghost cron-toolbar__refresh" :title="t('cronSkills.view.refresh')" :disabled="refreshing" @click="refreshCron">
          <Icon name="refresh" :size="16" />
          <span class="cron-toolbar__refresh-label" aria-live="polite">
            <span :class="{ 'is-hidden': refreshing }">{{ t('cronSkills.view.refresh') }}</span>
            <span :class="{ 'is-hidden': !refreshing }">{{ t('cronSkills.view.refreshing') }}</span>
          </span>
        </button>
        <button class="btn btn--ghost" :class="{ 'is-active': bulkMode }" type="button" @click="toggleBulkMode">
          <Icon name="listChecks" :size="16" /><span>{{ bulkMode ? t('cronSkills.view.exitBulk') : t('cronSkills.view.bulkManage') }}</span>
        </button>
        <button class="btn btn--ghost" type="button" @click="overviewOpen = true">
          <Icon name="clock" :size="16" /><span>{{ t('cronSkills.view.overview') }}</span>
        </button>        <button class="btn btn--ghost" type="button" @click="openTemplateGallery">
          <Icon name="copy" :size="16" /><span>{{ t('cronSkills.view.addFromTemplate') }}</span>
        </button>
        <button class="btn btn--primary" type="button" @click="cronForm.openPanel(null)">
          <Icon name="plus" :size="16" /><span>{{ t('cronSkills.view.addAutomation') }}</span>
        </button>
      </div>
    </header>

    <section
      v-if="cronJobs.hasLoaded.value && !cronJobs.error.value"
      class="automation-launch"
      :class="{ 'automation-launch--compact': cronJobs.jobs.value.length > 0 }"
      :aria-labelledby="cronJobs.jobs.value.length > 0 ? 'automation-status-title' : 'automation-empty-title'"
    >
      <div class="automation-launch__clock" aria-hidden="true">
        <svg viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
          <defs><radialGradient id="automation-launch-glow" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="color-mix(in srgb, var(--accent) 20%, transparent)" /><stop offset="60%" stop-color="color-mix(in srgb, var(--accent) 5%, transparent)" /><stop offset="100%" stop-color="transparent" /></radialGradient></defs>
          <circle cx="60" cy="60" r="58" fill="url(#automation-launch-glow)" />
          <circle cx="60" cy="60" r="44" fill="none" stroke="currentColor" stroke-opacity="0.18" stroke-width="1" />
          <circle cx="60" cy="60" r="44" fill="none" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="2 6" class="cron-empty__ring" />
          <line v-for="deg in [0,30,60,90,120,150,180,210,240,270,300,330]" :key="deg" :x1="60 + Math.cos(deg * Math.PI / 180) * 40" :y1="60 + Math.sin(deg * Math.PI / 180) * 40" :x2="60 + Math.cos(deg * Math.PI / 180) * (deg % 90 === 0 ? 32 : 36)" :y2="60 + Math.sin(deg * Math.PI / 180) * (deg % 90 === 0 ? 32 : 36)" stroke="currentColor" :stroke-opacity="deg % 90 === 0 ? 0.5 : 0.25" :stroke-width="deg % 90 === 0 ? 1.5 : 1" />
          <line x1="60" y1="60" x2="60" y2="28" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" class="cron-empty__hand" />
          <line x1="60" y1="60" x2="84" y2="60" stroke="currentColor" stroke-opacity="0.6" stroke-width="2" stroke-linecap="round" />
          <circle cx="60" cy="60" r="3" fill="var(--accent)" />
        </svg>
      </div>
      <template v-if="cronJobs.jobs.value.length === 0">
        <h2 id="automation-empty-title" class="automation-launch__title">{{ t('cronSkills.view.heroTitle') }}</h2>
        <p class="automation-launch__hint">{{ t('cronSkills.view.heroHint') }}</p>
        <button class="automation-launch__button" type="button" @click="cronForm.openPanel(null)"><Icon name="plus" :size="15" />{{ t('cronSkills.view.addAutomation') }}</button>
      </template>
      <template v-else>
        <div class="automation-launch__status-copy">
          <div class="automation-launch__status-line">
            <span class="automation-launch__status-dot" aria-hidden="true" />
            <h2 id="automation-status-title" class="automation-launch__status-title">{{ t('cronSkills.view.runningTitle') }}</h2>
          </div>
          <p class="automation-launch__status-summary">{{ t('cronSkills.view.runningSummary', { enabled: cronJobs.enabledCount.value, total: cronJobs.jobs.value.length }) }}</p>
        </div>
        <div class="automation-launch__metric">
          <span>{{ t('cronSkills.view.nextRun') }}</span>
          <strong>{{ cronJobs.nextCountdown.value }}</strong>
        </div>
        <div class="automation-launch__metric">
          <span>{{ t('cronSkills.view.enabledLabel') }}</span>
          <strong>{{ cronJobs.enabledCount.value }} / {{ cronJobs.jobs.value.length }}</strong>
        </div>
        <div class="automation-launch__metric automation-launch__metric--activity">
          <span>{{ t('cronSkills.view.last24hRuns') }}</span>
          <strong>{{ cronJobs.last24h.value.runs }}</strong>
        </div>
      </template>
    </section>

    <Transition name="modal">
      <div v-if="overviewOpen" class="automation-template-modal" role="dialog" aria-modal="true" aria-labelledby="automation-overview-title" @click.self="overviewOpen = false">
        <section class="automation-template-modal__panel automation-overview-modal__panel">
          <header class="automation-template-modal__head">
            <div>
              <div class="automation-templates__eyebrow">{{ t('cronSkills.view.overviewEyebrow') }}</div>
              <h2 id="automation-overview-title">{{ t('cronSkills.view.overview') }}</h2>
              <p>{{ t('cronSkills.view.overviewDesc') }}</p>
            </div>
            <button class="cron-iconbtn" type="button" :title="t('common.close')" @click="overviewOpen = false"><Icon name="x" :size="18" /></button>
          </header>
              <section class="automation-overview">
                      <div class="cron-summary control-stat-grid control-stat-grid--fixed" style="--control-stat-columns: 4">
                  <div class="stat stat--hero control-stat control-stat--hero"><div class="stat-label control-stat__label">{{ t('cronSkills.view.activeSchedules') }}</div><div class="stat-value control-stat__value">{{ cronJobs.enabledCount.value }}<span class="stat-total"> / {{ cronJobs.jobs.value.length }}</span></div><div class="stat-hint control-stat__hint">{{ cronJobs.pausedCount.value ? t('cronSkills.view.pausedCount', { n: cronJobs.pausedCount.value }) : t('cronSkills.view.allEnabled') }}</div></div>
                  <div class="stat control-stat"><div class="stat-label control-stat__label">{{ t('cronSkills.view.nextRun') }}</div><div class="stat-value mono control-stat__value control-stat__value--mono">{{ cronJobs.nextCountdown.value }}</div><div class="stat-hint control-stat__hint">{{ cronJobs.nextRunHint.value }}</div></div>
                  <div class="stat control-stat"><div class="stat-label control-stat__label">{{ t('cronSkills.view.last24hRuns') }}</div><div class="stat-value control-stat__value">{{ cronJobs.last24h.value.runs }}</div><div class="stat-hint control-stat__hint"><span v-if="cronJobs.last24h.value.ok" class="cron-pos">{{ t('cronSkills.view.okCount', { n: cronJobs.last24h.value.ok }) }}</span><span v-if="cronJobs.last24h.value.ok && cronJobs.last24h.value.err"> &middot; </span><span v-if="cronJobs.last24h.value.err" class="cron-neg">{{ t('cronSkills.view.failCount', { n: cronJobs.last24h.value.err }) }}</span><span v-if="!cronJobs.last24h.value.ok && !cronJobs.last24h.value.err">{{ t('cronSkills.view.awaitingFirstRun') }}</span></div></div>
                  <div class="stat control-stat"><div class="stat-label control-stat__label">{{ t('cronSkills.view.mix') }}</div><div class="stat-value control-stat__value"><span :title="t('cronSkills.view.reminders')"><span class="stat__chip stat__chip--info">{{ cronJobs.reminderCount.value }}</span></span><span>/</span><span :title="t('cronSkills.view.agentTasks')"><span class="stat__chip stat__chip--accent">{{ cronJobs.agentTaskCount.value }}</span></span></div><div class="stat-hint control-stat__hint">{{ t('cronSkills.view.mixHint') }}</div></div>
                </div>
              </section>

              <section v-if="cronJobs.upcomingHorizon.value.length > 0" class="cron-horizon">
                <div class="cron-horizon__head"><span class="cron-horizon__title">{{ t('cronSkills.view.next12h') }}</span><span class="cron-horizon__legend"><span class="cron-horizon__dot" />{{ t('cronSkills.view.upcomingRun') }}</span></div>
                <div class="cron-horizon__rail"><button v-for="(o, i) in cronJobs.upcomingHorizon.value" :key="o.job.id" class="cron-horizon__marker" :style="{ left: horizonLeft(o.ts), '--i': i }" @click="onHorizonClick(o.job.id)"><span class="cron-horizon__marker-dot" /><span class="cron-horizon__marker-tip"><strong>{{ localizedCronJobName(o.job.name, o.job.id, t) }}</strong><em>{{ humanCountdown(new Date(o.ts), cronJobs.now.value) }}</em></span></button></div>
                <div class="cron-horizon__axis"><span v-for="h in [0, 3, 6, 9, 12]" :key="h" class="cron-horizon__tick" :style="{ left: (h / 12) * 100 + '%' }"><span class="cron-horizon__tick-line" /><span class="cron-horizon__tick-label">{{ h === 0 ? t('cronSkills.view.nowTick') : horizonTickLabel(h) }}</span></span></div>
              </section>
        </section>
      </div>
    </Transition>
    <div v-if="bulkMode" class="cron-bulkbar">
      <span class="cron-bulkbar__count">{{ t('cronSkills.view.selectedCount', { count: selectedJobIds.size }) }}</span>
      <button class="cron-bulkbar__select" type="button" @click="selectAllVisible">{{ t('cronSkills.view.selectVisible') }}</button>
      <button class="cron-bulkbar__select" type="button" @click="clearBulkSelection">{{ t('cronSkills.view.clearSelection') }}</button>
      <span class="cron-bulkbar__spacer" />
      <button class="btn btn--ghost" type="button" :disabled="selectedJobIds.size === 0 || bulkWorking" @click="bulkSetEnabled(true)">
        <Icon name="play" :size="14" />{{ t('cronSkills.list.resume') }}
      </button>
      <button class="btn btn--ghost" type="button" :disabled="selectedJobIds.size === 0 || bulkWorking" @click="bulkSetEnabled(false)">
        <Icon name="pause" :size="14" />{{ t('cronSkills.list.pause') }}
      </button>
      <button class="btn btn--ghost cron-bulkbar__danger" type="button" :disabled="selectedJobIds.size === 0 || bulkWorking" @click="bulkDeleteSelected">
        <Icon name="trash" :size="14" />{{ t('cronSkills.list.delete') }}
      </button>
    </div>
    <ErrorState
      v-if="cronJobs.error.value"
      :message="cronJobs.error.value"
      :on-retry="cronJobs.loadData"
    />

    <div v-else-if="!cronJobs.hasLoaded.value || (cronJobs.loading.value && cronJobs.jobs.value.length === 0)" class="state">
      <LoadingSpinner />
    </div>

    <CronJobList
      v-else-if="cronJobs.jobs.value.length > 0"
      :jobs="cronJobs.filteredSortedJobs.value"
      :total-jobs="cronJobs.jobs.value.length"
      :search-text="cronJobs.searchText.value"
      :view-mode="cronJobs.viewMode.value"
      :selected-id="selectedId"
      :sort-col="cronJobs.sortCol.value"
      :sort-asc="cronJobs.sortAsc.value"
      :now="cronJobs.now.value"
      :running-job-ids="runningJobIds"
      :bulk-mode="bulkMode"
      :selected-job-ids="selectedJobIds"
      :project-workspaces="cronForm.projectWorkspaces.value"
      :project-workspaces-loaded="cronForm.projectWorkspacesLoaded.value"
      @update:view-mode="cronJobs.viewMode.value = $event"
      @toggle-selection="toggleBulkSelection"
      @create="cronForm.openPanel(null)"
      @preset="cronForm.openPanel(null, $event)"
      @select="showRunHistory"
      @run="runJobAndShowHistory"
      @toggle="cronJobs.toggleJob"
      @edit="cronForm.openPanel"
      @delete="deleteJob"
      @sort="cronJobs.onSort"
    />

    <CronRunHistory
      v-if="selectedId && selectedJob"
      :job="selectedJob"
      :runs="cronRuns.runs.value"
      :loading="cronRuns.runsLoading.value"
      @close="selectedId = null"
      @open-chat="openRunChat"
    />

    <Transition name="modal">
      <div v-if="templateGalleryOpen" class="automation-template-modal" role="dialog" aria-modal="true" aria-labelledby="template-gallery-title" @click.self="templateGalleryOpen = false">
        <section class="automation-template-modal__panel">
          <header class="automation-template-modal__head">
            <div>
              <div class="automation-templates__eyebrow">{{ t('cronSkills.view.galleryEyebrow') }}</div>
              <h2 id="template-gallery-title">{{ t('cronSkills.view.addFromTemplate') }}</h2>
              <p>{{ t('cronSkills.view.templateDesc') }}</p>
            </div>
            <button class="cron-iconbtn" type="button" :title="t('common.close')" @click="templateGalleryOpen = false"><Icon name="x" :size="18" /></button>
          </header>
          <div class="automation-template-grid automation-template-grid--modal">
            <button
              v-for="template in localizedAutomationTemplates"
              :key="template.id"
              class="automation-template"
              :class="`automation-template--${template.tone}`"
              type="button"
              @click="useAutomationTemplate(template)"
            >
              <span class="automation-template__icon"><Icon :name="template.icon" :size="19" /></span>
              <span class="automation-template__body">
                <span class="automation-template__meta">{{ template.category }}</span>
                <strong>{{ template.title }}</strong>
                <span class="automation-template__description">{{ template.description }}</span>
                <span class="automation-template__schedule"><Icon name="clock" :size="13" />{{ template.scheduleLabel }}</span>
              </span>
              <span class="automation-template__add"><Icon name="plus" :size="15" /></span>
            </button>
          </div>
        </section>
      </div>
    </Transition>
    <CronJobPanel
      v-model:form="cronForm.form"
      :open="cronForm.panelOpen.value"
      :editing-job="cronForm.editingJob.value"
      :cron-explain-human="cronForm.cronExplainHuman.value"
      :cron-explain-valid="cronForm.cronExplainValid.value"
      :cron-explain-invalid="cronForm.cronExplainInvalid.value"
      :cron-explain-upcoming="cronForm.cronExplainUpcoming.value"
      :job-mode-hint="cronForm.jobModeHint.value"
      :session-target-hint="cronForm.sessionTargetHint.value"
      :show-target-session-row="cronForm.showTargetSessionRow.value"
      :target-session-label="cronForm.targetSessionLabel.value"
      :target-session-hint="cronForm.targetSessionHint.value"
      :message-label="cronForm.messageLabel.value"
      :project-workspaces="cronForm.projectWorkspaces.value"
      :project-workspaces-loading="cronForm.projectWorkspacesLoading.value"
      @close="cronForm.closePanel"
      @save="cronForm.saveJob"
      @cron-input="cronForm.renderCronExplain(cronForm.form.cron)"
      @preset="cronForm.applyPreset"
      @payload-kind-change="cronForm.onPayloadKindChange"
      @session-target-change="cronForm.onSessionTargetChange"
    />

    <CronDeleteDialog
      :open="deleteModalOpen"
      :job="deleteTarget"
      @cancel="closeDeleteDialog"
      @confirm="confirmDelete"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import CronDeleteDialog from '@/components/cron/CronDeleteDialog.vue'
import CronJobList from '@/components/cron/CronJobList.vue'
import CronJobPanel from '@/components/cron/CronJobPanel.vue'
import CronRunHistory from '@/components/cron/CronRunHistory.vue'
import { useCronForm } from '@/composables/cron/useCronForm'
import { useCronJobs } from '@/composables/cron/useCronJobs'
import { useCronRuns } from '@/composables/cron/useCronRuns'
import { useToasts } from '@/composables/useToasts'
import type { CronJob, CronPanelTemplate } from '@/types/cron'
import type { IconName } from '@/utils/icons'
import { humanCountdown } from '@/utils/cron/time'
import { localizedCronJobName, localizedCronTemplate } from '@/utils/cron/templateNames'

const router = useRouter()
const { t } = useI18n()
const { pushToast } = useToasts()
const selectedId = ref<string | null>(null)
const deleteModalOpen = ref(false)
const deleteTarget = ref<CronJob | null>(null)
const templateGalleryOpen = ref(false)
const overviewOpen = ref(false)
const bulkMode = ref(false)
const bulkWorking = ref(false)
const selectedJobIds = ref<Set<string>>(new Set())

const cronJobs = useCronJobs()
const cronRuns = useCronRuns(selectedId)
const cronForm = useCronForm({ afterSaved: cronJobs.loadData })

onActivated(() => {
  void cronForm.loadProjectWorkspaces().catch(() => undefined)
})

interface AutomationTemplate extends CronPanelTemplate {
  id: string
  title: string
  description: string
  category: string
  scheduleLabel: string
  icon: IconName
  tone: 'orange' | 'blue' | 'violet' | 'green' | 'rose' | 'cyan'
  featured?: boolean
}

const automationTemplates: AutomationTemplate[] = [
  {
    id: 'ai-daily',
    title: '每日 AI 资讯简报',
    description: '检索过去 24 小时的重要 AI 新闻，去重并生成带来源链接的中文简报。',
    category: '资讯与情报',
    scheduleLabel: '每天 08:00',
    icon: 'search',
    tone: 'orange',
    featured: true,
    name: '每日 AI 资讯简报',
    expression: '0 8 * * *',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    message: '检索过去 24 小时的重要 AI 产品、模型与行业新闻，合并重复事件并整理成面向普通用户的中文简报。检索、抓取、筛选、去重和核验必须静默完成：回复开头直接输出“过去 24 小时 AI 重要新闻简报”，禁止展示任务开始时间、检索窗口、搜索步骤、抓取过程、候选、自检、验证过程、舍弃条目或舍弃原因，也不要使用“我将”“让我”“正在”等过程性表述。每条新闻仅展示标题、简短摘要、发布时间、来源和可点击链接；链接应与当前新闻相符，不要混入其他事件的链接。优先选择官方来源或可靠媒体；发布时间能确认到日期即可，无法确认的细节不要补写。最多保留 10 条，不足时按实际数量输出。如果没有合适新闻，只写“过去 24 小时暂无值得关注的 AI 新闻”。最终回复只能包含简报正文和来源。',
  },
  {
    id: 'weekly-report',
    title: '每周工作复盘',
    description: '汇总本周任务与交付，提炼进展、风险和下周优先事项。',
    category: '效率办公',
    scheduleLabel: '每周五 17:30',
    icon: 'fileText',
    tone: 'blue',
    name: '每周工作复盘',
    expression: '30 17 * * 5',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    requiresWorkspace: true,
    message: '汇总本周已完成任务、进行中事项和主要交付物，整理为结构化周报。必须包含：本周成果、关键数据、风险与阻塞、需要协作的事项、下周三项最高优先级。对无法确认的信息明确标为待核验。',
  },
  {
    id: 'english-five',
    title: '每天 5 个英语单词',
    description: '结合例句与小测验，生成轻量、可坚持的英语学习卡片。',
    category: '学习成长',
    scheduleLabel: '每天 09:00',
    icon: 'languages',
    tone: 'violet',
    name: '每天 5 个英语单词',
    expression: '0 9 * * *',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    message: '选择 5 个实用英语单词，提供音标、中文释义、常见搭配和自然例句。最后设计 3 道简短测验，并在折叠式答案区给出答案。避免连续七天重复单词。',
  },
  {
    id: 'project-risk',
    title: '项目风险巡检',
    description: '检查延期事项、异常日志与待处理问题，输出风险等级和建议。',
    category: '项目研发',
    scheduleLabel: '工作日 10:00',
    icon: 'shield',
    tone: 'rose',
    name: '项目风险巡检',
    expression: '0 10 * * 1-5',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    requiresWorkspace: true,
    message: '仅检查当前绑定的项目空间，读取其中的项目文件、错误日志和待办记录。只报告有直接证据的项目风险，并按高、中、低风险分级，说明证据、影响范围和建议动作。缺少 Git 仓库、AGENTS.md、TOOLS.md、HEARTBEAT.md 等可选文件，以及本次巡检任务自身正在运行，均不得列为风险。不要检查 OpenSquilla 安装目录、Gateway、模型路由或系统依赖状态。没有证据的风险等级写“暂无”。不要执行删除、发布或修改生产配置等不可逆操作。',
  },
  {
    id: 'knowledge-review',
    title: '知识库周回顾',
    description: '整理本周新增笔记与会议纪要，补充标签并生成待消化清单。',
    category: '知识管理',
    scheduleLabel: '每周日 18:00',
    icon: 'skills',
    tone: 'green',
    name: '知识库周回顾',
    expression: '0 18 * * 0',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    requiresWorkspace: true,
    message: '整理本周新增的笔记、会议纪要与收藏内容。合并重复主题，提炼关键结论，建议标签和关联条目，并输出下周最值得继续消化的 5 项内容。保留原始来源路径或链接。',
  },
  {
    id: 'daily-idea',
    title: '每日灵感与冷知识',
    description: '每天送上一条可靠、有出处的知识和一个可行动的小灵感。',
    category: '轻松生活',
    scheduleLabel: '每天 12:00',
    icon: 'sun',
    tone: 'cyan',
    name: '每日灵感与冷知识',
    expression: '0 12 * * *',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    message: '提供一条有可靠来源的冷知识，以及一个能在 10 分钟内完成的小灵感或练习。内容需积极、具体、不重复；附上简短来源说明，不确定的事实不要输出。',
  },
  {
    id: 'bedtime-story',
    title: '每日儿童睡前故事',
    description: '生成一篇温暖、有启发、适合亲子共读的短篇故事。',
    category: '亲子陪伴',
    scheduleLabel: '每天 20:30',
    icon: 'moon',
    tone: 'violet',
    name: '每日儿童睡前故事',
    expression: '30 20 * * *',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    message: '创作一篇适合儿童睡前阅读的原创中文故事，阅读时长约 5 分钟。故事需要温暖、有想象力，包含一个自然但不说教的小启发；避免恐怖、暴力和危险模仿内容，结尾附 2 个亲子交流问题。',
  },
  {
    id: 'classic-movie',
    title: '经典电影推荐',
    description: '每周推荐一部高质量经典电影，并附无剧透导读。',
    category: '休闲娱乐',
    scheduleLabel: '每周六 19:00',
    icon: 'image',
    tone: 'rose',
    name: '经典电影推荐',
    expression: '0 19 * * 6',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    message: '推荐一部值得观看的经典电影，优先选择口碑稳定且有合法观看渠道的作品。提供片名、年份、国家、类型、推荐理由、适合人群和无剧透导读，并附可核验的资料来源；避免连续四周重复导演或系列。',
  },
  {
    id: 'today-in-history',
    title: '历史上的今天',
    description: '从科技、文化与社会领域挑选一件可靠的历史事件。',
    category: '每日知识',
    scheduleLabel: '每天 08:30',
    icon: 'cron',
    tone: 'blue',
    name: '历史上的今天',
    expression: '30 8 * * *',
    payloadKind: 'agent_turn',
    sessionTarget: 'isolated',
    message: '从历史上的今天挑选一件值得了解的事件，优先覆盖科技、文化或社会发展。用简洁中文说明事件背景、发生经过和长期影响，附至少两个可靠来源。若日期或细节存在争议，需要明确说明，不要虚构。',
  },
]

const localizedAutomationTemplates = computed(() =>
  automationTemplates.map(template => localizedCronTemplate(template, t)),
)

function useAutomationTemplate(template: AutomationTemplate) {
  templateGalleryOpen.value = false
  cronForm.openPanel(null, template)
}

// cronJobs.loadData is the silent useRequest refresh (no loading flag), so wrap
// it in a local flag to give the manual refresh button a busy state.
const refreshing = ref(false)
async function refreshCron() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await cronJobs.loadData()
  } finally {
    refreshing.value = false
  }
}

const selectedJob = computed(() => cronJobs.jobs.value.find(job => job.id === selectedId.value) || null)
const runningJobIds = computed(() => cronJobs.runningJobIds.value)

function openTemplateGallery() {
  templateGalleryOpen.value = true
  nextTick(() => {
    const panel = document.querySelector<HTMLElement>('.automation-template-modal__panel')
    if (panel) panel.scrollTop = 0
  })
}

function toggleBulkMode() {
  bulkMode.value = !bulkMode.value
  if (!bulkMode.value) selectedJobIds.value = new Set()
}

function toggleBulkSelection(id: string) {
  const next = new Set(selectedJobIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedJobIds.value = next
}

function selectAllVisible() {
  selectedJobIds.value = new Set(cronJobs.filteredSortedJobs.value.map(job => job.id))
}

function clearBulkSelection() {
  selectedJobIds.value = new Set()
}

async function bulkSetEnabled(enabled: boolean) {
  if (bulkWorking.value || selectedJobIds.value.size === 0) return
  bulkWorking.value = true
  try {
    const jobs = cronJobs.jobs.value.filter(job => selectedJobIds.value.has(job.id) && !!job.enabled !== enabled)
    for (const job of jobs) await cronJobs.toggleJob(job)
    clearBulkSelection()
  } finally {
    bulkWorking.value = false
  }
}

async function bulkDeleteSelected() {
  if (bulkWorking.value || selectedJobIds.value.size === 0) return
  const count = selectedJobIds.value.size
  if (!window.confirm(t('cronSkills.view.bulkDeleteConfirm', { count }))) return
  bulkWorking.value = true
  try {
    for (const id of [...selectedJobIds.value]) await cronJobs.removeJob(id)
    clearBulkSelection()
  } finally {
    bulkWorking.value = false
  }
}

function showRunHistory(id: string) {
  selectedId.value = id
}

async function runJobAndShowHistory(id: string) {
  // Make the execution observable immediately instead of asking users to
  // discover that the job name doubles as a hidden history control.
  selectedId.value = id
  await cronJobs.runJob(id)
  if (selectedId.value === id) await cronRuns.loadRuns(id)
}

function onHorizonClick(id: string) {
  selectedId.value = id
  nextTick(() => {
    const card = document.querySelector(`[data-cron-row="${CSS.escape(id)}"]`)
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

function horizonLeft(ts: number): string {
  const span = 12 * 3600 * 1000
  const left = ((ts - cronJobs.now.value) / span) * 100
  return Math.max(0, Math.min(100, left)) + '%'
}

function horizonTickLabel(h: number): string {
  const ts = cronJobs.now.value + h * 3600 * 1000
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}
function openRunChat(sessionKey: string) {
  router.push('/chat?session=' + encodeURIComponent(sessionKey))
}

function deleteJob(job: CronJob) {
  deleteTarget.value = job
  deleteModalOpen.value = true
}

function closeDeleteDialog() {
  deleteModalOpen.value = false
  deleteTarget.value = null
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  try {
    const id = deleteTarget.value.id
    await cronJobs.removeJob(id)
    if (selectedId.value === id) selectedId.value = null
  } catch (err) {
    pushToast(t('cronSkills.view.deleteFailed', { error: err instanceof Error ? err.message : String(err) }), { tone: 'danger' })
  } finally {
    closeDeleteDialog()
  }
}
</script>

<style>
.cron-stage {
  margin: 0 auto;
  max-width: 1200px;
  width: 100%;
}

.cron-toolbar {
  align-items: flex-end;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  padding-bottom: 18px;
}

.cron-toolbar__actions {
  flex-wrap: wrap;
}

.cron-toolbar__refresh-label {
  display: grid;
}

.cron-toolbar__refresh-label > span {
  grid-area: 1 / 1;
  white-space: nowrap;
}

.cron-toolbar__refresh-label > .is-hidden {
  visibility: hidden;
}

.cron-toolbar .btn.is-active {
  background: var(--bg-elevated);
  border-color: var(--border-focus);
  color: var(--accent);
}

.cron-bulkbar {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 5%, var(--bg-surface));
  border-radius: var(--radius-md);
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
}

.cron-bulkbar__count {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 650;
}

.cron-bulkbar__select {
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs);
  padding: 4px;
}

.cron-bulkbar__select:hover {
  color: var(--accent);
}

.cron-bulkbar__spacer {
  flex: 1;
}

.cron-bulkbar__danger {
  color: var(--danger);
}

.cron-bulk-check {
  accent-color: var(--accent);
  cursor: pointer;
  flex: 0 0 auto;
  height: 15px;
  margin: 0;
  width: 15px;
}

.cron-card__workspace,
.cron-table__workspace {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-xs);
  gap: 6px;
  min-width: 0;
}

.cron-card__workspace {
  padding: 0 2px;
}

.cron-card__workspace span,
.cron-table__workspace span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cron-card__workspace.is-unavailable,
.cron-table__workspace.is-unavailable {
  color: var(--danger);
}

.cron-workspace-rebind {
  background: transparent;
  border: 0;
  color: var(--accent);
  cursor: pointer;
  flex: 0 0 auto;
  font: inherit;
  font-weight: 600;
  padding: 0;
}

.cron-workspace-rebind:hover {
  text-decoration: underline;
}

.cron-table__check {
  width: 34px;
}

.automation-template-modal {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: 28px;
  position: fixed;
  z-index: 1100;
}

.automation-overview-modal__panel {
  max-width: 1040px;
}

.automation-overview-modal__panel .automation-overview {
  padding-top: 0;
}

.automation-overview-modal__panel .cron-horizon {
  margin-top: 18px;
}
.automation-template-modal__panel {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--elev-3);
  display: flex;
  flex-direction: column;
  max-height: min(760px, calc(100vh - 56px));
  max-width: 980px;
  overflow: hidden;
  width: 100%;
}

.automation-template-modal__head {
  align-items: flex-start;
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  flex: 0 0 auto;
  gap: 20px;
  justify-content: space-between;
  padding: 22px;
  position: relative;
  z-index: 1;
}

.automation-template-modal__head h2 {
  font-size: 1.125rem;
  margin: 0;
}

.automation-template-modal__head p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 5px 0 0;
}

.automation-template-grid--modal {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 18px 22px 22px;
  scrollbar-gutter: stable;
}
.cron-search-wrap {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-sizing: border-box;
  display: flex;
  flex: 0 0 232px;
  gap: 8px;
  padding: 0 12px;
  width: 232px;
}

.cron-search-icon {
  color: var(--text-dim);
}

.cron-search-input {
  background: transparent;
  border: none;
  color: var(--text);
  font-size: var(--fs-sm);
  min-width: 180px;
  outline: none;
  padding: 8px 0;
}

/* The wrapper is the single visual field; neutralize the global input border
   so focus does not create a second orange rectangle inside it. */
.cron-search-wrap {
  height: 38px;
  padding: 0 11px;
  transition:
    border-color var(--dur-fast) var(--ease-standard),
    box-shadow var(--dur-fast) var(--ease-standard);
}
.cron-search-wrap:focus-within {
  border-color: color-mix(in srgb, var(--accent) 72%, var(--border));
  box-shadow: none;
}
.cron-search-wrap:focus-within .cron-search-icon {
  color: var(--accent);
}
.cron-search-icon {
  align-items: center;
  display: inline-flex;
  flex: 0 0 18px;
  height: 18px;
  justify-content: center;
  line-height: 0;
  width: 18px;
}
#app .cron-search-wrap > input.cron-search-input,
#app .cron-search-wrap > input.cron-search-input:focus {
  appearance: none;
  background: transparent !important;
  border: 0;
  border-radius: 0;
  box-shadow: none !important;
  box-sizing: border-box;
  flex: 1 1 auto;
  height: 36px;
  line-height: 20px;
  min-height: 0;
  min-width: 0;
  outline: 0;
  padding: 8px 0;
  width: 100%;
}

#app .cron-search-wrap > input.cron-search-input::-webkit-search-decoration,
#app .cron-search-wrap > input.cron-search-input::-webkit-search-cancel-button {
  appearance: none;
}

.stat--hero {
  min-height: 104px;
}

.stat-total {
  color: var(--text-muted);
  font-size: 0.875rem;
  font-weight: 500;
}

.stat__chip {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: 0;
  display: inline-flex;
  font-family: var(--font-mono);
  font-size: 1.25rem;
  font-weight: 700;
  padding: 0;
}

.stat__chip--info {
  color: var(--accent);
}

.stat__chip--accent {
  color: var(--ok);
}

.cron-pos { color: var(--ok); }
.cron-neg { color: var(--danger); }

/*
 * A quiet WorkBuddy-like information strip: hierarchy comes from spacing,
 * type, and hairline dividers instead of four nested cards.
 */
.cron-summary.control-stat-grid {
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--accent) 5%, transparent), transparent 28%),
    color-mix(in srgb, var(--bg-surface) 70%, transparent);
  border-radius: var(--radius-lg);
  gap: 0;
  overflow: hidden;
  padding: 4px 0;
}

.cron-summary .control-stat {
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  gap: 2px;
  justify-content: center;
  min-height: 104px;
  overflow: visible;
  padding: 18px 24px;
}

.cron-summary .control-stat + .control-stat::before {
  background: linear-gradient(
    to bottom,
    transparent,
    color-mix(in srgb, var(--border) 72%, transparent) 22%,
    color-mix(in srgb, var(--border) 72%, transparent) 78%,
    transparent
  );
  bottom: 12px;
  content: "";
  left: 0;
  position: absolute;
  top: 12px;
  width: 1px;
}

.cron-summary .control-stat__label {
  align-items: center;
  display: flex;
  gap: 8px;
  margin-bottom: 7px;
}

.cron-summary .control-stat__label::before {
  background: var(--accent);
  border-radius: 999px;
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 10%, transparent);
  content: "";
  height: 6px;
  width: 6px;
}

.cron-summary .control-stat:nth-child(2) .control-stat__label::before {
  background: var(--warn);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 10%, transparent);
}

.cron-summary .control-stat:nth-child(3) .control-stat__label::before {
  background: var(--ok);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 10%, transparent);
}

.cron-summary .control-stat:nth-child(4) .control-stat__label::before {
  background: var(--text-muted);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--text-muted) 10%, transparent);
}

.cron-summary .control-stat__value {
  font-size: 1.8rem;
  letter-spacing: -0.035em;
}

.cron-summary .control-stat__value--mono {
  font-family: var(--font-display);
  font-size: 1.8rem;
}

.cron-summary .control-stat__hint {
  margin-top: 5px;
}

/* Run overview uses one typography system; only weight and scale establish
   hierarchy. This also removes the mixed display/mono faces in the counters. */
.automation-overview-modal__panel {
  font-family: var(--font-sans);
}
.automation-overview-modal__panel .automation-template-modal__head h2 {
  font-family: inherit;
  font-size: 18px;
  font-weight: 700;
  line-height: 26px;
}
.automation-overview-modal__panel .automation-template-modal__head p {
  font-family: inherit;
  font-size: 13px;
  line-height: 20px;
}
.automation-overview-modal__panel .automation-templates__eyebrow {
  font-family: inherit;
  font-size: 10px;
  line-height: 16px;
}
.automation-overview-modal__panel .cron-summary .control-stat__label {
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  line-height: 20px;
  margin-bottom: 6px;
}
.automation-overview-modal__panel .cron-summary .control-stat__label::before {
  flex: 0 0 6px;
}
.automation-overview-modal__panel .cron-summary .control-stat__value,
.automation-overview-modal__panel .cron-summary .control-stat__value--mono {
  font-family: inherit;
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 32px;
}
.automation-overview-modal__panel .cron-summary .stat-total {
  font-family: inherit;
  font-size: 13px;
  line-height: 20px;
}
.automation-overview-modal__panel .cron-summary .control-stat__hint {
  font-family: inherit;
  font-size: 12px;
  line-height: 18px;
  margin-top: 4px;
}
.automation-overview-modal__panel .stat__chip {
  font-family: inherit;
  font-size: inherit;
  line-height: inherit;
}

/* WorkBuddy-like quick-create hero */
.automation-launch {
  align-items: center;
  background:
    radial-gradient(circle at 50% 38%, color-mix(in srgb, var(--accent) 7%, transparent), transparent 34%),
    color-mix(in srgb, var(--bg-surface) 74%, transparent);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 220px;
  padding: 30px 20px;
  text-align: center;
}

.automation-launch__clock {
  color: var(--text-dim);
  height: 82px;
  margin-bottom: 8px;
  opacity: 1;
  width: 82px;
}

.automation-launch__clock svg {
  height: 100%;
  width: 100%;
}

.automation-launch--compact {
  align-items: center;
  background: color-mix(in srgb, var(--bg-surface) 82%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--border));
  display: grid;
  gap: 20px;
  grid-template-columns: auto minmax(180px, 1fr) repeat(3, minmax(88px, auto));
  min-height: 88px;
  padding: 16px 20px;
  text-align: left;
}

.automation-launch--compact .automation-launch__clock {
  height: 54px;
  margin: 0;
  width: 54px;
}

.automation-launch__status-copy {
  min-width: 0;
}

.automation-launch__status-line {
  align-items: center;
  display: flex;
  gap: 8px;
}

.automation-launch__status-dot {
  background: var(--ok);
  border-radius: var(--radius-pill);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 14%, transparent);
  flex: 0 0 auto;
  height: 7px;
  width: 7px;
}

.automation-launch__status-title {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 700;
  margin: 0;
}

.automation-launch__status-summary {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: 5px 0 0 15px;
}

.automation-launch__metric {
  border-left: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  padding-left: 16px;
}

.automation-launch__metric > span {
  color: var(--text-dim);
  font-size: 10px;
  white-space: nowrap;
}

.automation-launch__metric > strong {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 650;
  white-space: nowrap;
}

.automation-launch__title {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 600;
  margin: 0;
}

.automation-launch__hint {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  line-height: 1.5;
  margin: 7px 0 16px;
  max-width: 520px;
}

.automation-launch__button {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  color: var(--text);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  gap: 6px;
  padding: 9px 15px;
  transition: background var(--transition), border-color var(--transition), box-shadow var(--transition), transform var(--transition);
}

.automation-launch__button:hover {
  background: var(--bg-surface);
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  box-shadow: var(--elev-1);
  transform: translateY(-1px);
}

.automation-section-head h2 {
  color: var(--text);
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: -0.015em;
  margin: 0;
}

.automation-section-head p {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 4px 0 0;
}

.automation-overview {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-top: 8px;
}
/* WorkBuddy-inspired automation gallery */
.automation-templates {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 8px 0 4px;
}

.automation-templates__head {
  align-items: flex-end;
  display: flex;
  gap: 24px;
  justify-content: space-between;
}

.automation-templates__eyebrow {
  color: var(--accent);
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 0.14em;
  margin-bottom: 7px;
}

.automation-templates__title {
  color: var(--text);
  font-size: 1.125rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin: 0;
}

.automation-templates__subtitle {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.55;
  margin: 5px 0 0;
}

.automation-templates__custom {
  align-items: center;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  flex: 0 0 auto;
  font: inherit;
  font-size: var(--fs-xs);
  gap: 6px;
  padding: 6px 0;
}

.automation-templates__custom:hover {
  color: var(--accent);
}

.automation-template-grid {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.automation-template {
  align-items: flex-start;
  background: color-mix(in srgb, var(--bg-elevated) 72%, var(--bg-surface));
  border: 1px solid transparent;
  border-radius: var(--radius-lg);
  color: var(--text);
  cursor: pointer;
  display: flex;
  font: inherit;
  gap: 13px;
  min-height: 112px;
  padding: 14px 15px;
  position: relative;
  text-align: left;
  transition: background var(--transition), border-color var(--transition), box-shadow var(--transition), transform var(--transition);
}

.automation-template:hover {
  background: var(--bg-surface);
  border-color: color-mix(in srgb, var(--template-tone, var(--accent)) 30%, var(--border));
  box-shadow: 0 10px 26px color-mix(in srgb, var(--template-tone, var(--accent)) 9%, transparent);
  transform: translateY(-2px);
}

.automation-template--featured {
  background:
    radial-gradient(circle at 8% 15%, color-mix(in srgb, var(--template-tone) 15%, transparent), transparent 42%),
    color-mix(in srgb, var(--bg-elevated) 78%, var(--bg-surface));
}

.automation-template--orange { --template-tone: var(--accent); }
.automation-template--blue { --template-tone: var(--info); }
.automation-template--violet { --template-tone: color-mix(in srgb, var(--accent) 58%, var(--info)); }
.automation-template--green { --template-tone: var(--ok); }
.automation-template--rose { --template-tone: var(--danger); }
.automation-template--cyan { --template-tone: color-mix(in srgb, var(--info) 68%, var(--ok)); }

.automation-template__icon {
  align-items: center;
  background: color-mix(in srgb, var(--template-tone) 13%, transparent);
  border-radius: var(--radius-md);
  color: var(--template-tone);
  display: inline-flex;
  flex: 0 0 auto;
  height: 38px;
  justify-content: center;
  width: 38px;
}

.automation-template__body {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.automation-template__meta {
  color: var(--template-tone);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  margin-bottom: 5px;
}

.automation-template__body strong {
  font-size: var(--fs-sm);
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1.35;
}

.automation-template__description {
  color: var(--text-muted);
  display: -webkit-box;
  font-size: 11px;
  line-height: 1.55;
  margin-top: 5px;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.automation-template__schedule {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  font-family: var(--font-mono);
  font-size: 10px;
  gap: 5px;
  margin-top: auto;
  padding-top: 10px;
}

.automation-template__add {
  align-items: center;
  border-radius: 999px;
  color: var(--text-dim);
  display: inline-flex;
  height: 26px;
  justify-content: center;
  opacity: 0;
  position: absolute;
  right: 12px;
  top: 12px;
  transform: scale(0.85);
  transition: opacity var(--transition), transform var(--transition), color var(--transition), background var(--transition);
  width: 26px;
}

.automation-template:hover .automation-template__add {
  background: color-mix(in srgb, var(--template-tone) 12%, transparent);
  color: var(--template-tone);
  opacity: 1;
  transform: scale(1);
}

/* Horizon */
.cron-horizon {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--sp-4);
}

.cron-horizon__head {
  align-items: center;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  margin-bottom: var(--sp-3);
}

.cron-horizon__title {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.cron-horizon__legend {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  font-size: 11px;
  gap: 6px;
}

.cron-horizon__dot {
  background: var(--accent);
  border-radius: 50%;
  display: inline-block;
  height: 8px;
  width: 8px;
}

.cron-horizon__rail {
  height: 32px;
  position: relative;
}

.cron-horizon__marker {
  align-items: center;
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  display: flex;
  padding: 0;
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
}

.cron-horizon__marker-dot {
  background: var(--accent);
  border-radius: 50%;
  height: 10px;
  width: 10px;
}

.cron-horizon__marker-tip {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-md);
  display: none;
  font-size: 11px;
  left: 50%;
  padding: 6px 10px;
  position: absolute;
  top: -8px;
  transform: translate(-50%, -100%);
  white-space: nowrap;
  z-index: 10;
}

.cron-horizon__marker:hover .cron-horizon__marker-tip {
  display: block;
}

.cron-horizon__marker-tip strong {
  display: block;
  font-size: 12px;
}

.cron-horizon__marker-tip em {
  color: var(--text-muted);
  font-style: normal;
}

.cron-horizon__axis {
  border-top: 1px solid var(--border);
  height: 20px;
  margin-top: var(--sp-2);
  position: relative;
}

.cron-horizon__tick {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
}

.cron-horizon__tick-line {
  background: var(--border);
  display: block;
  height: 6px;
  margin: 0 auto;
  width: 1px;
}

.cron-horizon__tick-label {
  color: var(--text-dim);
  display: block;
  font-size: 10px;
  margin-top: 2px;
  text-align: center;
}

/* Minimal task cards; full controls remain available in table view. */
.cron-card {
  background: color-mix(in srgb, var(--bg-surface) 88%, var(--bg-elevated));
  border-color: color-mix(in srgb, var(--border) 76%, transparent);
  gap: 12px;
  min-height: 138px;
  padding: 15px 16px;
}

.cron-card__timing {
  align-items: center;
  display: flex;
  gap: 12px;
  justify-content: space-between;
  min-width: 0;
}

.cron-card__schedule-text {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  font-size: 12px;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cron-card__next {
  color: var(--accent);
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 650;
}

.cron-card__next.is-paused {
  color: var(--text-dim);
}

/* Jobs list */
.cron-jobs__head {
  align-items: center;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
}

.cron-jobs__title {
  font-size: var(--fs-md);
  letter-spacing: 0;
  margin: 0;
}

.cron-jobs__count {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-variant-numeric: tabular-nums;
  margin-left: 6px;
  padding: 2px 8px;
}

.cron-card.is-selected {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

.cron-card.is-imminent {
  animation: cron-pulse 2s infinite;
}

@keyframes cron-pulse {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 30%, transparent); }
  50% { box-shadow: 0 0 0 4px transparent; }
}

.cron-card__head {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
}

.cron-card__dot {
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
  height: 10px;
  width: 10px;
}

.cron-card__dot.is-on { background: var(--ok); }
.cron-card__dot.is-off { background: var(--text-dim); }
.cron-card__dot.is-error { background: var(--danger); }

.cron-card__name {
  background: none;
  border: none;
  color: var(--text);
  cursor: pointer;
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  font-weight: 650;
  overflow: hidden;
  padding: 0;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cron-card__name:hover {
  color: var(--accent);
}

.cron-pill {
  border-radius: var(--radius-sm);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.04em;
  margin-left: auto;
  padding: 2px 8px;
  text-transform: uppercase;
}

.cron-pill--is-reminder {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border));
  color: var(--accent);
}

.cron-pill--is-agent {
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}

.cron-card__schedule {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.cron-expr {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 2px 8px;
}

.cron-expr--inline {
  background: transparent;
  border: none;
  padding: 0;
}

.cron-card__human {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.cron-card__meta {
  display: grid;
  gap: var(--sp-2);
  margin: 0;
}

.cron-card__meta > div {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
}

.cron-card__meta dt {
  color: var(--text-dim);
  font-size: 13px;
  font-weight: 650;
  line-height: 1.25;
}

.cron-card__meta dd {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 0;
}

.cron-mono {
  font-family: var(--font-mono);
}

.cron-muted {
  color: var(--text-dim);
}

.cron-card__abs {
  color: var(--text-dim);
  font-size: 11px;
}

.cron-card__message {
  grid-column: 1 / -1;
}

.cron-card__message dd {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
}

.cron-card__actions {
  display: flex;
  gap: 4px;
  margin-top: auto;
  padding-top: var(--sp-2);
}

.cron-iconbtn {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  gap: 4px;
  padding: 4px 8px;
  font-size: 12px;
}

.cron-iconbtn:hover {
  background: var(--bg-elevated);
  border-color: var(--border);
  color: var(--text);
}

.cron-iconbtn:disabled {
  cursor: wait;
  opacity: 0.72;
}

.cron-iconbtn--accent:hover {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  color: var(--accent);
}

.cron-iconbtn--danger:hover {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  border-color: color-mix(in srgb, var(--danger) 40%, var(--border));
  color: var(--danger);
}

.cron-iconbtn--sm {
  padding: 2px 6px;
}

/* Table */
.cron-table-wrap {
  overflow-x: auto;
}

.cron-table {
  border-collapse: collapse;
  font-size: var(--fs-sm);
  width: 100%;
}

.cron-table th,
.cron-table td {
  border-bottom: 1px solid var(--border);
  padding: 10px 12px;
  text-align: left;
  vertical-align: middle;
}

.cron-table th {
  color: var(--text-dim);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.cron-th-sort {
  cursor: pointer;
  user-select: none;
}

.cron-th-sort:hover {
  color: var(--text);
}

.cron-table__arrow {
  color: var(--accent);
}

.cron-table tr.is-selected td {
  background: color-mix(in srgb, var(--accent) 5%, transparent);
}

.cron-table__actions {
  display: flex;
  gap: 2px;
  white-space: nowrap;
}

.cron-link {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  padding: 0;
  text-decoration: underline;
}

.cron-link:hover {
  color: var(--text);
}

/* Refined task table */
.cron-table-wrap {
  background: color-mix(in srgb, var(--bg-surface) 82%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 72%, transparent);
  border-radius: var(--radius-lg);
  overflow-x: auto;
}

.cron-table--tasks {
  min-width: 620px;
}

.cron-table--tasks th,
.cron-table--tasks td {
  padding: 14px 16px;
}

.cron-table--tasks th {
  background: color-mix(in srgb, var(--bg-elevated) 62%, transparent);
  border-bottom-color: color-mix(in srgb, var(--border) 78%, transparent);
  color: var(--text-dim);
  font-size: 10px;
  letter-spacing: 0.08em;
}

.cron-table--tasks tbody tr {
  transition: background var(--transition);
}

.cron-table--tasks tbody tr:hover td {
  background: color-mix(in srgb, var(--accent) 3.5%, transparent);
}

.cron-table--tasks tbody tr:last-child td {
  border-bottom: 0;
}

.cron-table__task {
  min-width: 180px;
}

.cron-table__task-content {
  align-items: center;
  display: flex;
  gap: 10px;
}

.cron-table--tasks .cron-link {
  color: var(--text);
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  font-weight: 650;
  max-width: 240px;
  overflow: hidden;
  text-decoration: none;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cron-table--tasks .cron-link:hover {
  color: var(--accent);
}

.cron-table__schedule {
  color: var(--text-muted);
  min-width: 150px;
  white-space: nowrap;
}

.cron-table__schedule-content {
  align-items: center;
  display: flex;
  gap: 7px;
}

.cron-table__status {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  font-size: 12px;
  gap: 7px;
  white-space: nowrap;
}

.cron-table__status-dot {
  background: var(--text-dim);
  border-radius: 50%;
  height: 7px;
  width: 7px;
}

.cron-table__status.is-enabled .cron-table__status-dot {
  background: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 10%, transparent);
}

.cron-table__time {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: nowrap;
}

.cron-table__next {
  color: var(--text-muted);
  font-weight: 400;
}

.cron-table--tasks .cron-table__actions {
  min-width: 150px;
}

.cron-table__actions-content {
  display: flex;
  gap: 2px;
  justify-content: flex-end;
  white-space: nowrap;
}

.cron-table__actions-head {
  text-align: right !important;
}

.cron-table--tasks .cron-iconbtn--sm {
  padding: 5px 7px;
}
/* Status */
.status {
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  text-transform: uppercase;
}

.status--ok {
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}

.status--err {
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
  color: var(--danger);
}

.status--off {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text-dim);
}

/* Compact task-empty state; the template gallery now carries discovery. */
.state.automation-empty {
  align-items: center;
  background:
    linear-gradient(110deg, color-mix(in srgb, var(--accent) 6%, transparent), transparent 42%),
    color-mix(in srgb, var(--bg-elevated) 56%, transparent);
  border: 0;
  border-radius: var(--radius-lg);
  display: grid;
  gap: 14px;
  grid-template-columns: auto minmax(0, 1fr);
  min-height: 0;
  padding: 20px 22px;
  text-align: left;
}

.automation-empty__icon {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius: var(--radius-md);
  color: var(--accent);
  display: inline-flex;
  height: 42px;
  justify-content: center;
  width: 42px;
}

.automation-empty__copy {
  min-width: 0;
}

.automation-empty .cron-empty__title {
  font-size: var(--fs-sm);
  margin-bottom: 3px;
}

.automation-empty .cron-empty__msg {
  font-size: var(--fs-xs);
}

.automation-empty .cron-empty__cta {
  white-space: nowrap;
}

/* Empty state */
.state {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  color: var(--text);
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-8) var(--sp-4);
  text-align: center;
}

.state-icon {
  color: var(--text-dim);
}

.state-title {
  font-size: var(--fs-lg);
  font-weight: 600;
}

.state-text {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin: 0;
  max-width: 520px;
}

.cron-empty__clock {
  color: var(--text-dim);
  height: 120px;
  width: 120px;
}

.cron-empty__clock svg {
  height: 100%;
  width: 100%;
}

.cron-empty__ring {
  animation: cron-spin 60s linear infinite;
  transform-origin: center;
}

.cron-empty__hand {
  animation: cron-spin 12s linear infinite;
  transform-origin: 60px 60px;
}

@keyframes cron-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.cron-empty__title {
  font-size: var(--fs-lg);
  font-weight: 600;
}

.cron-empty__msg {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin: 0;
}

.cron-empty__cta {
  align-items: center;
  display: inline-flex;
  gap: 6px;
}

.cron-empty__hints {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.cron-empty__hints-label {
  color: var(--text-dim);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.cron-empty-hint {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  cursor: pointer;
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
  text-align: left;
  width: 100%;
}

.cron-empty-hint:hover {
  border-color: var(--accent);
}

.cron-empty-hint code {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 2px 8px;
  white-space: nowrap;
}

.cron-empty-hint span {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

/* Panel */
.cron-panel-overlay {
  align-items: center;
  display: flex;
  inset: 0;
  justify-content: center;
  padding: 24px;
  position: fixed;
  z-index: 1000;
}

.cron-panel__scrim {
  background: var(--scrim);
  inset: 0;
  opacity: 0;
  position: fixed;
  transition: opacity var(--dur-base);
}

.cron-panel__scrim.is-open {
  opacity: 1;
}

.cron-panel {
  background: var(--bg-surface);
  border: 1px solid color-mix(in srgb, var(--border) 88%, transparent);
  border-radius: var(--radius-lg);
  box-shadow: var(--elev-3);
  display: flex;
  flex-direction: column;
  max-height: min(780px, calc(100vh - 48px));
  max-width: 620px;
  opacity: 0;
  overflow: hidden;
  position: relative;
  transform: translateY(14px) scale(0.985);
  transition: opacity var(--dur-base), transform var(--dur-base) var(--ease-out);
  width: 100%;
  z-index: 1001;
}

.cron-panel.is-open {
  opacity: 1;
  transform: translateY(0) scale(1);
}

.cron-panel__head {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-4);
}

.cron-panel__eyebrow {
  color: var(--text-dim);
  display: block;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.cron-panel__title {
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0;
}

.cron-panel__body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-4);
}

.cron-panel__actions {
  display: flex;
  gap: var(--sp-3);
  margin-top: var(--sp-4);
}

.cron-friendly-time-row {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}

.cron-friendly-time-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.cron-time-picker {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  transition: border-color var(--dur-fast), box-shadow var(--dur-fast);
}

.cron-time-picker:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
}

.cron-time-selects__separator {
  color: var(--text-muted);
  font-size: var(--fs-md);
  font-weight: 650;
  padding: 0 2px;
}

.cron-friendly-time-input {
  font-family: var(--font-sans);
  font-variant-numeric: tabular-nums;
}
.cron-friendly-time-input::-webkit-datetime-edit {
  align-items: center;
  display: inline-flex;
}
.cron-friendly-time-input::-webkit-datetime-edit-hour-field,
.cron-friendly-time-input::-webkit-datetime-edit-minute-field {
  padding: 0;
}
.cron-friendly-time-input::-webkit-datetime-edit-text {
  color: var(--text);
  padding: 0 2px;
}

/* Form fields */
.cron-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: var(--sp-3);
}

.cron-field__label {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.cron-field__input {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font-size: var(--fs-sm);
  padding: 8px 12px;
  width: 100%;
}

.cron-field__input:focus {
  border-color: var(--accent);
  outline: none;
}

.cron-field__input--mono {
  font-family: var(--font-mono);
}

.cron-field__input--textarea {
  min-height: 80px;
  resize: vertical;
}

.cron-field__hint {
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1.5;
}

/* Cron explain */
.cron-explain {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--sp-3);
}

.cron-explain.is-valid {
  border-color: var(--ok);
}

.cron-explain.is-invalid {
  border-color: var(--danger);
}

.cron-explain__human {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.cron-explain__hint {
  color: var(--text-dim);
  font-size: 12px;
  margin-top: 4px;
}

.cron-explain__upcoming {
  list-style: none;
  margin: var(--sp-2) 0 0;
  padding: 0;
}

.cron-explain__upcoming li {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  padding: 2px 0;
}

.cron-explain__num {
  color: var(--text-dim);
  font-size: 11px;
  min-width: 18px;
}

.cron-explain__abs {
  color: var(--text-dim);
  font-size: 11px;
}

/* Presets */
.cron-presets {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: var(--sp-2);
}

.cron-presets__label {
  color: var(--text-dim);
  font-size: 11px;
}

.cron-preset {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  font-size: 11px;
  padding: 2px 8px;
}

.cron-preset:hover {
  border-color: var(--accent);
  color: var(--accent);
}

/* Advanced */
.cron-advanced {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  margin-bottom: var(--sp-3);
}

.cron-advanced__summary {
  color: var(--text-muted);
  cursor: pointer;
  font-size: var(--fs-sm);
  font-weight: 500;
  padding: var(--sp-3);
  user-select: none;
}

.cron-advanced__body {
  border-top: 1px solid var(--border);
  padding: var(--sp-3);
}

.cron-advanced--nested {
  margin-top: var(--sp-3);
}

/* Toggle */
.cron-toggle {
  align-items: center;
  cursor: pointer;
  display: inline-flex;
  gap: 10px;
  margin-bottom: var(--sp-3);
}

.cron-toggle__label {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

/* Spinner */
.cron-spinner {
  animation: cron-spin 1s linear infinite;
  border: 2px solid var(--border);
  border-radius: 50%;
  border-top-color: var(--accent);
  display: inline-block;
  height: 14px;
  width: 14px;
}

/* Modal */
.modal-overlay {
  align-items: center;
  background: var(--scrim);
  bottom: 0;
  display: flex;
  justify-content: center;
  left: 0;
  position: fixed;
  right: 0;
  top: 0;
  z-index: 1100;
}

.modal {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  max-width: 420px;
  padding: var(--sp-5);
  width: 90%;
}

.modal__title {
  font-size: var(--fs-md);
  font-weight: 600;
  margin: 0 0 var(--sp-3);
}

.modal__body {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin-bottom: var(--sp-4);
}

.modal__footer {
  display: flex;
  gap: var(--sp-3);
  justify-content: flex-end;
}

@media (max-width: 640px) {
  .cron-panel-overlay {
    padding: 12px;
  }

  .cron-panel {
    border-radius: var(--radius-md);
    max-height: calc(100vh - 24px);
  }

  .cron-panel__head,
  .cron-panel__body {
    padding-left: 16px;
    padding-right: 16px;
  }
}
/* Transitions */
.panel-enter-active,
.panel-leave-active {
  transition: opacity var(--dur-base);
}

.panel-enter-from,
.panel-leave-to {
  opacity: 0;
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity var(--dur-base);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

/* Responsive */
@media (max-width: 980px) {
  .automation-launch--compact {
    grid-template-columns: auto minmax(180px, 1fr) repeat(2, minmax(88px, auto));
  }

  .automation-launch__metric--activity {
    display: none;
  }

  .automation-template-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .cron-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .cron-summary .control-stat:nth-child(3)::before {
    display: none;
  }
}

@media (max-width: 760px) {
  .automation-launch--compact {
    gap: 14px;
    grid-template-columns: auto minmax(0, 1fr);
    min-height: 78px;
    padding: 14px 16px;
  }

  .automation-launch--compact .automation-launch__clock {
    height: 46px;
    width: 46px;
  }

  .automation-launch__metric {
    display: none;
  }

  .cron-toolbar {
    align-items: stretch;
  }

  .cron-toolbar__actions {
    align-items: stretch;
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .cron-search-wrap {
    flex-basis: auto;
    grid-column: 1 / -1;
    width: 100%;
  }

  .cron-toolbar__refresh {
    display: none;
  }

  .cron-toolbar__actions .btn--primary {
    grid-column: 1 / -1;
  }

  .automation-template-modal {
    align-items: flex-end;
    padding: 0;
  }

  .automation-overview-modal__panel {
  max-width: 1040px;
}

.automation-overview-modal__panel .automation-overview {
  padding-top: 0;
}

.automation-overview-modal__panel .cron-horizon {
  margin-top: 18px;
}
.automation-template-modal__panel {
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    max-height: 88vh;
    padding: 0;
  }

  .automation-template-modal__head {
    padding: 18px;
  }

  .automation-template-grid--modal {
    padding: 14px 18px 18px;
  }

  .automation-template-grid--modal {
    grid-template-columns: 1fr;
  }

  .automation-templates__head {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .automation-template--featured {
    grid-column: span 1;
  }

  .state.automation-empty {
    align-items: flex-start;
    grid-template-columns: auto 1fr;
  }

  .automation-empty .cron-empty__cta {
    grid-column: 1 / -1;
    justify-content: center;
    width: 100%;
  }

  .cron-stage__header {
    align-items: stretch;
    flex-direction: column;
  }

  .cron-card-grid {
    grid-template-columns: 1fr;
  }

  .cron-panel {
    max-width: 100%;
  }
}

@media (max-width: 480px) {
  .automation-template-grid {
    grid-template-columns: 1fr;
  }

  .cron-summary {
    grid-template-columns: 1fr;
  }

  .cron-summary .control-stat + .control-stat::before,
  .cron-summary .control-stat:nth-child(3)::before {
    background: linear-gradient(
      to right,
      transparent,
      color-mix(in srgb, var(--border) 72%, transparent) 18%,
      color-mix(in srgb, var(--border) 72%, transparent) 82%,
      transparent
    );
    bottom: auto;
    display: block;
    height: 1px;
    left: 18px;
    right: 18px;
    top: 0;
    width: auto;
  }
}
/* Typography and icon alignment pass */
.cron-stage,
.cron-stage button,
.cron-stage input,
.cron-stage select,
.cron-stage textarea {
  font-family: var(--font-sans);
}

.cron-stage .btn,
.cron-iconbtn,
.automation-launch__button,
.cron-bulkbar__select,
.control-segmented__btn {
  align-items: center;
  line-height: 1;
}

.cron-stage .btn svg,
.cron-iconbtn svg,
.automation-launch__button svg,
.cron-card__schedule-text svg,
.cron-table__schedule-content svg {
  display: block;
  flex: 0 0 auto;
}

.cron-iconbtn {
  font-family: var(--font-sans);
  justify-content: center;
  line-height: 1;
}

.cron-card__actions .cron-iconbtn,
.cron-table__actions-content .cron-iconbtn {
  height: 30px;
  padding: 0;
  width: 30px;
}

.control-segmented__btn {
  display: inline-flex;
  font-size: 13px;
  justify-content: center;
}

.cron-table--tasks th {
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
  line-height: 1.4;
  text-transform: none;
}

.cron-table--tasks td,
.cron-table--tasks .cron-link,
.cron-table__schedule,
.cron-table__time,
.cron-table__next {
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 20px;
}

.cron-table--tasks .cron-link {
  font-weight: 650;
}

.cron-table__next {
  font-weight: 400;
}

.cron-card__name {
  font-family: var(--font-sans);
  font-size: 13px;
  font-weight: 650;
  line-height: 20px;
}

.cron-card__schedule-text,
.cron-card__next {
  font-family: var(--font-sans);
  font-size: 13px;
  font-weight: 400;
  line-height: 20px;
}

.cron-jobs__title,
.cron-bulkbar__count {
  line-height: 1.4;
}

/* Bulk actions and view switch share the page's regular control typography. */
.cron-bulkbar,
.cron-bulkbar button,
.cron-bulkbar__count,
.cron-bulkbar__select,
.control-segmented,
.control-segmented__btn {
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 20px;
}
.cron-bulkbar .btn,
.cron-bulkbar__select {
  align-items: center;
  display: inline-flex;
  gap: 6px;
  justify-content: center;
}
.cron-bulkbar .btn > .icon {
  align-items: center;
  display: inline-flex;
  height: 16px;
  justify-content: center;
  line-height: 0;
  width: 16px;
}
.cron-bulkbar .btn > .icon svg {
  display: block;
}
.control-segmented__btn {
  align-items: center;
  display: inline-flex;
  height: 32px;
  justify-content: center;
  min-height: 32px;
  padding: 5px 12px;
}

</style>
