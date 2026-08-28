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

// This pinned ledger owns both RPC symbol debt and authored Gateway HTTP
// boundary details. Static assets, data/blob URLs, external resources, tests,
// generated code, Gateway Adapters, and transport Implementations are outside
// the migration-debt population.
