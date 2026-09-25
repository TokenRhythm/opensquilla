import { getCurrentScope, onScopeDispose, ref, type Ref } from 'vue'
import type { SkillCatalog } from '@/modules/skillCatalog'
import type { SkillCandidate } from '@/types/skills'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import { replaceSlashQuery, shortSlashDescription, slashQueryAt, slashSearchRank, type SlashQueryRange } from '@/utils/chat/slashPalette'
import i18n from '@/i18n'
import type { CommandCatalog } from '@/modules/commandCatalog'
import type {
  UsageReporting,
  UsageReportingRequestOptions,
} from '@/modules/usageReporting'
import type { SessionMaintenance } from '@/modules/sessionMaintenance'
import {
  formatGoalDuration,
  type GoalSnapshot,
} from '@/composables/chat/useChatGoals'

export interface ChatSlashCommand {
  kind?: 'command' | 'skill'
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
  '/compact',
  '/goal',
  '/new',
  '/plan',
  '/reset',
  '/usage',
  'compact_context',
  'goal.set',
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
  commandCatalog: CommandCatalog
  usageReporting: UsageReporting
  sessionMaintenance: SessionMaintenance
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
  notify: (message: string) => void
  // Send the optional text after "/plan" through the normal composer path so
  // attachments, intent, optimistic rendering, and retry restoration are kept.
  dispatchPlanPrompt: (prompt: string, composerText: string) => void
  activatePlanMode?: () => boolean | Promise<boolean>
  planModeAvailable?: () => boolean
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

export type SlashCommandClassification = 'registered' | 'unknown' | 'unavailable'

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
  return {
    ...cmd,
    name,
    cmd: name,
    label: cmd?.label || name,
    desc: cmd?.description || cmd?.desc || cmd?.usage || '',
    aliases: Array.isArray(cmd?.aliases) ? cmd.aliases : [],
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

const PALETTE_COPY: Record<string, { key: string; aliases: string[] }> = {
  xlsx: { key: 'xlsx', aliases: ['Excel', 'spreadsheet', '表格'] },
  docx: { key: 'docx', aliases: ['Word', 'document', '文档'] },
  pptx: { key: 'pptx', aliases: ['PowerPoint', 'presentation', '幻灯片', '演示'] },
  'pdf-toolkit': { key: 'pdf', aliases: ['PDF', '文档'] },
  github: { key: 'github', aliases: ['GitHub', 'repository', '代码仓库'] },
  'html-coder': { key: 'html', aliases: ['HTML', 'webpage', '网页'] },
}

function paletteCopy(name: string, description: string): { label: string; desc: string; aliases: string[] } {
  const copy = PALETTE_COPY[name]
  return copy ? {
    label: i18n.global.t(`chat.skillPalette.items.${copy.key}.name`),
    desc: i18n.global.t(`chat.skillPalette.items.${copy.key}.description`),
    aliases: copy.aliases,
  } : { label: name, desc: shortSlashDescription(description), aliases: [] }
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
  let queryRange: SlashQueryRange | null = null
  let candidatesLoaded = false
  let candidateEpoch = 0
  const unsubscribe = options.skillCatalog?.subscribeInvalidation?.(invalidateSkillCandidates)
  if (unsubscribe && getCurrentScope()) onScopeDispose(unsubscribe)

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
    const skills: ChatSlashCommand[] = skillCandidates.value.map(skill => {
      const copy = paletteCopy(skill.name, String(i18n.global.locale.value).startsWith('zh')
        ? skill.descriptionZh || skill.description : skill.description)
      return {
        ...copy, name: skill.name, cmd: '/' + skill.name,
        aliases: [...skill.aliases, ...copy.aliases], kind: 'skill', skill,
      }
    })
    filteredSlashCmds.value = [...commands, ...skills]
      .map((command, index) => ({ command, index, rank: slashSearchRank(query,
        [command.label, command.name, command.cmd, ...command.aliases],
        [command.desc, ...(command.searchDescriptions || []), command.skill?.description || '', command.skill?.descriptionZh || '']) }))
      .filter(item => item.rank >= 0)
      .sort((a, b) => a.rank - b.rank || a.index - b.index)
      .map(item => item.command)
    slashIdx.value = Math.max(0, Math.min(slashIdx.value, filteredSlashCmds.value.length - 1))
    slashOpen.value = true
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
    const names: Record<string, string> = {
      new_chat: 'new', '/new': 'new',
      compact_context: 'compact', 'sessions.contextCompact': 'compact', '/compact': 'compact',
      'goal.set': 'goal', '/goal': 'goal',
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
    closeSlashMenu()
  }

  function closeSlashMenu() {
    slashOpen.value = false
    filteredSlashCmds.value = []
  }

  function completeSlashCmd(cmd: ChatSlashCommand) {
    if (cmd.kind === 'skill' && cmd.skill && queryRange) {
      const skill = cmd.skill
      if (skill.disabled || !skill.ready) {
        options.manageSkill?.(skill.name)
        closeSlashMenu()
        return
      }
      const selected = options.selectedSkills
      if (!selected) return
      if (!selected.value.some(item => item.instanceId === skill.instanceId)) {
        if (selected.value.length >= 16) {
          options.notify(i18n.global.t('chat.skillPalette.limit'))
          return
        }
        selected.value = [...selected.value, { name: skill.name, instanceId: skill.instanceId, digest: skill.digest }]
      }
      const caret = queryRange.start
      options.inputText.value = replaceSlashQuery(options.inputText.value, queryRange)
      closeSlashMenu()
      options.autoResizeTextarea()
      options.setCaret?.(caret)
      return
    }
    if (queryRange && queryRange.start > 0) {
      options.notify(i18n.global.t('chat.skillPalette.commandAtStart'))
      return
    }
    closeSlashMenu()
    const action = cmd?.execution?.action || cmd.cmd || cmd.name
    if (action === 'goal.set' && !options.selectedSkills?.value.length) {
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
    options.inputText.value = cmd.cmd
    options.autoResizeTextarea()
  }

  function activateSlashCmd(cmd: ChatSlashCommand) {
    if (cmd.kind === 'skill') {
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
    skillsLoading,
    skillsError,
    loadSkillCandidates,
    invalidateSkillCandidates,
    slashIdx,
    filteredSlashCmds,
    loadSlashCommands,
    handleSlashInput,
    closeSlashMenu,
    completeSlashCmd,
    activateSlashCmd,
    selectSlashCmd,
    classifySlashCommand,
    executeSlashCommand,
  }
}
