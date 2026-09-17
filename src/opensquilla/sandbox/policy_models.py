"""Versioned user-editable Safe policy models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class _PolicyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        alias_generator=to_camel,
    )


class FilePolicySettings(_PolicyModel):
    custom_deny_write_paths: list[str] = Field(default_factory=list)
    recursive_delete_backup_enabled: bool = True
    backup_quota_bytes: int = Field(default=3 * 1024**3, ge=1)

    @field_validator("custom_deny_write_paths")
    @classmethod
    def _clean_paths(cls, values: list[str]) -> list[str]:
        cleaned = [str(value).strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("custom deny-write paths must not be empty")
        return list(dict.fromkeys(cleaned))


class CommandPolicySettings(_PolicyModel):
    require_approval_prefixes: list[list[str]] = Field(default_factory=list)
    auto_allow_prefixes: list[list[str]] = Field(default_factory=list)
    system_tools: Literal["auto", "prompt", "disabled"] = "auto"

    @field_validator("require_approval_prefixes", "auto_allow_prefixes")
    @classmethod
    def _clean_prefixes(cls, prefixes: list[list[str]]) -> list[list[str]]:
        cleaned: list[list[str]] = []
        for prefix in prefixes:
            tokens = [str(token).strip() for token in prefix]
            if not tokens or any(not token for token in tokens):
                raise ValueError("command prefixes require non-empty tokens")
            if tokens not in cleaned:
                cleaned.append(tokens)
        return cleaned


class NetworkPolicySettings(_PolicyModel):
    block_all_network: bool = False
    allow_domains: list[str] = Field(default_factory=list)
    deny_domains: list[str] = Field(default_factory=list)

    @field_validator("allow_domains", "deny_domains")
    @classmethod
    def _clean_domains(cls, values: list[str]) -> list[str]:
        cleaned = [str(value).strip().lower().rstrip(".") for value in values]
        if any(not value for value in cleaned):
            raise ValueError("domain rules must not be empty")
        return list(dict.fromkeys(cleaned))


class RuntimePolicySettings(_PolicyModel):
    enabled: bool = True
    python: bool = True
    node: bool = True
    git_bash: bool = True


class SandboxPolicy(_PolicyModel):
    schema_version: Literal[2] = 2
    policy_version: int = Field(default=0, ge=0)
    files: FilePolicySettings = Field(default_factory=FilePolicySettings)
    commands: CommandPolicySettings = Field(default_factory=CommandPolicySettings)
    network: NetworkPolicySettings = Field(default_factory=NetworkPolicySettings)
    runtimes: RuntimePolicySettings = Field(default_factory=RuntimePolicySettings)

    def to_public_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


__all__ = [
    "CommandPolicySettings",
    "FilePolicySettings",
    "NetworkPolicySettings",
    "RuntimePolicySettings",
    "SandboxPolicy",
]
