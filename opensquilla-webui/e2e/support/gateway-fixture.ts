export type HelloOkOverrides = {
  server?: Record<string, unknown>
  features?: Record<string, unknown>
  snapshot?: Record<string, unknown>
  policy?: Record<string, unknown>
  auth?: { principal?: Record<string, unknown> | null; [key: string]: unknown } | null
  [key: string]: unknown
}

/** Build the complete v3 Hello frame expected by the production RPC decoder. */
export function helloOkFrame(overrides: HelloOkOverrides = {}) {
  const {
    server = {},
    features = {},
    snapshot = {},
    policy = {},
    auth = {},
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
    // Real Gateway hellos carry a complete server-derived principal. Partial
    // fixture overrides customize it; auth:null explicitly models a peer
    // without identity proof and must not authorize durable delivery.
    auth: auth === null ? null : {
      ...auth,
      principal: auth.principal === null ? null : {
        role: 'operator', authenticated: true, isOwner: true, authState: 'authenticated',
        scopes: ['operator.read', 'operator.write'], capabilities: ['chat.read', 'chat.write'],
        tokenPublicId: null, guestOwnerId: null,
        ...auth.principal,
      },
    },
  }
}

export function helloOkResponse(overrides: HelloOkOverrides = {}): string {
  return JSON.stringify(helloOkFrame(overrides))
}
