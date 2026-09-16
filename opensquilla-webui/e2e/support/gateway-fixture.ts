export type HelloOkOverrides = {
  server?: Record<string, unknown>
  features?: Record<string, unknown>
  snapshot?: Record<string, unknown>
  policy?: Record<string, unknown>
  auth?: Record<string, unknown> | null
  [key: string]: unknown
}

/** Build the complete v3 Hello frame expected by the production RPC decoder. */
export function helloOkFrame(overrides: HelloOkOverrides = {}) {
  const {
    server = {},
    features = {},
    snapshot = {},
    policy = {},
    auth = null,
    ...extensions
  } = overrides
  return {
    ...extensions,
    type: 'hello-ok',
    protocol: 3,
    server: {
      version: 'e2e',
      conn_id: 'e2e-fake-gateway',
      ...server,
    },
    features: {
      methods: [],
      events: [],
      ...features,
    },
    snapshot,
    policy: {
      tick_interval_ms: 30_000,
      ...policy,
    },
    auth,
  }
}

export function helloOkResponse(overrides: HelloOkOverrides = {}): string {
  return JSON.stringify(helloOkFrame(overrides))
}
