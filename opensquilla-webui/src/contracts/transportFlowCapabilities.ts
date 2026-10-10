/** Wire names shared by the bundled WebUI's lane-ACK handshake and RPC. */
export const TRANSPORT_SESSION_FLOW_V2_CAPABILITY = 'transport.session-flow.v2' as const
export const TRANSPORT_SESSION_FLOW_V2_METHOD = 'transport.sessionFlow.update.v2' as const
// The generated contract keeps the schema method-availability name. It is
// deliberately recorded beside the wire name so a future generator change
// cannot silently advertise one while dispatching the other.
export const TRANSPORT_SESSION_FLOW_V2_SCHEMA_CAPABILITY = 'transport.sessionFlow.v2' as const
