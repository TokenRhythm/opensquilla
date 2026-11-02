"""Exact RouterInput contract extracted from the frozen training runtime."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

class RouterInput(ContractModel):
    current_request: str
    task_anchor: str = ""
    history_user: tuple[str, ...] = Field(default=(), max_length=4)
    previous_answer: str = ""
    previous_usage: dict[str, JsonValue] = Field(default_factory=dict)
    previous_outcome: Literal["success", "failure", "clarification", "unknown"] = "unknown"
    active_route_tier: Literal["C0", "C1", "C2", "C3"] | None = None
    route_history: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=5)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    tool_state: dict[str, JsonValue] = Field(default_factory=dict)
    attachments: tuple[dict[str, JsonValue], ...] = ()
