/** True for control-plane input that must never enter same-turn steer. */
export function isControlInput(text: string): boolean {
  const value = text.trim()
  return (
    (value.startsWith('/') && !value.startsWith('//'))
    || value.startsWith('!')
  )
}
