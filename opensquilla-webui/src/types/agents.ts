export interface AgentOption {
  id: string
  name: string
  model?: string
}

export interface Agent {
  id?: string
  name?: string
  type?: string
  isBuiltin?: boolean
  description?: string
  model?: string
  tools?: string[]
  skills?: string[]
  workspace?: string
  agent_dir?: string
  agentDir?: string
  enabled?: boolean
  system_prompt?: string
  systemPrompt?: string
}

export interface AgentForm {
  id: string
  name: string
  description: string
  tools: string[]
  workspace: string
  agentDir: string
  enabled: boolean
}
