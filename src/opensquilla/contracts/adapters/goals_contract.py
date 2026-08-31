"""Typed validation seam for the durable Goals v4 RPCs.

The adapter deliberately does not call GoalService. It validates and projects
wire payloads while keeping the existing Gateway handlers and state machine
unchanged; the Gateway registration layer wraps those handlers here without
changing their public behavior.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

from opensquilla.contracts.generated.v4.goals_capabilities import (
    LegacyNonObjectParams as GoalsCapabilitiesLegacyNonObjectParams,
)
from opensquilla.contracts.generated.v4.goals_capabilities import (
    Params as GoalsCapabilitiesParams,
)
from opensquilla.contracts.generated.v4.goals_capabilities import (
    Result as GoalsCapabilitiesResult,
)
from opensquilla.contracts.generated.v4.goals_capabilities_metadata import (
    GOALS_CAPABILITIES_METHOD,
)
from opensquilla.contracts.generated.v4.goals_reattach import (
    LegacyNonObjectParams as GoalsReattachLegacyNonObjectParams,
)
from opensquilla.contracts.generated.v4.goals_reattach import (
    Params as GoalsReattachParams,
)
from opensquilla.contracts.generated.v4.goals_reattach import (
    Result as GoalsReattachResult,
)
from opensquilla.contracts.generated.v4.goals_reattach_metadata import (
    GOALS_REATTACH_METHOD,
)
from opensquilla.contracts.generated.v4.goals_set import (
    LegacyNonObjectParams as GoalsSetLegacyNonObjectParams,
)
from opensquilla.contracts.generated.v4.goals_set import (
    LegacyParams as GoalsSetLegacyParams,
)
from opensquilla.contracts.generated.v4.goals_set import Params as GoalsSetParams
from opensquilla.contracts.generated.v4.goals_set import Result as GoalsSetResult
from opensquilla.contracts.generated.v4.goals_set_metadata import GOALS_SET_METHOD
from opensquilla.contracts.generated.v4.goals_status import (
    LegacyNonObjectParams as GoalsStatusLegacyNonObjectParams,
)
from opensquilla.contracts.generated.v4.goals_status import Params as GoalsStatusParams
from opensquilla.contracts.generated.v4.goals_status import Result as GoalsStatusResult
from opensquilla.contracts.generated.v4.goals_status_metadata import GOALS_STATUS_METHOD


class GoalsContractError(ValueError):
    """Payload crossed the Goals adapter without satisfying its Contract."""


def _errors(exc: Exception) -> tuple[dict[str, Any], ...]:
    if not isinstance(exc, ValidationError):
        return ({"type": "value_error", "loc": (), "msg": str(exc)},)
    return tuple(
        cast(
            list[dict[str, Any]],
            exc.errors(include_url=False, include_context=False, include_input=False),
        )
    )


def goals_status_params_contract_errors(params: Any) -> tuple[dict[str, Any], ...]:
    """Observe status request drift without replacing Gateway error semantics."""

    try:
        if isinstance(params, Mapping):
            GoalsStatusParams.model_validate(dict(params))
        elif params is not None:
            GoalsStatusLegacyNonObjectParams.model_validate(params)
    except (ValidationError, ValueError) as exc:
        return _errors(exc)
    return ()


def goals_capabilities_params_contract_errors(
    params: Any,
) -> tuple[dict[str, Any], ...]:
    """Observe capability-query request drift without changing v4 errors."""

    try:
        if isinstance(params, Mapping):
            GoalsCapabilitiesParams.model_validate(dict(params))
        elif params is not None:
            GoalsCapabilitiesLegacyNonObjectParams.model_validate(params)
    except (ValidationError, ValueError) as exc:
        return _errors(exc)
    return ()


def goals_set_params_contract_errors(params: Any) -> tuple[dict[str, Any], ...]:
    """Observe set request drift while forwarding legacy values unchanged."""

    try:
        if isinstance(params, Mapping):
            values = dict(params)
            try:
                GoalsSetParams.model_validate(values)
            except ValidationError:
                GoalsSetLegacyParams.model_validate(values)
        elif params is not None:
            GoalsSetLegacyNonObjectParams.model_validate(params)
    except (ValidationError, ValueError) as exc:
        return _errors(exc)
    return ()


def validate_goals_status_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, Mapping):
        raise GoalsContractError("goals.status params must be an object")
    try:
        return dict(
            GoalsStatusParams.model_validate(dict(params)).root.model_dump(exclude_none=False)
        )
    except ValidationError as exc:
        raise GoalsContractError(
            f"{GOALS_STATUS_METHOD} params violated Contract: {_errors(exc)}"
        ) from exc


def validate_goals_status_result(payload: Any) -> dict[str, Any]:
    try:
        return GoalsStatusResult.model_validate(payload).model_dump(exclude_none=False)
    except ValidationError as exc:
        raise GoalsContractError(
            f"{GOALS_STATUS_METHOD} result violated Contract: {_errors(exc)}"
        ) from exc


def validate_goals_capabilities_result(payload: Any) -> dict[str, Any]:
    """Validate the canonical top-level capability projection and preserve extensions."""

    if not isinstance(payload, Mapping):
        raise GoalsContractError(
            f"{GOALS_CAPABILITIES_METHOD} result must be a JSON object"
        )
    try:
        GoalsCapabilitiesResult.model_validate(dict(payload))
    except ValidationError as exc:
        raise GoalsContractError(
            f"{GOALS_CAPABILITIES_METHOD} result violated Contract: {_errors(exc)}"
        ) from exc
    return dict(payload)


def validate_goals_set_params(params: Any) -> dict[str, Any]:
    if not isinstance(params, Mapping):
        raise GoalsContractError("goals.set params must be an object")
    try:
        return dict(GoalsSetParams.model_validate(dict(params)).root.model_dump(exclude_none=False))
    except ValidationError as exc:
        try:
            return dict(
                GoalsSetLegacyParams.model_validate(dict(params)).model_dump(exclude_none=False)
            )
        except ValidationError:
            raise GoalsContractError(
                f"{GOALS_SET_METHOD} params violated Contract: {_errors(exc)}"
            ) from exc


def validate_goals_set_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or "goal" not in payload:
        raise GoalsContractError(f"{GOALS_SET_METHOD} result is missing acceptance outcome")
    try:
        result = GoalsSetResult.model_validate(payload).model_dump(exclude_none=False)
        if result.get("accepted") is not True or "goal" not in result:
            raise GoalsContractError(f"{GOALS_SET_METHOD} result is missing acceptance outcome")
        return result
    except ValidationError as exc:
        raise GoalsContractError(
            f"{GOALS_SET_METHOD} result violated Contract: {_errors(exc)}"
        ) from exc


def goals_reattach_params_contract_errors(params: Any) -> tuple[dict[str, Any], ...]:
    """Observe reattach request drift without replacing its legacy errors."""

    try:
        if isinstance(params, Mapping):
            values = _canonicalize_reattach_aliases(params)
            GoalsReattachParams.model_validate(values)
            for field in ("sessionKey", "sessionId", "expectedGoalId", "epoch"):
                if values.get(field) is None:
                    return ({
                        "type": "missing",
                        "loc": (field,),
                        "msg": f"{field} is required",
                    },)
            # The generated Pydantic model intentionally keeps the conditional
            # token rule open for legacy aliases.  Observe it explicitly so
            # diagnostics still identify an omitted continuity proof while the
            # existing handler remains the source of client-visible errors.
            if values.get("takeover") is not True and not (
                isinstance(values.get("continuityToken"), str)
                and bool(values.get("continuityToken"))
            ):
                return ({
                    "type": "missing",
                    "loc": ("continuityToken",),
                    "msg": "continuityToken is required unless takeover is true",
                },)
        elif params is not None:
            GoalsReattachLegacyNonObjectParams.model_validate(params)
    except (ValidationError, ValueError) as exc:
        return _errors(exc)
    return ()


def validate_goals_reattach_result(payload: Any) -> dict[str, Any]:
    """Validate the accepted lease response while preserving open fields."""

    if not isinstance(payload, Mapping):
        raise GoalsContractError(
            f"{GOALS_REATTACH_METHOD} result must be a JSON object"
        )
    try:
        normalized = _canonicalize_reattach_aliases(payload, result=True)
        GoalsReattachResult.model_validate(normalized)
        goal = normalized.get("goal")
        _validate_complete_reattach_goal(goal)
        _validate_reattach_identity_consistency(normalized, goal)
    except (ValidationError, ValueError) as exc:
        raise GoalsContractError(
            f"{GOALS_REATTACH_METHOD} result violated Contract: {_errors(exc)}"
        ) from exc
    if (
        normalized.get("accepted") is not True
        or normalized.get("sessionKey") is None
        or normalized.get("sessionId") is None
        or normalized.get("epoch") is None
        or "goal" not in normalized
        or normalized.get("goal") is None
        or not isinstance(normalized.get("continuityToken"), str)
        or not normalized["continuityToken"]
    ):
        raise GoalsContractError(
            f"{GOALS_REATTACH_METHOD} result is missing acceptance outcome"
        )
    return dict(payload)


def _validate_complete_reattach_goal(value: Any) -> None:
    """Enforce the identity/revision fields required by the reattach UI.

    ``datamodel-code-generator`` represents JSON-Schema ``anyOf`` required
    alias groups as optional Pydantic fields.  Keep the semantic requirement
    explicit at this adapter boundary instead of weakening the lease response
    to a status-only projection that the WebUI cannot safely adopt.
    """

    if not isinstance(value, Mapping):
        raise ValueError("goal must be an object")
    _required_goal_alias(value, ("goalId", "goal_id"), string=True)
    _required_goal_alias(value, ("sessionKey", "session_key"), string=True)
    _required_goal_alias(value, ("sessionId", "session_id"), string=True)
    _required_goal_alias(
        value,
        ("epoch", "sessionEpoch", "session_epoch"),
        string=False,
    )
    _required_goal_alias(
        value,
        ("objective", "goalText", "goal_text"),
        string=True,
    )
    if not isinstance(value.get("status"), str) or not value["status"].strip():
        raise ValueError("goal status must be a non-empty string")
    for names in (
        ("stateRevision", "state_revision"),
        ("objectiveRevision", "objective_revision"),
        ("progressRevision", "progress_revision"),
    ):
        _required_goal_alias(value, names, string=False)


def _validate_reattach_identity_consistency(
    result: Mapping[str, Any],
    goal: Any,
) -> None:
    """Reject a response whose outer session fence disagrees with its Goal."""

    if not isinstance(goal, Mapping):
        # ``_validate_complete_reattach_goal`` provides the detailed error.
        raise ValueError("goal must be an object")
    goal_key = _required_goal_alias(goal, ("sessionKey", "session_key"), string=True)
    goal_session_id = _required_goal_alias(
        goal,
        ("sessionId", "session_id"),
        string=True,
    )
    goal_epoch = _required_goal_alias(
        goal,
        ("epoch", "sessionEpoch", "session_epoch"),
        string=False,
    )
    result_key = result.get("sessionKey")
    result_session_id = result.get("sessionId")
    result_epoch = _nonnegative_integer(result.get("epoch"), "result epoch")
    if not isinstance(result_key, str) or not isinstance(result_session_id, str):
        raise ValueError("result session fence must contain string identities")
    if result_key.strip() != goal_key.strip():
        raise ValueError("result sessionKey disagrees with Goal sessionKey")
    if result_session_id != goal_session_id or result_epoch != goal_epoch:
        raise ValueError("result session fence disagrees with Goal identity")


def _required_goal_alias(
    values: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    string: bool,
) -> Any:
    present = [(name, values[name]) for name in names if name in values]
    if not present:
        raise ValueError(f"goal is missing one of: {', '.join(names)}")
    normalized: list[tuple[str, Any]] = []
    for name, value in present:
        if value is None:
            raise ValueError(f"goal {name} must not be null")
        if string:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"goal {name} must be a non-empty string")
            value = value.strip()
        else:
            value = _nonnegative_integer(value, f"goal {name}")
        normalized.append((name, value))
    _, first_value = normalized[0]
    if any(value != first_value for _, value in normalized[1:]):
        raise ValueError(
            f"conflicting Goal aliases: {', '.join(name for name, _ in normalized)}"
        )
    return first_value


def _nonnegative_integer(value: Any, label: str) -> int:
    """Match JSON-Schema's mathematical integer semantics across JSON parsers."""

    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    if isinstance(value, int):
        if value >= 0:
            return value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    raise ValueError(f"{label} must be a non-negative integer")


def _canonicalize_reattach_aliases(
    values: Mapping[str, Any],
    *,
    result: bool = False,
) -> dict[str, Any]:
    """Copy legacy spellings into the generated model's canonical fields.

    The generated Pydantic model intentionally describes the canonical v4
    tree.  Older clients may still send snake-case (or ``key``/epoch aliases),
    so this copy belongs at the compatibility seam rather than in the model or
    the Goal service.  Conflicting aliases are rejected instead of allowing a
    validator and the legacy handler to choose different values.
    """

    is_result = result
    normalized = dict(values)
    aliases = (
        ("sessionKey", "session_key", "key"),
        ("sessionId", "session_id"),
        ("epoch", "sessionEpoch", "session_epoch"),
        ("expectedGoalId", "expected_goal_id"),
        ("continuityToken", "continuity_token"),
        ("sourceKind", "source_kind"),
    )
    if is_result:
        aliases = (
            ("sessionKey", "session_key"),
            ("sessionId", "session_id"),
            ("epoch",),
            ("continuityToken", "continuity_token"),
        )
    for canonical, *legacy_names in aliases:
        supplied_names = [
            name for name in (canonical, *legacy_names) if name in values
        ]
        present = [
            (name, values[name])
            for name in supplied_names
        ]
        if not present:
            continue
        normalized_present: list[tuple[str, Any]] = []
        for name, value in present:
            if value is None:
                raise ValueError(
                    f"null is not allowed for {canonical} aliases: "
                    + ", ".join(supplied_names)
                )
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    raise ValueError(f"blank value is not allowed for {canonical}")
            normalized_present.append((name, value))
        _, first_value = normalized_present[0]
        if any(value != first_value for _, value in normalized_present[1:]):
            raise ValueError(
                f"conflicting aliases for {canonical}: "
                + ", ".join(name for name, _ in normalized_present)
            )
        normalized[canonical] = first_value
    if not is_result:
        if "takeover" in values and not isinstance(values["takeover"], bool):
            raise ValueError("takeover must be a boolean")
        if "source" in values and not isinstance(values["source"], Mapping):
            raise ValueError("source must be an object")
    return normalized
