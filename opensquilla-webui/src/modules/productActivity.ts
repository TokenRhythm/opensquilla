import type { InjectionKey } from 'vue'

export type ProductActivitySurface = 'desktop' | 'web'

export class ProductActivityError extends Error {
  constructor(readonly code: 'unsupported' | 'unavailable') {
    super('Product activity recording is unavailable')
    this.name = 'ProductActivityError'
  }
}

/** Content-free foreground activity; the Gateway owns consent and identity. */
export interface ProductActivity {
  recordActive(surface: ProductActivitySurface, options?: { signal?: AbortSignal }): Promise<boolean>
}

export const PRODUCT_ACTIVITY_KEY: InjectionKey<ProductActivity> = Symbol('ProductActivity')
