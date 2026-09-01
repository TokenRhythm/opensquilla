import { canonicalSessionKey } from '@/utils/chat/sessionKeys'

interface AuthoritativeSessionSwitchResult {
  authoritative: boolean
}

export async function switchChatViewSession<
  Result extends AuthoritativeSessionSwitchResult,
>(
  requestedSessionKey: string,
  switchSession: (sessionKey: string) => Promise<Result | undefined>,
  onAuthoritativeSubscription: (sessionKey: string) => void | Promise<void>,
): Promise<Result | undefined> {
  const mountedSessionKey = canonicalSessionKey(requestedSessionKey)
  const outcome = await switchSession(requestedSessionKey)
  if (outcome?.authoritative) {
    await onAuthoritativeSubscription(mountedSessionKey)
  }
  return outcome
}
