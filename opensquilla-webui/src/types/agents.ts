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
