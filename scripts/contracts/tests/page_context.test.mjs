import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { loadContractValidators } from '../gateway_contract_verification.mjs'

const { cases } = JSON.parse(readFileSync(new URL(
  '../../../contracts/gateway/v4/conversation/fixtures/page-context.json', import.meta.url,
)))

for (const [method, validatorName, base] of [
  ['chat.send', 'validateChatSendParams', { message: 'Synthetic change' }],
  ['sessions.send', 'validateSessionsSendParams', { message: 'Synthetic change' }],
  ['sessions.pending_inputs.enqueue', 'validateParams', {
    key: 'webchat:synthetic', pendingInputId: 'pending-1', message: 'Synthetic change',
  }],
]) {
  test(`${method} validates ordinary page context without source authority`, async () => {
    const validators = await loadContractValidators(method)
    for (const entry of cases) {
      const value = { ...base, pageContext: entry.context }
      const original = structuredClone(value)
      assert.equal(validators[validatorName](value), entry.valid, entry.id)
      assert.deepEqual(value, original, 'wire validation must not rewrite context')
    }
  })
}

for (const method of ['chat.history', 'sessions.pending_inputs.list']) {
  test(`${method} preserves page context in returned message projections`, async () => {
    const validators = await loadContractValidators(method)
    for (const entry of cases) {
      const value = method === 'chat.history' ? {
        messages: [{ role: 'user', text: 'Synthetic change', pageContext: entry.context }],
        has_more: false, oldest_cursor: null, newest_cursor: null,
        history_scope: 'complete', loaded_count: 1, page_size: 50,
        canonical_available: true, canonical_complete: true,
        compaction_summaries: [], turn_outcomes: [],
      } : {
        items: [{ pendingInputId: 'pending-1', clientRequestId: 'request-1',
          clientMessageId: 'message-1', pageContext: entry.context }],
      }
      const validate = validators[method === 'chat.history' ? 'validateChatHistoryResult' : 'validateResult']
      assert.equal(validate(value), entry.valid, entry.id)
    }
  })
}
