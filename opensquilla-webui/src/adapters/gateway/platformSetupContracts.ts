import { ONBOARDING_STATUS_METHOD } from '@/contracts/generated/v4/onboardingStatus'
import { validateOnboardingStatusResult as validateStatus } from '@/contracts/generated/v4/onboardingStatusValidators.mjs'
import { ONBOARDING_PROVIDER_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingProviderConfigure'
import { validateOnboardingProviderConfigureResult as validateProviderConfigure } from '@/contracts/generated/v4/onboardingProviderConfigureValidators.mjs'
import { ONBOARDING_PROVIDER_PROBE_METHOD } from '@/contracts/generated/v4/onboardingProviderProbe'
import { validateOnboardingProviderProbeResult as validateProviderProbe } from '@/contracts/generated/v4/onboardingProviderProbeValidators.mjs'
import { ONBOARDING_MODELS_DISCOVER_METHOD } from '@/contracts/generated/v4/onboardingModelsDiscover'
import { validateOnboardingModelsDiscoverResult as validateModelsDiscover } from '@/contracts/generated/v4/onboardingModelsDiscoverValidators.mjs'
import { ONBOARDING_IMAGE_GENERATION_MODELS_DISCOVER_METHOD } from '@/contracts/generated/v4/onboardingImageGenerationModelsDiscover'
import { validateOnboardingImageGenerationModelsDiscoverResult as validateImageModelsDiscover } from '@/contracts/generated/v4/onboardingImageGenerationModelsDiscoverValidators.mjs'
import { ONBOARDING_PROVIDER_CREDENTIAL_REVEAL_METHOD } from '@/contracts/generated/v4/onboardingProviderCredentialReveal'
import { validateOnboardingProviderCredentialRevealResult as validateCredentialReveal } from '@/contracts/generated/v4/onboardingProviderCredentialRevealValidators.mjs'
import { ONBOARDING_PROVIDER_CREDENTIAL_CLEAR_METHOD } from '@/contracts/generated/v4/onboardingProviderCredentialClear'
import { validateOnboardingProviderCredentialClearResult as validateCredentialClear } from '@/contracts/generated/v4/onboardingProviderCredentialClearValidators.mjs'
import { ONBOARDING_LLM_PROFILE_UPSERT_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileUpsert'
import { validateOnboardingLlmProfileUpsertResult as validateProfileUpsert } from '@/contracts/generated/v4/onboardingLlmProfileUpsertValidators.mjs'
import { ONBOARDING_LLM_PROFILE_ACTIVATE_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileActivate'
import { validateOnboardingLlmProfileActivateResult as validateProfileActivate } from '@/contracts/generated/v4/onboardingLlmProfileActivateValidators.mjs'
import { ONBOARDING_LLM_PROFILE_REMOVE_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileRemove'
import { validateOnboardingLlmProfileRemoveResult as validateProfileRemove } from '@/contracts/generated/v4/onboardingLlmProfileRemoveValidators.mjs'
import { ONBOARDING_LLM_PROFILE_ACTIVE_REMOVE_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileActiveRemove'
import { validateOnboardingLlmProfileActiveRemoveResult as validateProfileActiveRemove } from '@/contracts/generated/v4/onboardingLlmProfileActiveRemoveValidators.mjs'
import { ONBOARDING_LLM_PROFILE_CREDENTIAL_CLEAR_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileCredentialClear'
import { validateOnboardingLlmProfileCredentialClearResult as validateProfileCredentialClear } from '@/contracts/generated/v4/onboardingLlmProfileCredentialClearValidators.mjs'
import { ONBOARDING_LLM_PROFILE_PROBE_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileProbe'
import { validateOnboardingLlmProfileProbeResult as validateProfileProbe } from '@/contracts/generated/v4/onboardingLlmProfileProbeValidators.mjs'
import { ONBOARDING_LLM_PROFILE_DRAFT_PROBE_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileDraftProbe'
import { validateOnboardingLlmProfileDraftProbeResult as validateProfileDraftProbe } from '@/contracts/generated/v4/onboardingLlmProfileDraftProbeValidators.mjs'
import { ONBOARDING_LLM_PROFILE_MODELS_DISCOVER_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileModelsDiscover'
import { validateOnboardingLlmProfileModelsDiscoverResult as validateProfileModelsDiscover } from '@/contracts/generated/v4/onboardingLlmProfileModelsDiscoverValidators.mjs'
import { ONBOARDING_LLM_PROFILE_DRAFT_MODELS_DISCOVER_METHOD } from '@/contracts/generated/v4/onboardingLlmProfileDraftModelsDiscover'
import { validateOnboardingLlmProfileDraftModelsDiscoverResult as validateProfileDraftModelsDiscover } from '@/contracts/generated/v4/onboardingLlmProfileDraftModelsDiscoverValidators.mjs'
import { ONBOARDING_ROUTER_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingRouterConfigure'
import { validateOnboardingRouterConfigureResult as validateRouterConfigure } from '@/contracts/generated/v4/onboardingRouterConfigureValidators.mjs'
import { ONBOARDING_ENSEMBLE_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingEnsembleConfigure'
import { validateOnboardingEnsembleConfigureResult as validateEnsembleConfigure } from '@/contracts/generated/v4/onboardingEnsembleConfigureValidators.mjs'
import { ONBOARDING_SEARCH_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingSearchConfigure'
import { validateOnboardingSearchConfigureResult as validateSearchConfigure } from '@/contracts/generated/v4/onboardingSearchConfigureValidators.mjs'
import { ONBOARDING_IMAGE_GENERATION_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingImageGenerationConfigure'
import { validateOnboardingImageGenerationConfigureResult as validateImageGenerationConfigure } from '@/contracts/generated/v4/onboardingImageGenerationConfigureValidators.mjs'
import { ONBOARDING_MEMORY_EMBEDDING_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingMemoryEmbeddingConfigure'
import { validateOnboardingMemoryEmbeddingConfigureResult as validateMemoryEmbeddingConfigure } from '@/contracts/generated/v4/onboardingMemoryEmbeddingConfigureValidators.mjs'
import { ONBOARDING_AUDIO_CONFIGURE_METHOD } from '@/contracts/generated/v4/onboardingAudioConfigure'
import { validateOnboardingAudioConfigureResult as validateAudioConfigure } from '@/contracts/generated/v4/onboardingAudioConfigureValidators.mjs'
import { ONBOARDING_CAPABILITY_RESET_METHOD } from '@/contracts/generated/v4/onboardingCapabilityReset'
import { validateOnboardingCapabilityResetResult as validateCapabilityReset } from '@/contracts/generated/v4/onboardingCapabilityResetValidators.mjs'

export interface SetupContractDescriptor {
  readonly method: string
  readonly validateResult: (value: unknown) => boolean
}

const descriptor = (method: string, validateResult: (value: unknown) => boolean): SetupContractDescriptor => ({ method, validateResult })

export const setupContracts = {
  status: descriptor(ONBOARDING_STATUS_METHOD, validateStatus),
  providerConfigure: descriptor(ONBOARDING_PROVIDER_CONFIGURE_METHOD, validateProviderConfigure),
  providerProbe: descriptor(ONBOARDING_PROVIDER_PROBE_METHOD, validateProviderProbe),
  modelsDiscover: descriptor(ONBOARDING_MODELS_DISCOVER_METHOD, validateModelsDiscover),
  imageModelsDiscover: descriptor(ONBOARDING_IMAGE_GENERATION_MODELS_DISCOVER_METHOD, validateImageModelsDiscover),
  credentialReveal: descriptor(ONBOARDING_PROVIDER_CREDENTIAL_REVEAL_METHOD, validateCredentialReveal),
  credentialClear: descriptor(ONBOARDING_PROVIDER_CREDENTIAL_CLEAR_METHOD, validateCredentialClear),
  profileUpsert: descriptor(ONBOARDING_LLM_PROFILE_UPSERT_METHOD, validateProfileUpsert),
  profileActivate: descriptor(ONBOARDING_LLM_PROFILE_ACTIVATE_METHOD, validateProfileActivate),
  profileRemove: descriptor(ONBOARDING_LLM_PROFILE_REMOVE_METHOD, validateProfileRemove),
  profileActiveRemove: descriptor(ONBOARDING_LLM_PROFILE_ACTIVE_REMOVE_METHOD, validateProfileActiveRemove),
  profileCredentialClear: descriptor(ONBOARDING_LLM_PROFILE_CREDENTIAL_CLEAR_METHOD, validateProfileCredentialClear),
  profileProbe: descriptor(ONBOARDING_LLM_PROFILE_PROBE_METHOD, validateProfileProbe),
  profileDraftProbe: descriptor(ONBOARDING_LLM_PROFILE_DRAFT_PROBE_METHOD, validateProfileDraftProbe),
  profileModelsDiscover: descriptor(ONBOARDING_LLM_PROFILE_MODELS_DISCOVER_METHOD, validateProfileModelsDiscover),
  profileDraftModelsDiscover: descriptor(ONBOARDING_LLM_PROFILE_DRAFT_MODELS_DISCOVER_METHOD, validateProfileDraftModelsDiscover),
  routerConfigure: descriptor(ONBOARDING_ROUTER_CONFIGURE_METHOD, validateRouterConfigure),
  ensembleConfigure: descriptor(ONBOARDING_ENSEMBLE_CONFIGURE_METHOD, validateEnsembleConfigure),
  searchConfigure: descriptor(ONBOARDING_SEARCH_CONFIGURE_METHOD, validateSearchConfigure),
  imageGenerationConfigure: descriptor(ONBOARDING_IMAGE_GENERATION_CONFIGURE_METHOD, validateImageGenerationConfigure),
  memoryEmbeddingConfigure: descriptor(ONBOARDING_MEMORY_EMBEDDING_CONFIGURE_METHOD, validateMemoryEmbeddingConfigure),
  audioConfigure: descriptor(ONBOARDING_AUDIO_CONFIGURE_METHOD, validateAudioConfigure),
  capabilityReset: descriptor(ONBOARDING_CAPABILITY_RESET_METHOD, validateCapabilityReset),
} as const
