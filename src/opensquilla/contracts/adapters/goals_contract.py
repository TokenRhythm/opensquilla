"""Typed validation seam for the durable Goals v4 RPCs.

The adapter deliberately does not call GoalService.  It validates and projects
wire payloads while keeping the existing Gateway handlers and state machine
unchanged; a later migration can plug it into the registration boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError

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


def _errors(exc: ValidationError) -> tuple[dict[str, Any], ...]:
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
    except ValidationError as exc:
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
    except ValidationError as exc:
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
