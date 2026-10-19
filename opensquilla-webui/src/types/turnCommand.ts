/** Explicit user intent carried by an ordinary turn, including queued sends. */
export interface ProjectInitTurnCommand {
  readonly name: 'project.init'
  readonly args: string
}

export function isProjectInitTurnCommand(value: unknown): value is ProjectInitTurnCommand {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const command = value as Record<string, unknown>
  return command.name === 'project.init' && typeof command.args === 'string'
    && Object.keys(command).every(key => key === 'name' || key === 'args')
}

export function copyTurnCommand(command: ProjectInitTurnCommand): ProjectInitTurnCommand {
  return { name: command.name, args: command.args }
}
