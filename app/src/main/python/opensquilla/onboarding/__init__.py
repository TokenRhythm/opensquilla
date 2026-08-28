"""Shared onboarding/configuration core used by CLI, RPC, and WebUI."""

from opensquilla.onboarding.audio_specs import (
    AudioProviderSetupField,
    AudioProviderSetupSpec,
    audio_provider_catalog_payload,
    get_audio_provider_setup_spec,
    list_audio_provider_setup_specs,
)
from opensquilla.onboarding.channel_specs import (
    ChannelSetupField,
    ChannelSetupSpec,
    channel_catalog_payload,
    get_channel_setup_spec,
    list_channel_setup_specs,
)
from opensquilla.onboarding.image_generation_specs import (
    ImageGenerationProviderSetupField,
    ImageGenerationProviderSetupSpec,
    get_image_generation_provider_setup_spec,
    image_generation_provider_catalog_payload,
    list_image_generation_provider_setup_specs,
)
from opensquilla.onboarding.memory_embedding_specs import (
    MemoryEmbeddingProviderSetupSpec,
    get_memory_embedding_provider_setup_spec,
    list_memory_embedding_provider_setup_specs,
    memory_embedding_provider_catalog_payload,
)
from opensquilla.onboarding.provider_specs import (
    ProviderSetupField,
    ProviderSetupSpec,
    get_provider_setup_spec,
    list_provider_setup_specs,
    provider_catalog_payload,
)
from opensquilla.onboarding.router_specs import (
    RouterSetupProfile,
    get_router_setup_profile,
    list_router_setup_profiles,
    router_catalog_payload,
)
from opensquilla.onboarding.search_specs import (
    SearchProviderSetupField,
    SearchProviderSetupSpec,
    get_search_provider_setup_spec,
    list_search_provider_setup_specs,
    search_provider_catalog_payload,
)

__all__ = [
    "AudioProviderSetupField",
    "AudioProviderSetupSpec",
    "ChannelSetupField",
    "ChannelSetupSpec",
    "ImageGenerationProviderSetupField",
    "ImageGenerationProviderSetupSpec",
    "MemoryEmbeddingProviderSetupSpec",
    "ProviderSetupField",
    "ProviderSetupSpec",
    "RouterSetupProfile",
    "SearchProviderSetupField",
    "SearchProviderSetupSpec",
    "audio_provider_catalog_payload",
    "channel_catalog_payload",
    "get_audio_provider_setup_spec",
    "get_channel_setup_spec",
    "get_image_generation_provider_setup_spec",
    "get_memory_embedding_provider_setup_spec",
    "get_provider_setup_spec",
    "get_router_setup_profile",
    "get_search_provider_setup_spec",
    "image_generation_provider_catalog_payload",
    "memory_embedding_provider_catalog_payload",
    "list_audio_provider_setup_specs",
    "list_channel_setup_specs",
    "list_image_generation_provider_setup_specs",
    "list_memory_embedding_provider_setup_specs",
    "list_provider_setup_specs",
    "list_router_setup_profiles",
    "list_search_provider_setup_specs",
    "provider_catalog_payload",
    "router_catalog_payload",
    "search_provider_catalog_payload",
]
