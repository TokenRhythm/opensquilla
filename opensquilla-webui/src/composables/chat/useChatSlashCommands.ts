import { computed, getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { SkillCatalog } from '@/modules/skillCatalog'
import type { SkillCandidate } from '@/types/skills'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import { replaceSlashQuery, shortSlashDescription, slashQueryAt, slashSearchRank, type SlashQueryRange } from '@/utils/chat/slashPalette'
import i18n from '@/i18n'
import {
  MetaRunCenterError,
  type MetaLaunchDraftPayload,
  type MetaRunCenter,
} from '@/modules/metaRunCenter'
import type { CommandCatalog } from '@/modules/commandCatalog'
import type {
  UsageReporting,
  UsageReportingRequestOptions,
} from '@/modules/usageReporting'
import type { SessionMaintenance } from '@/modules/sessionMaintenance'
import type { HiddenControlDispatchResult } from '@/types/chat'
import type { MetaSetupReadiness } from '@/types/metaSetup'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
import {
  formatGoalDuration,
  type GoalSnapshot,
} from '@/composables/chat/useChatGoals'

export interface ArgumentChoice {
  value: string
  description: string
  status?: 'ready' | 'needs_setup'
  missingBins?: string[]
  missingEnv?: string[]
  missingEnvAny?: string[][]
  missingSkills?: string[]
  missingCapabilities?: string[]
}

export interface ChatSlashCommand {
  kind?: 'command' | 'skill' | 'meta'
  skill?: SkillCandidate
  name: string
  cmd: string
  label: string
  desc: string
  searchDescriptions?: string[]
  aliases: string[]
  execution?: {
    action?: string
  }
  // Tab-completable argument candidates for this command (e.g. meta-skill names).
  argumentChoices?: ArgumentChoice[]
  // Set on synthetic entries that represent a chosen argument ("/meta <skill>").
  argValue?: string
  metaStatus?: 'ready' | 'needs_setup'
  missingBins?: string[]
  missingEnv?: string[]
  missingEnvAny?: string[][]
  missingSkills?: string[]
  missingCapabilities?: string[]
  [key: string]: unknown
}

interface SlashCommandPayload extends Record<string, unknown> {
  name?: string
  cmd?: string
  label?: string
  description?: string
  desc?: string
  usage?: string
  aliases?: unknown
  execution?: {
    action?: string
  }
}

const SUPPORTED_WEB_SLASH_ACTIONS = new Set([
  '/coding',
  '/compact',
  '/goal',
  '/new',
  '/plan',
  '/reset',
  '/usage',
  'coding.mode',
  'compact_context',
  'goal.set',
  'meta.menu',
  'new_chat',
  'plans.setMode',
  'plans.toggleMode',
  'reset_session',
  'sessions.contextCompact',
  'sessions.reset',
  'usage.status',
  'usage_status',
])

export interface UseChatSlashCommandsOptions {
  skillCatalog?: SkillCatalog
  selectedSkills?: Ref<SelectedSkillRef[]>
  getCaret?: () => number
  setCaret?: (position: number) => void
  manageSkill?: (name: string) => void
  hasNonTextInput?: () => boolean
  commandCatalog: CommandCatalog
  usageReporting: UsageReporting
  sessionMaintenance: SessionMaintenance
  /** Domain seam for MetaSkill launch; wire method names stay in its adapter. */
  metaRunCenter?: MetaRunCenter
  catalogCallOptions?: UsageReportingRequestOptions
  inputText: Ref<string>
  sessionKey: Ref<string>
  autoResizeTextarea: () => void
  newSession: () => void
  resetCurrentSession: () => void
  setCompactInFlight: (active: boolean, key?: string) => void
  showCompactStatus: (
    status: string,
    message: string,
    options?: { tone?: string; detail?: string; dismissMs?: number; source?: string },
  ) => void
  showCompactionToast: (payload: Record<string, unknown>, meta?: Record<string, unknown>) => void
  // Surface a short, client-side notice (e.g. the meta-skill list). No provider call.
  notify: (message: string) => void
  // Send a turn whose provider text bypasses slash parsing (mirrors the TUI
  // override path). Used by /meta <name> to trigger the launch after meta.run.
  dispatchHidden: (
    providerText: string,
    displayText: string,
    clientRequestId?: string,
    targetSessionKey?: string,
  ) => void | HiddenControlDispatchResult | Promise<void | HiddenControlDispatchResult>
  // Recover a request removed from the composer when launch cannot proceed.
  // The callback owns same-session queueing and cross-session persistence.
  restoreDraft?: (launchText: string, sessionKey: string) => void
  // Open a persistent, explicitly-confirmed setup flow. Older embeddings can
  // omit this callback and keep the compact toast fallback.
  requestMetaSetup?: (
    name: string,
    readiness: MetaSetupReadiness,
    originatingSessionKey: string,
    launchText: string,
    clientRequestId?: string,
  ) => void | 'visible' | 'deferred' | Promise<void | 'visible' | 'deferred'>
  // Send the optional text after "/plan" through the normal composer path so
  // attachments, intent, optimistic rendering, and retry restoration are kept.
  dispatchPlanPrompt: (prompt: string, composerText: string) => void
  activatePlanMode?: () => boolean | Promise<boolean>
  planModeAvailable?: () => boolean
  codingModeEnabled: Ref<boolean>
  setCodingModeEnabled: (enabled: boolean) => Promise<boolean>
  // Arm the goal composer: selecting /goal switches the composer into goal
  // draft mode so the user types the goal normally and sends it.
  armGoal?: () => boolean | Promise<boolean>
  startGoal?: (objective: string) => Promise<boolean>
  goalStatus?: () => Promise<GoalSnapshot | null>
  goalEdit?: (objective: string) => Promise<boolean>
  goalPause?: () => Promise<boolean>
  goalResume?: () => Promise<boolean>
  goalClear?: () => Promise<boolean>
}

export interface MetaCommandInvocation {
  skillName: string
  launchText: string
}

export type DurableMetaDraft = MetaLaunchDraftPayload

export type SlashCommandClassification = 'registered' | 'unknown' | 'unavailable'

export function parseMetaCommandInvocation(args: string): MetaCommandInvocation | null {
  const trimmed = String(args || '').trim()
  if (!trimmed) return null

  const firstWhitespace = trimmed.search(/\s/)
  const skillName = firstWhitespace === -1 ? trimmed : trimmed.slice(0, firstWhitespace)
  const suffix = firstWhitespace === -1 ? '' : trimmed.slice(firstWhitespace).trim()
  const requestMatch = suffix.match(/^--(?:\s+([\s\S]*))?$/)
  const request = requestMatch ? String(requestMatch[1] || '').trim() : ''
  return {
    skillName,
    launchText: request ? `/meta ${skillName} -- ${request}` : `/meta ${skillName}`,
  }
}

function slashCommandKey(value: string): string {
  const raw = String(value || '').trim().split(/\s+/, 1)[0].toLowerCase()
  if (!raw) return ''
  return raw.startsWith('/') ? raw : '/' + raw
}

function slashCommandKeys(command: Pick<ChatSlashCommand, 'aliases' | 'cmd' | 'name'>): string[] {
  return [command.name, command.cmd, ...command.aliases]
    .map(slashCommandKey)
    .filter(Boolean)
}

function isMenuCommand(command: ChatSlashCommand): boolean {
  return !slashCommandKeys(command).some(key => key === '/reset' || key === '/usage')
}

function normalizeSlashCommand(cmd: SlashCommandPayload): ChatSlashCommand {
  const name = cmd?.name || cmd?.cmd || ''
  const rawChoices = Array.isArray((cmd as { argument_choices?: unknown })?.argument_choices)
    ? (cmd as { argument_choices: Array<Record<string, unknown>> }).argument_choices
    : []
  return {
    ...cmd,
    name,
    cmd: name,
    label: cmd?.label || name,
    desc: cmd?.description || cmd?.desc || cmd?.usage || '',
    aliases: Array.isArray(cmd?.aliases) ? cmd.aliases : [],
    argumentChoices: rawChoices
      .map((c) => ({
        value: String(c?.value ?? ''),
        description: String(c?.description ?? ''),
        status: c?.status === 'needs_setup' ? 'needs_setup' as const : 'ready' as const,
        missingBins: Array.isArray(c?.missing_bins) ? c.missing_bins.map(String) : [],
        missingEnv: Array.isArray(c?.missing_env) ? c.missing_env.map(String) : [],
        missingEnvAny: Array.isArray(c?.missing_env_any)
          ? c.missing_env_any.map(group => Array.isArray(group) ? group.map(String) : [])
          : [],
        missingSkills: Array.isArray(c?.missing_skills) ? c.missing_skills.map(String) : [],
        missingCapabilities: Array.isArray(c?.missing_capabilities)
          ? c.missing_capabilities.map(String)
          : [],
      }))
      .filter((c) => c.value),
  }
}

function isValidSlashCommandPayload(value: unknown): value is SlashCommandPayload {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const command = value as SlashCommandPayload
  const isValidKey = (candidate: unknown): candidate is string => {
    if (typeof candidate !== 'string') return false
    const trimmedKey = candidate.trim()
    return Boolean(
      trimmedKey
      && !/\s/.test(trimmedKey)
      && slashCommandKey(trimmedKey).length > 1,
    )
  }
  const declaredKeys = [command.name, command.cmd]
  if (!declaredKeys.some(isValidKey)) return false
  if (declaredKeys.some(key => key !== undefined && !isValidKey(key))) return false
  if (
    command.aliases !== undefined
    && (
      !Array.isArray(command.aliases)
      || !command.aliases.every(isValidKey)
    )
  ) return false
  if (command.execution !== undefined) {
    if (
      !command.execution
      || typeof command.execution !== 'object'
      || Array.isArray(command.execution)
    ) return false
    if (
      command.execution.action !== undefined
      && (
        typeof command.execution.action !== 'string'
        || !command.execution.action.trim()
      )
    ) return false
  }
  const rawAction = command.execution?.action || command.name || command.cmd
  return typeof rawAction === 'string'
    && rawAction === rawAction.trim()
    && SUPPORTED_WEB_SLASH_ACTIONS.has(rawAction)
}

function makeArgCandidate(parent: ChatSlashCommand, choice: ArgumentChoice): ChatSlashCommand {
  const full = parent.cmd + ' ' + choice.value
  return {
    name: full,
    cmd: full,
    label: full,
    desc: localizedMetaDescription(choice),
    searchDescriptions: [choice.description],
    aliases: [],
    execution: parent.execution,
    argValue: choice.value,
    metaStatus: choice.status,
    missingBins: choice.missingBins,
    missingEnv: choice.missingEnv,
    missingEnvAny: choice.missingEnvAny,
    missingSkills: choice.missingSkills,
    missingCapabilities: choice.missingCapabilities,
  }
}

const PALETTE_COPY: Record<string, { key: string; aliases: string[] }> = {
  xlsx: { key: 'xlsx', aliases: ['Excel', 'spreadsheet', '表格'] },
  docx: { key: 'docx', aliases: ['Word', 'document', '文档'] },
  pptx: { key: 'pptx', aliases: ['PowerPoint', 'presentation', '幻灯片', '演示'] },
  'pdf-toolkit': { key: 'pdf', aliases: ['PDF', '文档'] },
  github: { key: 'github', aliases: ['GitHub', 'repository', '代码仓库'] },
  'html-coder': { key: 'html', aliases: ['HTML', 'webpage', '网页'] },
  AwesomeWebpageMetaSkill: { key: 'webpage', aliases: ['website', '网站'] },
  'meta-kid-project-planner': { key: 'kidsProject', aliases: ['children', '儿童', '创意项目'] },
  'meta-short-drama': { key: 'shortDrama', aliases: ['video', '短剧', '视频'] },
  'meta-skill-creator': { key: 'skillCreator', aliases: ['workflow', '工作流'] },
  'meta-paper-write': { key: 'paperWriting', aliases: ['paper', '论文'] },
}
const PREFERRED_META_NAMES = ['AwesomeWebpageMetaSkill', 'meta-short-drama', 'meta-paper-write']

function paletteCopy(name: string, description: string): { label: string; desc: string; aliases: string[] } {
  const copy = PALETTE_COPY[name]
  return copy ? {
    label: i18n.global.t(`chat.skillPalette.items.${copy.key}.name`),
    desc: i18n.global.t(`chat.skillPalette.items.${copy.key}.description`),
    aliases: copy.aliases,
  } : { label: name, desc: shortSlashDescription(description), aliases: [] }
}

function localizedMetaDescription(choice: ArgumentChoice): string {
  return paletteCopy(choice.value, choice.description).desc
}

export function useChatSlashCommands(options: UseChatSlashCommandsOptions) {
  const commandCatalog = options.commandCatalog
  const usageReporting = options.usageReporting
  const maintenance = options.sessionMaintenance
  const slashOpen = ref(false)
  const slashIdx = ref(0)
  const slashCmds = ref<ChatSlashCommand[]>([])
  const filteredSlashCmds = ref<ChatSlashCommand[]>([])
  const slashCatalogLoaded = ref(false)
  const skillCandidates = ref<SkillCandidate[]>([])
  const skillsLoading = ref(false)
  const skillsError = ref('')
  const metaDraft = ref<{ name: string; label?: string; text: string; originalText: string; sessionKey: string } | null>(null)
  let queryRange: SlashQueryRange | null = null
  let candidatesLoaded = false
  let candidateEpoch = 0
  const unsubscribe = options.skillCatalog?.subscribeInvalidation?.(invalidateSkillCandidates)
  if (unsubscribe && getCurrentScope()) onScopeDispose(unsubscribe)

  watch(options.sessionKey, (sessionKey) => {
    const draft = metaDraft.value
    if (!draft || draft.sessionKey === sessionKey) return
    if (draft.text.trim()) {
      options.restoreDraft?.(`/meta ${draft.name} -- ${draft.text.trim()}`, draft.sessionKey)
    }
    metaDraft.value = null
  })

  function resetSkillCandidates() {
    candidateEpoch += 1
    candidatesLoaded = false
    skillsLoading.value = false
    skillCandidates.value = []
    skillsError.value = ''
  }

  function invalidateSkillCandidates() {
    resetSkillCandidates()
    closeSlashMenu()
  }

  async function loadSkillCandidates() {
    if (candidatesLoaded || skillsLoading.value || !options.skillCatalog) return
    if (!options.skillCatalog.supportsCandidates()) {
      skillsError.value = i18n.global.t('chat.skillPalette.upgrade')
      return
    }
    const epoch = candidateEpoch
    skillsLoading.value = true
    skillsError.value = ''
    try {
      const result = await options.skillCatalog.listCandidates({ sessionKey: options.sessionKey.value })
      if (epoch !== candidateEpoch) return
      skillCandidates.value = [...result.candidates]
      candidatesLoaded = true
    } catch {
      if (epoch === candidateEpoch) skillsError.value = i18n.global.t('chat.skillPalette.loadFailed')
    } finally {
      if (epoch === candidateEpoch) {
        skillsLoading.value = false
        if (slashOpen.value) updatePalette()
      }
    }
  }

  function updatePalette() {
    if (!queryRange) return
    const query = queryRange.query
    const commands = slashCmds.value.filter(isMenuCommand).map(command => ({
      ...withLiveDescription(command), searchDescriptions: [command.desc], kind: 'command' as const,
    }))
    const meta = slashCmds.value.flatMap(parent => (parent.argumentChoices || []).map(choice => ({
      ...makeArgCandidate(parent, choice), kind: 'meta' as const,
      ...paletteCopy(choice.value, choice.description),
    })))
    const skills: ChatSlashCommand[] = skillCandidates.value.map(skill => {
      const copy = paletteCopy(skill.name, String(i18n.global.locale.value).startsWith('zh')
        ? skill.descriptionZh || skill.description : skill.description)
      return {
        ...copy, name: skill.name, cmd: '/' + skill.name,
        aliases: [...skill.aliases, ...copy.aliases], kind: 'skill', skill,
      }
    })
    filteredSlashCmds.value = [...commands, ...skills, ...meta]
      .map((command, index) => ({ command, index, rank: slashSearchRank(query,
        [command.label, command.name, command.cmd, ...command.aliases],
        [command.desc, ...(command.searchDescriptions || []), command.skill?.description || '', command.skill?.descriptionZh || '']) }))
      .filter(item => item.rank >= 0)
      .sort((a, b) => a.rank - b.rank || a.index - b.index)
      .map(item => item.command)
    slashIdx.value = Math.max(0, Math.min(slashIdx.value, filteredSlashCmds.value.length - 1))
    slashOpen.value = true
  }

  async function launchMetaDraft() {
    const draft = metaDraft.value
    if (!draft?.text.trim() || draft.sessionKey !== options.sessionKey.value) return
    if (options.hasNonTextInput?.() || options.selectedSkills?.value.length) {
      options.notify(i18n.global.t('chat.skillPalette.metaTextOnly'))
      return
    }
    const launchText = `/meta ${draft.name} -- ${draft.text.trim()}`
    metaDraft.value = null
    const outcome = await runMetaInvocation({ skillName: draft.name, launchText,
      originatingSessionKey: draft.sessionKey, clientRequestId: createClientRequestId() })
    if (outcome !== 'failed' && outcome !== 'discarded'
      && options.sessionKey.value === draft.sessionKey && options.inputText.value === draft.originalText) {
      options.inputText.value = ''
      options.autoResizeTextarea()
    }
  }
  const metaSkillChoices = computed(() => {
    const command = slashCmds.value.find(c => slashCommandKey(c.name) === '/meta')
    const choices = command?.argumentChoices || []
    return PREFERRED_META_NAMES
      .map(name => choices.find(choice => choice.value === name))
      .filter((choice): choice is ArgumentChoice => Boolean(choice))
  })

  async function runMetaInvocation(input: {
    skillName: string
    launchText: string
    originatingSessionKey: string
    clientRequestId: string
  }): Promise<'accepted' | 'queued' | 'setup' | 'failed' | 'discarded'> {
    const {
      skillName,
      launchText,
      originatingSessionKey,
      clientRequestId,
    } = input
    const retainStableRetry = async (error: string): Promise<void> => {
      if (options.requestMetaSetup) {
        try {
          const disposition = await options.requestMetaSetup(
            skillName,
            {
              ready: false,
              status: 'needs_setup',
              reasons: [error],
              setup_actions: [],
              manual_setup_actions: [],
            },
            originatingSessionKey,
            launchText,
            clientRequestId,
          )
          if (disposition === 'deferred') {
            options.notify(i18n.global.t('chat.metaRuns.savedForRetry', { skill: skillName }))
          }
          return
        } catch {
          // The Gateway outbox still owns this identity. Fall through to an
          // explicit notice, never to ordinary composer text with a new id.
        }
      }
      options.notify(i18n.global.t('chat.metaRuns.couldNotRunSkillError', { error }))
    }
    try {
      if (!options.metaRunCenter) throw new Error('MetaRunCenter is unavailable')
      const result = await options.metaRunCenter.launch({
        name: skillName,
        sessionKey: originatingSessionKey,
        clientRequestId,
        launchText,
      })
      if (result?.ok) {
        const dispatchResult = await options.dispatchHidden(
          launchText,
          launchText,
          clientRequestId,
          originatingSessionKey,
        )
        if (dispatchResult?.status === 'rejected') {
          await retainStableRetry(dispatchResult.reason)
          return 'failed'
        }
        if (dispatchResult?.status === 'unknown') {
          // The server and browser outboxes retain the exact id and payload.
          // Surface uncertainty without creating a second sendable draft.
          options.notify(i18n.global.t('chat.metaRuns.couldNotRunSkillError', {
            error: dispatchResult.reason,
          }))
          return 'queued'
        }
        return dispatchResult?.status === 'queued' ? 'queued' : 'accepted'
      }
      if (result?.setupRequired) {
        const readiness = result.readiness || {}
        if (options.requestMetaSetup) {
          const disposition = await options.requestMetaSetup(
            skillName,
            readiness,
            originatingSessionKey,
            launchText,
            clientRequestId,
          )
          if (disposition === 'deferred') {
            options.notify(i18n.global.t('chat.metaRuns.savedForRetry', { skill: skillName }))
          }
          return 'setup'
        }
        const dependencies = [
          ...(readiness.missing_bins || []),
          ...(readiness.missing_env || []),
          ...(readiness.missing_env_any || []).map(group => group.join(' / ')),
          ...(readiness.missing_skills || []),
          ...(readiness.missing_capabilities || []),
        ].join(', ') || i18n.global.t('chat.metaRuns.unknownDependency')
        options.notify(i18n.global.t('chat.metaRuns.setupRequired', {
          skill: skillName,
          dependencies,
        }))
        return 'setup'
      }
      const error = result?.error
        || i18n.global.t('chat.metaRuns.couldNotRunSkill', { skill: skillName })
      if (result?.drafted) {
        await retainStableRetry(error)
        return 'failed'
      }
      // Disabled/unknown skills are rejected before the Gateway stages a raw
      // request, so returning those to the composer cannot create two ids.
      options.restoreDraft?.(launchText, originatingSessionKey)
      options.notify(
        error,
      )
      return 'failed'
    } catch (err: unknown) {
      if (err instanceof MetaRunCenterError && err.code === 'draft-discarded') {
        // Another tab already committed the user's cancellation. This identity
        // is terminal: never recreate a setup card or a sendable composer copy.
        options.notify(i18n.global.t('chat.metaRuns.couldNotRunSkillError', {
          error: err.message,
        }))
        return 'discarded'
      }
      // A transport error can happen after the Gateway commits the draft. Keep
      // the same request id in a retry card; restoring plain text would race
      // server recovery and create a second logical request.
      await retainStableRetry(err instanceof Error ? err.message : String(err))
      return 'failed'
    }
  }

  async function restoreDurableMetaDrafts(
    drafts: DurableMetaDraft[],
    isCurrent: () => boolean = () => true,
  ): Promise<string[]> {
    const attemptedRequestIds: string[] = []
    for (const draft of drafts) {
      if (!isCurrent() || draft.sessionKey !== options.sessionKey.value) {
        return attemptedRequestIds
      }
      if (
        !draft.name
        || !draft.launchText
        || !/^\S{1,256}$/.test(draft.clientRequestId)
      ) continue
      attemptedRequestIds.push(draft.clientRequestId)
      const outcome = await runMetaInvocation({
        skillName: draft.name,
        launchText: draft.launchText,
        originatingSessionKey: draft.sessionKey,
        clientRequestId: draft.clientRequestId,
      })
      if (!isCurrent()) return attemptedRequestIds
      if (outcome === 'discarded') continue
      // A setup card or queued hidden turn owns the next user-visible slot.
      // Remaining server drafts stay durable and will be resumed later.
      if (outcome !== 'accepted') return attemptedRequestIds
    }
    return attemptedRequestIds
  }

  async function loadSlashCommands() {
    try {
      const res = await commandCatalog.list('web_chat', options.catalogCallOptions)
      if (
        !Array.isArray(res?.commands)
        || !res.commands.every(isValidSlashCommandPayload)
      ) throw new Error('invalid command catalog')
      slashCmds.value = res.commands.map(normalizeSlashCommand)
      if (
        options.activatePlanMode
        && (options.planModeAvailable?.() ?? true)
        && !slashCmds.value.some(command => slashCommandKeys(command).includes('/plan'))
      ) {
        slashCmds.value.push({
          name: '/plan',
          cmd: '/plan',
          label: '/plan',
          desc: i18n.global.t('chat.planMode.commandDescription'),
          aliases: [],
          execution: { action: 'plans.setMode' },
        })
      }
      slashCatalogLoaded.value = true
      if (options.inputText.value.startsWith('/') && !options.inputText.value.startsWith('//')) {
        handleSlashInput()
      }
    } catch {
      slashCmds.value = []
      slashCatalogLoaded.value = false
    }
  }

  function openWith(cmds: ChatSlashCommand[]): void {
    filteredSlashCmds.value = cmds
    if (cmds.length > 0) {
      slashOpen.value = true
      slashIdx.value = 0
    } else {
      closeSlashMenu()
    }
  }

  function withLiveDescription(command: ChatSlashCommand): ChatSlashCommand {
    const action = command?.execution?.action || command.cmd || command.name
    if (action === 'coding.mode' || action === '/coding') return {
      ...command, desc: i18n.global.t(
        options.codingModeEnabled.value
          ? 'chat.codingMode.commandDisable'
          : 'chat.codingMode.commandEnable',
      ),
    }
    const names: Record<string, string> = {
      new_chat: 'new', '/new': 'new',
      compact_context: 'compact', 'sessions.contextCompact': 'compact', '/compact': 'compact',
      'goal.set': 'goal', '/goal': 'goal', 'meta.menu': 'meta',
      'plans.setMode': 'plan', 'plans.toggleMode': 'plan', '/plan': 'plan',
      reset_session: 'reset', 'sessions.reset': 'reset', '/reset': 'reset',
      usage_status: 'usage', 'usage.status': 'usage', '/usage': 'usage',
    }
    return { ...command, desc: names[action]
      ? i18n.global.t(`chat.skillPalette.commandDescriptions.${names[action]}`)
      : shortSlashDescription(command.desc) }
  }

  function handleSlashInput() {
    const val = options.inputText.value
    queryRange = slashQueryAt(val, options.getCaret?.() ?? val.length)
    if (queryRange) {
      // Directory edits and mutations in another client have no push event.
      // Revalidate at the next palette opening, never on each search keystroke
      // or during client startup. The epoch also fences an earlier open's RPC.
      if (!slashOpen.value) resetSkillCandidates()
      slashIdx.value = 0
      updatePalette()
      void loadSkillCandidates()
      return
    }
    if (val.startsWith('//') || !val.startsWith('/')) {
      closeSlashMenu()
      return
    }
    const firstSpace = val.indexOf(' ')
    if (firstSpace === -1) {
      // Command-name completion: "/me" -> matching commands.
      const query = val.slice(1).toLowerCase()
      const matches = slashCmds.value
        .filter(isMenuCommand)
        .filter(command =>
          slashCommandKeys(command).some(key => key.slice(1).startsWith(query)),
        )
        .map(withLiveDescription)
      const exactKey = slashCommandKey(val)
      const exactMatches = matches.filter(command =>
        slashCommandKeys(command).includes(exactKey),
      )
      openWith(exactMatches.length > 0 ? exactMatches : matches)
      return
    }
    // Argument completion: "/meta <partial>" -> the command's argument choices.
    const head = '/' + val.slice(1, firstSpace).toLowerCase()
    const partial = val.slice(firstSpace + 1).trimStart().toLowerCase()
    const parent = slashCmds.value.find(c => slashCommandKey(c.name) === slashCommandKey(head))
    const choices = parent?.argumentChoices || []
    if (parent && isMenuCommand(parent) && choices.length > 0) {
      openWith(
        choices
          .filter(ch => ch.value.toLowerCase().startsWith(partial))
          .map(ch => makeArgCandidate(parent, ch)),
      )
      return
    }
    closeSlashMenu()
  }

  function closeSlashMenu() {
    slashOpen.value = false
    filteredSlashCmds.value = []
  }

  function addSelectedSkill(skill: SkillCandidate): boolean {
    const selected = options.selectedSkills
    if (!selected) return false
    if (selected.value.some(item => item.instanceId === skill.instanceId)) return true
    if (selected.value.length >= 16) {
      options.notify(i18n.global.t('chat.skillPalette.limit'))
      return false
    }
    selected.value = [...selected.value, {
      name: skill.name,
      instanceId: skill.instanceId,
      digest: skill.digest,
    }]
    return true
  }

  function handleUnavailableSkill(skill: SkillCandidate) {
    if (skill.reasonCode === 'tools_unavailable' && !skill.disabled) {
      options.notify(i18n.global.t('chat.skillPalette.toolsUnavailable'))
    } else {
      options.manageSkill?.(skill.name)
    }
  }

  async function selectSkillByName(name: string): Promise<boolean> {
    if (!options.skillCatalog?.supportsCandidates()) {
      options.notify(i18n.global.t('chat.skillPalette.upgrade'))
      return false
    }
    const sessionKey = options.sessionKey.value
    const epoch = candidateEpoch
    try {
      const result = await options.skillCatalog.listCandidates({ sessionKey })
      if (epoch !== candidateEpoch || sessionKey !== options.sessionKey.value) return false
      const skill = result.candidates.find(candidate => candidate.name === name)
      if (!skill) {
        options.notify(i18n.global.t('chat.skillPalette.empty'))
        return false
      }
      if (skill.disabled || !skill.ready) {
        handleUnavailableSkill(skill)
        return false
      }
      return addSelectedSkill(skill)
    } catch {
      options.notify(i18n.global.t('chat.skillPalette.loadFailed'))
      return false
    }
  }

  function completeSlashCmd(cmd: ChatSlashCommand) {
    if (cmd.kind === 'skill' && cmd.skill && queryRange) {
      const skill = cmd.skill
      if (skill.disabled || !skill.ready) {
        handleUnavailableSkill(skill)
        closeSlashMenu()
        return
      }
      if (!addSelectedSkill(skill)) return
      const caret = queryRange.start
      options.inputText.value = replaceSlashQuery(options.inputText.value, queryRange)
      closeSlashMenu()
      options.autoResizeTextarea()
      options.setCaret?.(caret)
      return
    }
    if (cmd.kind === 'meta') {
      if (options.hasNonTextInput?.() || options.selectedSkills?.value.length) {
        options.notify(i18n.global.t('chat.skillPalette.metaTextOnly'))
        return
      }
      const originalText = options.inputText.value
      metaDraft.value = { name: cmd.argValue!, label: cmd.label, originalText,
        text: queryRange ? replaceSlashQuery(originalText, queryRange).trim() : '',
        sessionKey: options.sessionKey.value }
      closeSlashMenu()
      return
    }
    if (queryRange && queryRange.start > 0) {
      options.notify(i18n.global.t('chat.skillPalette.commandAtStart'))
      return
    }
    closeSlashMenu()
    const needsArgument = !cmd.argValue && (cmd.argumentChoices?.length ?? 0) > 0
    const action = cmd?.execution?.action || cmd.cmd || cmd.name
    if (action === 'goal.set' && !cmd.argValue && !options.selectedSkills?.value.length) {
      // Selecting /goal arms the goal composer: the Goal chip appears next to
      // the access-mode controls and the user types the goal normally.
      const originalInput = options.inputText.value
      void Promise.resolve(options.armGoal?.() ?? false).then((accepted) => {
        if (!accepted || options.inputText.value !== originalInput) return
        options.inputText.value = ''
        options.autoResizeTextarea()
      })
      return
    }
    options.inputText.value = cmd.cmd + (needsArgument ? ' ' : '')
    options.autoResizeTextarea()
    if (needsArgument) handleSlashInput()
  }

  function activateSlashCmd(cmd: ChatSlashCommand) {
    if (cmd.kind === 'skill' || cmd.kind === 'meta') {
      completeSlashCmd(cmd)
      return
    }
    if (cmd.argValue) {
      completeSlashCmd(cmd)
      return
    }
    const typedKey = slashCommandKey(options.inputText.value)
    const isExact = slashCommandKeys(cmd).includes(typedKey)
    if (!isExact) {
      completeSlashCmd(cmd)
      return
    }
    selectSlashCmd(cmd)
  }

  function selectSlashCmd(cmd: ChatSlashCommand, args = '') {
    const action = cmd?.execution?.action || cmd.cmd || cmd.name
    // Argument candidate ("/meta <skill>"): Tab-completes into the composer;
    // the user presses Enter to run it.
    if (cmd.argValue) {
      completeSlashCmd(cmd)
      return
    }
    // A command that takes arguments, selected with none yet: complete to
    // "/cmd " and reopen the menu showing its argument candidates.
    if (
      action !== 'coding.mode'
      && action !== '/coding'
      && !args
      && (cmd.argumentChoices?.length ?? 0) > 0
    ) {
      closeSlashMenu()
      options.inputText.value = cmd.cmd + ' '
      options.autoResizeTextarea()
      handleSlashInput()
      return
    }

    if (options.selectedSkills?.value.length) {
      options.notify(i18n.global.t('chat.skillPalette.commandWithSkills'))
      return
    }

    if (
      action === 'plans.toggleMode'
      || action === 'plans.setMode'
      || action === '/plan'
    ) {
      closeSlashMenu()
      const originalInput = options.inputText.value
      const planPrompt = String(args || '').trim()
      void Promise.resolve(options.activatePlanMode?.() ?? false).then((accepted) => {
        if (!accepted || options.inputText.value !== originalInput) return
        if (planPrompt) {
          options.dispatchPlanPrompt(planPrompt, originalInput)
          return
        }
        options.inputText.value = ''
        options.autoResizeTextarea()
      })
      return
    }
    if (action === 'coding.mode' || action === '/coding') {
      closeSlashMenu()
      const mode = String(args || '').trim().toLowerCase()
      options.inputText.value = ''
      options.autoResizeTextarea()
      if (mode === 'status') {
        options.notify(i18n.global.t(
          options.codingModeEnabled.value
            ? 'chat.codingMode.enabled'
            : 'chat.codingMode.disabled',
        ))
        return
      }
      if (mode !== 'on' && mode !== 'off') {
        if (mode === '') {
          const enabled = !options.codingModeEnabled.value
          void options.setCodingModeEnabled(enabled).then((updated) => {
            options.notify(i18n.global.t(
              updated
                ? (enabled ? 'chat.codingMode.enabled' : 'chat.codingMode.disabled')
                : 'chat.codingMode.updateFailed',
            ))
          })
          return
        }
        options.notify(i18n.global.t('chat.codingMode.usage'))
        return
      }
      void options.setCodingModeEnabled(mode === 'on').then((updated) => {
        options.notify(i18n.global.t(
          updated
            ? (mode === 'on' ? 'chat.codingMode.enabled' : 'chat.codingMode.disabled')
            : 'chat.codingMode.updateFailed',
        ))
      })
      return
    }

    if (action === 'goal.set' || action === '/goal') {
      closeSlashMenu()
      const goalText = String(args || '').trim()
      const firstWord = goalText.split(/\s+/, 1)[0]?.toLowerCase() || ''
      const isSubcommand = ['status', 'clear', 'pause', 'resume', 'edit'].includes(firstWord)
      if (!isSubcommand && firstWord) {
        const objective = firstWord === 'set'
          ? goalText.slice(firstWord.length).trim()
          : goalText
        if (!objective) {
          options.notify(i18n.global.t('chat.slashCommands.goal.usage'))
          return
        }
        // A fully specified slash command accepts the Goal immediately. Menu
        // selection still arms the Goal composer through completeSlashCmd.
        const originalInput = options.inputText.value
        void Promise.resolve(options.startGoal?.(objective) ?? false).then((accepted) => {
          if (!accepted || options.inputText.value !== originalInput) return
          options.inputText.value = ''
          options.autoResizeTextarea()
        })
        return
      }
    }

    closeSlashMenu()
    options.inputText.value = ''
    options.autoResizeTextarea()

    switch (action) {
      case 'new_chat':
      case '/new':
        options.newSession()
        break
      case 'reset_session':
      case 'sessions.reset':
      case '/reset':
        maintenance.reset({ key: options.sessionKey.value })
          .then(() => {
            options.resetCurrentSession()
          })
          .catch((err: unknown) => console.warn('Reset failed:', err instanceof Error ? err.message : String(err)))
        break
      case 'compact_context':
      case 'sessions.contextCompact':
      case '/compact': {
        const compactKey = options.sessionKey.value
        options.setCompactInFlight(true, compactKey)
        options.showCompactStatus('started', i18n.global.t('chat.compact.compacting'), {
          tone: 'info',
          source: 'manual',
        })
        maintenance.compact({ key: compactKey, wait: false })
          .then((result) => {
            if (compactKey !== options.sessionKey.value) return
            options.showCompactionToast({ ...result, key: compactKey, source: 'manual' })
          })
          .catch((err: unknown) => {
            if (compactKey !== options.sessionKey.value) return
            options.showCompactionToast({
              key: compactKey,
              source: 'manual',
              status: 'failed',
              detail: err instanceof Error ? err.message : String(err),
            })
          })
        break
      }
      case 'usage_status':
      case 'usage.status':
      case '/usage':
        usageReporting.status()
          .then((result) => {
            options.notify(i18n.global.t('chat.skillPalette.usage', { tokens: result.totalTokens.toLocaleString() }))
          })
          .catch(() => options.notify(i18n.global.t('chat.skillPalette.usageFailed')))
        break
      case 'meta.menu': {
        // Bare "/meta" is handled by the argument-completion branch above
        // (it reopens the menu with the skill choices). Here we only reach the
        // run path, with a skill name supplied (e.g. Enter on "/meta <skill>").
        const invocation = parseMetaCommandInvocation(args)
        if (!invocation) break
        const { skillName, launchText } = invocation
        const originatingSessionKey = options.sessionKey.value
        const clientRequestId = createClientRequestId()
        // Save the exact request server-side before readiness/setup and retain
        // its stable identity through the eventual hidden turn.
        void runMetaInvocation({
          skillName,
          launchText,
          originatingSessionKey,
          clientRequestId,
        })
        break
      }
      case 'goal.set':
      case '/goal': {
        const goalText = String(args || '').trim()
        const first = goalText.split(/\s+/, 1)[0]?.toLowerCase() || ''
        const remainder = first ? goalText.slice(first.length).trim() : ''
        const fail = (err: unknown) => {
          options.notify(i18n.global.t('chat.slashCommands.goal.actionError', {
            error: err instanceof Error ? err.message : String(err),
          }))
        }
        const status = (goal: GoalSnapshot | null, showUsageWhenEmpty = false) => {
          if (!goal) {
            options.notify(i18n.global.t(
              showUsageWhenEmpty
                ? 'chat.slashCommands.goal.usage'
                : 'chat.slashCommands.goal.statusNone',
            ))
            return
          }
          const steps = goal.progress?.steps ?? []
          const completed = steps.filter(step => step.status === 'completed').length
          const currentStep = steps.find(step => step.status === 'in_progress')?.text
          const reason = goal.blockedReason
            || goal.pauseReason
            || goal.terminalReason
            || goal.continuationDeferredReason
            || ''
          options.notify(i18n.global.t('chat.slashCommands.goal.statusOk', {
            status: goal.status,
            execution: goal.executionState || 'idle',
            turns: goal.turnsSettled,
            tokens: (goal.usage?.totalTokens ?? 0).toLocaleString(),
            runtime: formatGoalDuration(goal.activeTimeMs),
            progress: `${completed}/${steps.length}${currentStep ? ` (${currentStep})` : ''}`,
            goal: goal.objective,
            reason: reason ? ` · ${reason}` : '',
          }))
        }
        if (!first) {
          Promise.resolve(options.goalStatus?.() ?? null)
            .then((goal) => {
              if (goal) {
                status(goal)
                return
              }
              return Promise.resolve(options.armGoal?.() ?? false)
            })
            .catch(fail)
          break
        }
        if (first === 'status') {
          Promise.resolve(options.goalStatus?.() ?? null)
            .then(goal => status(goal))
            .catch(fail)
          break
        }
        if (first === 'clear') {
          Promise.resolve(options.goalClear?.() ?? false)
            .then(accepted => {
              if (accepted) options.notify(i18n.global.t('chat.slashCommands.goal.clearOk'))
            })
            .catch(fail)
          break
        }
        if (first === 'pause') {
          Promise.resolve(options.goalPause?.() ?? false)
            .then(accepted => {
              if (accepted) options.notify(i18n.global.t('chat.slashCommands.goal.pauseOk'))
            })
            .catch(fail)
          break
        }
        if (first === 'resume') {
          Promise.resolve(options.goalResume?.() ?? false)
            .then(accepted => {
              if (accepted) options.notify(i18n.global.t('chat.slashCommands.goal.resumeOk'))
            })
            .catch(fail)
          break
        }
        if (first === 'edit') {
          if (!remainder) {
            options.notify(i18n.global.t('chat.slashCommands.goal.usage'))
            break
          }
          Promise.resolve(options.goalEdit?.(remainder) ?? false)
            .then(accepted => {
              if (accepted) options.notify(i18n.global.t('chat.goal.editNextTurn'))
            })
            .catch(fail)
        }
        break
      }
    }
  }

  async function executeSlashCommand(
    text: string,
    knownClassification?: SlashCommandClassification,
  ): Promise<boolean> {
    const classification = knownClassification ?? await classifySlashCommand(text)
    const trimmed = text.trim()
    const firstWhitespace = trimmed.search(/\s/)
    const cmdText = firstWhitespace === -1 ? trimmed : trimmed.slice(0, firstWhitespace)
    const args = firstWhitespace === -1 ? '' : trimmed.slice(firstWhitespace).trimStart()
    if (classification === 'unavailable') {
      closeSlashMenu()
      options.notify(i18n.global.t('chat.slashCommands.unknown', { command: cmdText }))
      return true
    }
    const commandKey = slashCommandKey(cmdText)
    const cmd = slashCmds.value.find(command =>
      slashCommandKeys(command).includes(commandKey),
    )
    if (!cmd) {
      closeSlashMenu()
      return false
    }
    selectSlashCmd(cmd, args)
    return true
  }

  async function classifySlashCommand(text: string): Promise<SlashCommandClassification> {
    if (!slashCatalogLoaded.value) await loadSlashCommands()
    if (!slashCatalogLoaded.value) return 'unavailable'
    const commandKey = slashCommandKey(text)
    return slashCmds.value.some(command => slashCommandKeys(command).includes(commandKey))
      ? 'registered'
      : 'unknown'
  }

  return {
    slashOpen,
    skillCandidates,
    skillsLoading,
    skillsError,
    loadSkillCandidates,
    invalidateSkillCandidates,
    metaDraft,
    launchMetaDraft,
    slashIdx,
    metaSkillChoices,
    filteredSlashCmds,
    loadSlashCommands,
    handleSlashInput,
    closeSlashMenu,
    completeSlashCmd,
    selectSkillByName,
    activateSlashCmd,
    selectSlashCmd,
    classifySlashCommand,
    executeSlashCommand,
    restoreDurableMetaDrafts,
  }
}
