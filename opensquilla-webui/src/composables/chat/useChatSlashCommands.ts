import { computed, ref, type Ref } from 'vue'
import i18n from '@/i18n'
import {
  waitForSessionRpcConnection,
} from '@/composables/chat/sessionBootstrapAdmission'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'

type RpcClient = {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<T>
}

export interface ArgumentChoice {
  value: string
  description: string
}

export interface ChatSlashCommand {
  name: string
  cmd: string
  label: string
  desc: string
  aliases: string[]
  execution?: {
    action?: string
  }
  // Tab-completable argument candidates for this command (e.g. meta-skill names).
  argumentChoices?: ArgumentChoice[]
  // Set on synthetic entries that represent a chosen argument ("/meta <skill>").
  argValue?: string
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

interface UsageStatusResult {
  totals?: {
    tokens?: number
  }
  totalTokens?: number
  total_tokens?: number
}

export interface UseChatSlashCommandsOptions {
  rpc: RpcClient
  catalogCallOptions?: RpcCallOptions
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
  dispatchHidden: (providerText: string, displayText: string) => void
  // Send the optional text after "/plan" through the normal composer path so
  // attachments, intent, optimistic rendering, and retry restoration are kept.
  dispatchPlanPrompt: (prompt: string, composerText: string) => void
  activatePlanMode?: () => boolean | Promise<boolean>
  planModeAvailable?: () => boolean
  codingModeEnabled: Ref<boolean>
  setCodingModeEnabled: (enabled: boolean) => Promise<boolean>
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

function normalizeSlashCommand(cmd: SlashCommandPayload): ChatSlashCommand {
  const name = cmd?.name || cmd?.cmd || ''
  const rawChoices = Array.isArray((cmd as { argument_choices?: unknown })?.argument_choices)
    ? (cmd as { argument_choices: Array<{ value?: unknown; description?: unknown }> }).argument_choices
    : []
  return {
    ...cmd,
    name,
    cmd: name,
    label: cmd?.label || name,
    desc: cmd?.description || cmd?.desc || cmd?.usage || '',
    aliases: Array.isArray(cmd?.aliases) ? cmd.aliases : [],
    argumentChoices: rawChoices
      .map((c) => ({ value: String(c?.value ?? ''), description: String(c?.description ?? '') }))
      .filter((c) => c.value),
  }
}

function makeArgCandidate(parent: ChatSlashCommand, choice: ArgumentChoice): ChatSlashCommand {
  const full = parent.cmd + ' ' + choice.value
  return {
    name: full,
    cmd: full,
    label: full,
    desc: localizedMetaDescription(choice),
    aliases: [],
    execution: parent.execution,
    argValue: choice.value,
  }
}

function localizedMetaDescription(choice: ArgumentChoice): string {
  const keys: Record<string, string> = {
    AwesomeWebpageMetaSkill: 'chat.metaDescriptions.webpage',
    'meta-kid-project-planner': 'chat.metaDescriptions.kidsProject',
    'meta-short-drama': 'chat.metaDescriptions.shortDrama',
    'meta-skill-creator': 'chat.metaDescriptions.skillCreator',
    'meta-paper-write': 'chat.metaDescriptions.paperWriting',
  }
  const key = keys[choice.value]
  return key ? i18n.global.t(key) : choice.description
}

export function useChatSlashCommands(options: UseChatSlashCommandsOptions) {
  const slashOpen = ref(false)
  const slashIdx = ref(0)
  const slashCmds = ref<ChatSlashCommand[]>([])
  const filteredSlashCmds = ref<ChatSlashCommand[]>([])
  const slashCatalogLoaded = ref(false)
  const metaSkillChoices = computed(() => {
    const command = slashCmds.value.find(c => slashCommandKey(c.name) === '/meta')
    const choices = command?.argumentChoices || []
    const preferred = [
      'AwesomeWebpageMetaSkill',
      'meta-short-drama',
      'meta-paper-write',
    ]
    return preferred
      .map(name => choices.find(choice => choice.value === name))
      .filter((choice): choice is ArgumentChoice => Boolean(choice))
  })

  async function loadSlashCommands() {
    try {
      await waitForSessionRpcConnection(options.rpc, options.catalogCallOptions)
      const params = { surface: 'web_chat' }
      const res = options.catalogCallOptions
        ? await options.rpc.call<{ commands?: ChatSlashCommand[] }>(
            'commands.list_for_surface',
            params,
            options.catalogCallOptions,
          )
        : await options.rpc.call<{ commands?: ChatSlashCommand[] }>(
            'commands.list_for_surface',
            params,
          )
      slashCmds.value = (Array.isArray(res?.commands) ? res.commands : []).map(normalizeSlashCommand)
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
    if (action !== 'coding.mode' && action !== '/coding') return command
    return {
      ...command,
      desc: i18n.global.t(
        options.codingModeEnabled.value
          ? 'chat.codingMode.commandDisable'
          : 'chat.codingMode.commandEnable',
      ),
    }
  }

  function handleSlashInput() {
    const val = options.inputText.value
    if (val.startsWith('//') || !val.startsWith('/')) {
      closeSlashMenu()
      return
    }
    const firstSpace = val.indexOf(' ')
    if (firstSpace === -1) {
      // Command-name completion: "/me" -> matching commands.
      const query = val.slice(1).toLowerCase()
      const matches = slashCmds.value
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
    if (parent && choices.length > 0) {
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

  function completeSlashCmd(cmd: ChatSlashCommand) {
    closeSlashMenu()
    const needsArgument = !cmd.argValue && (cmd.argumentChoices?.length ?? 0) > 0
    options.inputText.value = cmd.cmd + (needsArgument ? ' ' : '')
    options.autoResizeTextarea()
    if (needsArgument) handleSlashInput()
  }

  function activateSlashCmd(cmd: ChatSlashCommand) {
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
        options.rpc.call('sessions.reset', { key: options.sessionKey.value })
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
        options.rpc.call<Record<string, unknown>>('sessions.contextCompact', {
          key: compactKey,
          wait: false,
        })
          .then((result) => {
            if (compactKey !== options.sessionKey.value) return
            options.showCompactionToast({ key: compactKey, source: 'manual', ...result })
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
        options.rpc.call<UsageStatusResult>('usage.status')
          .then((result: UsageStatusResult) => {
            const totals = result?.totals || {}
            const tokens = Number(result?.totalTokens ?? result?.total_tokens ?? totals.tokens ?? 0)
            console.info(`Usage: ${tokens.toLocaleString()} tokens`)
          })
          .catch((err: unknown) => console.warn('Usage failed:', err instanceof Error ? err.message : String(err)))
        break
      case 'meta.menu': {
        // Bare "/meta" is handled by the argument-completion branch above
        // (it reopens the menu with the skill choices). Here we only reach the
        // run path, with a skill name supplied (e.g. Enter on "/meta <skill>").
        const skillName = String(args || '').trim()
        if (!skillName) break
        void runMetaSkill(skillName)
        break
      }
    }
  }

  async function runMetaSkill(skillName: string): Promise<void> {
    const name = String(skillName || '').trim()
    if (!name) return
    try {
      const result = await options.rpc.call<{ ok?: boolean; error?: string }>('meta.run', {
        name,
        sessionKey: options.sessionKey.value,
      })
      if (result?.ok) {
        options.dispatchHidden('/meta ' + name, '/meta ' + name)
      } else {
        options.notify(result?.error || i18n.global.t('chat.metaRuns.couldNotRunSkill', { skill: name }))
      }
    } catch (err: unknown) {
      options.notify(i18n.global.t('chat.metaRuns.couldNotRunSkillError', {
        error: err instanceof Error ? err.message : String(err),
      }))
    }
  }

  async function executeSlashCommand(text: string): Promise<boolean> {
    if (!slashCatalogLoaded.value) await loadSlashCommands()
    const [cmdText, ...rest] = text.trim().split(/\s+/)
    const commandKey = slashCommandKey(cmdText)
    const cmd = slashCmds.value.find(command =>
      slashCommandKeys(command).includes(commandKey),
    )
    if (!cmd) {
      closeSlashMenu()
      options.notify(i18n.global.t('chat.slashCommands.unknown', { command: cmdText }))
      return true
    }
    selectSlashCmd(cmd, rest.join(' '))
    return true
  }

  return {
    slashOpen,
    slashIdx,
    metaSkillChoices,
    filteredSlashCmds,
    loadSlashCommands,
    handleSlashInput,
    closeSlashMenu,
    completeSlashCmd,
    activateSlashCmd,
    selectSlashCmd,
    executeSlashCommand,
    runMetaSkill,
  }
}
