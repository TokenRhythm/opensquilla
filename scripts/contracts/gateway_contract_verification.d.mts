export interface ContractValidator {
  (value: unknown): boolean
  errors?: readonly unknown[] | null
}

export function loadContractValidators(
  wireName: string,
  options?: { kind?: 'method' | 'event' },
): Promise<Record<string, ContractValidator>>
