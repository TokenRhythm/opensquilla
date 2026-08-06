import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

describe('ChatView prompt cache keepalive feedback', () => {
  it('refreshes the menu state on demand and accepts the status returned by save', () => {
    expect(chatViewSource).toContain(
      ':prompt-cache-keepalive-status="promptCacheKeepaliveStatus"',
    )
    expect(chatViewSource).toContain(
      '@refresh-prompt-cache-keepalive="void refreshPromptCacheKeepaliveStatus()"',
    )
    expect(chatViewSource).toContain('@saved="onPromptCacheKeepaliveSaved"')
    expect(chatViewSource).toContain("'sessions.promptCacheKeepalive.status'")
    expect(chatViewSource).toContain(
      'if (sessionKey.value === key) promptCacheKeepaliveStatus.value = next',
    )
    expect(chatViewSource).toContain(
      'if (update.sessionKey === sessionKey.value)',
    )
  })
})
