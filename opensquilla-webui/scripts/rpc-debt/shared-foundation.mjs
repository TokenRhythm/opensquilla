export const lane = 'shared-foundation'

export const debt = {
  'src/App.vue': { call: 5, on: 6 },
  'src/composables/useRequest.ts': { call: 1, waitForConnection: 1 },
  'src/composables/useRpc.ts': { call: 2, on: 2 },
}
