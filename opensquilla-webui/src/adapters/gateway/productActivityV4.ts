import {
  TELEMETRY_PRODUCT_ACTIVE_RECORD_METHOD,
  type Params,
  type Result,
} from '@/contracts/generated/v4/telemetryProductActiveRecord'
import { validateResult } from '@/contracts/generated/v4/telemetryProductActiveRecordValidators.mjs'
import { ProductActivityError, type ProductActivity } from '@/modules/productActivity'
import { readTransportFailure, type TransportCallOptions } from './transportTypes'

interface ProductActivityTransport {
  request<T>(method: string, params: Record<string, unknown>, options: TransportCallOptions): Promise<T>
  supports(method: string): boolean
  markUnsupported(method: string): void
}

export function createV4ProductActivity(
  rpc: ProductActivityTransport,
): ProductActivity {
  return {
    async recordActive(surface, options) {
      if (!rpc.supports(TELEMETRY_PRODUCT_ACTIVE_RECORD_METHOD)) {
        throw new ProductActivityError('unsupported')
      }
      try {
        const params: Params = { surface }
        const result = await rpc.request<Result>(TELEMETRY_PRODUCT_ACTIVE_RECORD_METHOD, { ...params }, {
          timeoutMs: 5_000,
          timeoutAction: 'reject',
          abortAction: 'reject',
          ...(options?.signal ? { signal: options.signal } : {}),
        })
        if (!validateResult(result)) throw new ProductActivityError('unavailable')
        return result.recorded
      } catch (error) {
        if (readTransportFailure(error).code === 'METHOD_NOT_FOUND') {
          rpc.markUnsupported(TELEMETRY_PRODUCT_ACTIVE_RECORD_METHOD)
          throw new ProductActivityError('unsupported')
        }
        throw new ProductActivityError('unavailable')
      }
    },
  }
}
