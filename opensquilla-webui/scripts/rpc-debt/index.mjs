import { debt as independentDebt, lane as independentLane } from './independent.mjs'
import { debt as platformDebt, lane as platformLane } from './platform.mjs'
import { debt as sessionChatDebt, lane as sessionChatLane } from './session-chat.mjs'
import {
  debt as sharedFoundationDebt,
  lane as sharedFoundationLane,
} from './shared-foundation.mjs'

export const transportDebtLanes = [
  { lane: sharedFoundationLane, debt: sharedFoundationDebt },
  { lane: sessionChatLane, debt: sessionChatDebt },
  { lane: platformLane, debt: platformDebt },
  { lane: independentLane, debt: independentDebt },
]

// The review plan's “73 raw event registrations” was a textual estimate.
// This pinned ledger contains the 70 production `.on` operations whose
// symbols are proven to originate at the RpcStore/RpcClient boundary. Tests,
// generated code, Gateway Adapters, and the transport Implementation itself
// are intentionally outside the migration-debt population.
