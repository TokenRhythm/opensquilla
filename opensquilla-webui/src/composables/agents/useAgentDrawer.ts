import { computed, ref, type Ref } from 'vue'
import i18n from '@/i18n'
import type { Agent, AgentForm } from '@/types/agents'

export function isAgentBuiltin(agent: Agent): boolean {
  return agent.isBuiltin === true || agent.type === 'builtin'
}

export function agentToForm(agent: Agent): AgentForm {
  return {
    id: agent.id || '',
    name: agent.name || '',
    description: agent.description || '',
    tools: Array.isArray(agent.tools) ? agent.tools.slice() : [],
    workspace: agent.workspace || '',
    agentDir: agent.agent_dir || agent.agentDir || '',
    enabled: agent.enabled !== false,
  }
}

export function buildUpdatePayload(initial: AgentForm, current: AgentForm, id: string): Record<string, unknown> {
  const p: Record<string, unknown> = { id }
  for (const k of ['name', 'description', 'workspace', 'agentDir', 'enabled'] as const) {
    if (initial[k] !== current[k]) p[k] = current[k]
  }
  if (JSON.stringify(initial.tools || []) !== JSON.stringify(current.tools || [])) {
    p.tools = current.tools
  }
  return p
}

function cloneForm(form: AgentForm): AgentForm {
  return JSON.parse(JSON.stringify(form)) as AgentForm
}

export function useAgentDrawer(
  agents: Ref<Agent[]>,
  confirmDiscard: () => Promise<boolean>,
) {
  const drawerOpen = ref(false)
  const drawerMode = ref<'view' | 'edit'>('view')
  const drawerAgentId = ref('')
  const drawerIsBuiltin = ref(false)
  const drawerModel = ref('')
  const systemPromptHint = ref(false)
  const saving = ref(false)
  let pendingCloseRequest: Promise<boolean> | null = null

  const initialForm = ref<AgentForm>({
    id: '', name: '', description: '', tools: [], workspace: '', agentDir: '', enabled: true,
  })
  const form = ref<AgentForm>({
    id: '', name: '', description: '', tools: [], workspace: '', agentDir: '', enabled: true,
  })

  const drawerTitle = computed(() =>
    drawerMode.value === 'edit'
      ? i18n.global.t('console.agents.drawerTitleEdit', { id: drawerAgentId.value })
      : i18n.global.t('console.agents.drawerTitleView', { id: drawerAgentId.value })
  )

  const isDirty = computed(() => {
    try {
      return JSON.stringify(initialForm.value) !== JSON.stringify(form.value)
    } catch {
      return true
    }
  })

  const toolsInput = computed({
    get: () => (form.value.tools || []).join(', '),
    set: (val: string) => {
      form.value.tools = String(val || '').split(',').map(s => s.trim()).filter(Boolean)
    },
  })

  const advancedOpen = computed(() =>
    !!form.value.workspace || !!form.value.agentDir || (form.value.tools || []).length > 0 || !form.value.enabled
  )

  function openDrawer(mode: 'view' | 'edit', agentId?: string) {
    if (!agentId) return
    const found = agents.value.find(a => a.id === agentId)
    if (!found) {
      console.warn(`Agent "${agentId}" not found`)
      return
    }
    const seed = agentToForm(found)
    drawerMode.value = mode
    drawerAgentId.value = agentId
    drawerIsBuiltin.value = isAgentBuiltin(found)
    drawerModel.value = found.model || ''
    systemPromptHint.value = !!(found.system_prompt || found.systemPrompt)
    initialForm.value = cloneForm(seed)
    form.value = cloneForm(seed)
    drawerOpen.value = true
  }

  function commitCloseDrawer() {
    drawerOpen.value = false
  }

  function enterEditMode() {
    drawerMode.value = 'edit'
  }

  function requestCloseDrawer(): Promise<boolean> {
    if (!drawerOpen.value) return Promise.resolve(true)
    if (drawerMode.value === 'view' || !isDirty.value) {
      commitCloseDrawer()
      return Promise.resolve(true)
    }
    if (pendingCloseRequest) return pendingCloseRequest

    const request = confirmDiscard()
      .then((ok) => {
        if (ok && drawerOpen.value) commitCloseDrawer()
        return ok
      })
      .finally(() => {
        if (pendingCloseRequest === request) pendingCloseRequest = null
      })
    pendingCloseRequest = request
    return request
  }

  function onCancelEdit() {
    if (!isDirty.value) {
      drawerMode.value = 'view'
      return
    }
    confirmDiscard().then(ok => {
      if (ok) drawerMode.value = 'view'
    })
  }

  function buildSavePayload(): Record<string, unknown> {
    return buildUpdatePayload(initialForm.value, form.value, drawerAgentId.value)
  }

  function applyUpdatedAgent(agent: Agent) {
    const seed = agentToForm(agent)
    initialForm.value = cloneForm(seed)
    form.value = cloneForm(seed)
    drawerModel.value = agent.model || ''
    systemPromptHint.value = !!(agent.system_prompt || agent.systemPrompt)
    drawerMode.value = 'view'
  }

  return {
    drawerOpen,
    drawerMode,
    drawerAgentId,
    drawerIsBuiltin,
    drawerModel,
    systemPromptHint,
    saving,
    initialForm,
    form,
    drawerTitle,
    isDirty,
    toolsInput,
    advancedOpen,
    openDrawer,
    requestCloseDrawer,
    enterEditMode,
    onCancelEdit,
    buildSavePayload,
    applyUpdatedAgent,
  }
}
