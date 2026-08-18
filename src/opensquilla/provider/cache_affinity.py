"""Private, deterministic primitives for router-dynamic KV-cache affinity.

This module deliberately owns no routing state and does not import the engine
pricing layer.  Runtime code proves deployment/cache-domain continuity and
passes only frozen, non-secret evidence into the ranker.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import posixpath
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

CacheRole = Literal["single", "proposer", "aggregator"]
CacheTopology = Literal["single", "multiple"]
CacheEvidenceKind = Literal["read_hit", "write_only"]

_CACHE_ROLES = frozenset({"single", "proposer", "aggregator"})
_CACHE_TOPOLOGIES = frozenset({"single", "multiple"})
_CACHE_EVIDENCE_KINDS = frozenset({"read_hit", "write_only"})
_CREDENTIAL_TOKEN_PROTOCOL = b"opensquilla-cache-credential-namespace-v1"
_CACHE_DOMAIN_PROTOCOL = b"opensquilla-router-dynamic-cache-domain-v1"


@lru_cache(maxsize=1)
def _credential_hmac_key() -> bytes:
    """Allocate the process-local secret only when affinity is enabled."""

    return secrets.token_bytes(32)


def _finite_number(value: object, *, name: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} must be a finite number >= {minimum}")
    return number


def _exact_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _length_prefixed(parts: Sequence[bytes]) -> bytes:
    encoded = bytearray()
    for part in parts:
        encoded.extend(len(part).to_bytes(8, "big"))
        encoded.extend(part)
    return bytes(encoded)


class CredentialNamespaceToken:
    """Opaque, process-local proof of an exact credential namespace."""

    __slots__ = ("__digest",)

    def __init__(self, digest: bytes) -> None:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("credential namespace token has invalid length")
        self.__digest = digest

    def __repr__(self) -> str:
        return "<CredentialNamespaceToken opaque>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CredentialNamespaceToken) and hmac.compare_digest(
            self.__digest,
            other.__digest,
        )

    def __hash__(self) -> int:
        return hash(self.__digest)

    def __copy__(self) -> CredentialNamespaceToken:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> CredentialNamespaceToken:
        del memo
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("credential namespace tokens cannot be serialized")

    def _guard_material(self) -> bytes:
        """Return opaque bytes solely for constructing a cache-domain guard."""

        return self.__digest


def build_credential_namespace_token(
    *,
    provider: str,
    resolved_secret: str,
    org_id: str = "",
    tenant_headers: Mapping[str, str] | None = None,
) -> CredentialNamespaceToken | None:
    """HMAC the resolved credential namespace without retaining raw inputs."""

    provider_id = str(provider or "").strip().casefold()
    if not provider_id:
        return None
    normalized_headers: list[tuple[str, str]] = []
    for raw_name, raw_value in (tenant_headers or {}).items():
        name = str(raw_name or "").strip().casefold()
        value = str(raw_value or "").strip()
        if not name or not value:
            return None
        normalized_headers.append((name, value))
    normalized_headers.sort()
    if len({name for name, _ in normalized_headers}) != len(normalized_headers):
        return None
    parts = [
        _CREDENTIAL_TOKEN_PROTOCOL,
        provider_id.encode(),
        str(resolved_secret or "").encode(),
        str(org_id or "").strip().encode(),
    ]
    for name, value in normalized_headers:
        parts.extend((name.encode(), value.encode()))
    digest = hmac.new(
        _credential_hmac_key(),
        _length_prefixed(parts),
        hashlib.sha256,
    ).digest()
    return CredentialNamespaceToken(digest)


class CacheDomainGuard:
    """Opaque equality token for one conservative upstream cache domain."""

    __slots__ = ("__digest",)

    def __init__(self, digest: bytes) -> None:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("cache domain guard has invalid length")
        self.__digest = digest

    def __repr__(self) -> str:
        return "<CacheDomainGuard opaque>"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CacheDomainGuard) and hmac.compare_digest(
            self.__digest,
            other.__digest,
        )

    def __hash__(self) -> int:
        return hash(self.__digest)

    def __copy__(self) -> CacheDomainGuard:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> CacheDomainGuard:
        del memo
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("cache domain guards cannot be serialized")


def canonical_cache_endpoint(base_url: str) -> str | None:
    """Return a conservative canonical endpoint or ``None`` on ambiguity."""

    try:
        parsed = urlsplit(str(base_url or "").strip())
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = (parsed.hostname or "").casefold()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    effective_port = port or (443 if scheme == "https" else 80)
    raw_path = parsed.path or "/"
    normalized_path = posixpath.normpath("/" + raw_path.lstrip("/"))
    if raw_path.endswith("/") and normalized_path != "/":
        normalized_path += "/"
    return f"{scheme}://{host}:{effective_port}{normalized_path}"


def build_cache_domain_guard(
    *,
    session_epoch: int,
    role: CacheRole,
    topology: CacheTopology,
    provider: str,
    requested_model: str,
    base_url: str,
    upstream_provider: str = "",
    provider_routing_strict: bool = False,
    allow_fallbacks: bool = True,
    thinking_enabled: bool,
    effective_thinking_level: str,
    thinking_budget_tokens: int | None,
    credential_namespace_token: CredentialNamespaceToken | None,
) -> CacheDomainGuard | None:
    """Build a symmetric guard only when the physical cache domain is proven."""

    if (
        isinstance(session_epoch, bool)
        or not isinstance(session_epoch, int)
        or session_epoch < 0
        or role not in _CACHE_ROLES
        or topology not in _CACHE_TOPOLOGIES
        or (topology == "single") != (role == "single")
        or not isinstance(provider_routing_strict, bool)
        or not isinstance(allow_fallbacks, bool)
        or not isinstance(thinking_enabled, bool)
        or credential_namespace_token is None
    ):
        return None
    provider_id = str(provider or "").strip().casefold()
    model_id = str(requested_model or "").strip()
    upstream = str(upstream_provider or "").strip().casefold()
    thinking_level = str(effective_thinking_level or "").strip().casefold()
    if (
        not thinking_level
        or isinstance(thinking_budget_tokens, bool)
        or (
            thinking_budget_tokens is not None
            and (
                not isinstance(thinking_budget_tokens, int)
                or thinking_budget_tokens < 0
            )
        )
    ):
        return None
    thinking_budget = (
        b"none"
        if thinking_budget_tokens is None
        else str(thinking_budget_tokens).encode()
    )
    endpoint = canonical_cache_endpoint(base_url)
    if not provider_id or not model_id or endpoint is None:
        return None
    if provider_id == "openrouter" and (
        not upstream or upstream == "auto" or not provider_routing_strict or allow_fallbacks
    ):
        return None
    material = _length_prefixed(
        [
            _CACHE_DOMAIN_PROTOCOL,
            str(session_epoch).encode(),
            role.encode(),
            topology.encode(),
            provider_id.encode(),
            model_id.encode(),
            endpoint.encode(),
            upstream.encode(),
            b"1" if provider_routing_strict else b"0",
            b"1" if allow_fallbacks else b"0",
            b"1" if thinking_enabled else b"0",
            thinking_level.encode(),
            thinking_budget,
            credential_namespace_token._guard_material(),
        ]
    )
    return CacheDomainGuard(hashlib.sha256(material).digest())


@dataclass(frozen=True, slots=True)
class CacheAffinityReceipt:
    physical_attempt_id: str
    role: CacheRole
    topology: CacheTopology
    execution_slot: str
    requested_identity: str
    actual_identity: str
    cache_domain_guard: CacheDomainGuard
    evidence_kind: CacheEvidenceKind
    cached_tokens: int
    cache_write_tokens: int
    observed_at_monotonic: float


def build_cache_affinity_receipt(
    *,
    physical_attempt_id: str,
    role: CacheRole,
    topology: CacheTopology,
    execution_slot: str | int,
    requested_identity: str,
    actual_identity: str,
    cache_domain_guard: CacheDomainGuard | None,
    cached_tokens: object,
    cache_write_tokens: object,
    observed_at_monotonic: object,
) -> CacheAffinityReceipt | None:
    """Create one successful physical receipt, failing closed on ambiguity."""

    attempt_id = str(physical_attempt_id or "").strip()
    slot = str(execution_slot).strip()
    requested = str(requested_identity or "").strip()
    actual = str(actual_identity or "").strip()
    read_tokens = _exact_nonnegative_int(cached_tokens)
    write_tokens = _exact_nonnegative_int(cache_write_tokens)
    if (
        not attempt_id
        or role not in _CACHE_ROLES
        or topology not in _CACHE_TOPOLOGIES
        or (topology == "single") != (role == "single")
        or not slot
        or not requested
        or requested.casefold() != actual.casefold()
        or cache_domain_guard is None
        or read_tokens is None
        or write_tokens is None
    ):
        return None
    try:
        observed_at = _finite_number(
            observed_at_monotonic,
            name="observed_at_monotonic",
        )
    except ValueError:
        return None
    if read_tokens > 0:
        evidence_kind: CacheEvidenceKind = "read_hit"
    elif write_tokens > 0:
        evidence_kind = "write_only"
    else:
        return None
    return CacheAffinityReceipt(
        physical_attempt_id=attempt_id,
        role=role,
        topology=topology,
        execution_slot=slot,
        requested_identity=requested,
        actual_identity=actual,
        cache_domain_guard=cache_domain_guard,
        evidence_kind=evidence_kind,
        cached_tokens=read_tokens,
        cache_write_tokens=write_tokens,
        observed_at_monotonic=observed_at,
    )


def cache_affinity_decay_factor(
    *,
    observed_at_monotonic: object,
    now_monotonic: object,
    ttl_seconds: object,
    age_decay: str,
) -> float:
    """Compute the configured per-receipt TTL/decay factor."""

    try:
        observed_at = _finite_number(
            observed_at_monotonic,
            name="observed_at_monotonic",
        )
        now = _finite_number(now_monotonic, name="now_monotonic")
        ttl = _finite_number(ttl_seconds, name="ttl_seconds")
    except ValueError:
        return 0.0
    if ttl <= 0.0 or now < observed_at:
        return 0.0
    age = now - observed_at
    if age > ttl:
        return 0.0
    if age_decay == "none":
        return 1.0
    if age_decay == "linear":
        return max(0.0, 1.0 - age / ttl)
    return 0.0


@dataclass(frozen=True, slots=True)
class CachePriceQuote:
    """No-network total-bucket rates bound to one deployment scope."""

    provider: str
    canonical_model: str
    endpoint_scope: str
    upstream_scope: str
    price_source: str
    normal_input_per_million: float
    normal_output_per_million: float
    cache_read_per_million: float
    cache_write_per_million: float
    cache_bucket_rates_are_total: bool = True

    def __post_init__(self) -> None:
        for name in (
            "provider",
            "canonical_model",
            "endpoint_scope",
            "upstream_scope",
            "price_source",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"cache price quote {name} must be non-empty")
        if canonical_cache_endpoint(self.endpoint_scope) != self.endpoint_scope:
            raise ValueError(
                "cache price quote endpoint_scope must be canonical"
            )
        for name in (
            "normal_input_per_million",
            "normal_output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
        ):
            _finite_number(getattr(self, name), name=name)
        if self.cache_bucket_rates_are_total is not True:
            raise ValueError("cache price quote must use total bucket rates")


@dataclass(frozen=True, slots=True)
class CacheAffinityEvidenceInput:
    """Frozen, guard-verified evidence safe to pass into deterministic ranking."""

    identity: str
    role: CacheRole
    evidence_kind: CacheEvidenceKind
    cached_tokens: int
    cache_write_tokens: int
    decay_factor: float
    price_quote: CachePriceQuote | None = None
    ranking_price_source: str = ""
    endpoint_scope: str = ""
    upstream_scope: str = ""

    def __post_init__(self) -> None:
        if not self.identity.strip() or self.role not in _CACHE_ROLES:
            raise ValueError("cache affinity evidence identity/role is invalid")
        if self.evidence_kind not in _CACHE_EVIDENCE_KINDS:
            raise ValueError("cache affinity evidence kind is invalid")
        if (
            _exact_nonnegative_int(self.cached_tokens) is None
            or _exact_nonnegative_int(self.cache_write_tokens) is None
        ):
            raise ValueError("cache affinity token evidence must be exact non-negative ints")
        if self.evidence_kind == "read_hit" and self.cached_tokens <= 0:
            raise ValueError("read-hit evidence requires cached_tokens > 0")
        if self.evidence_kind == "write_only" and (
            self.cached_tokens != 0 or self.cache_write_tokens <= 0
        ):
            raise ValueError("write-only evidence requires only cache_write_tokens > 0")
        decay = _finite_number(self.decay_factor, name="decay_factor")
        if decay > 1.0:
            raise ValueError("cache affinity decay_factor must be <= 1")
        if self.price_quote is not None and (
            not isinstance(self.ranking_price_source, str)
            or not self.ranking_price_source.strip()
        ):
            raise ValueError(
                "expected-cost cache evidence requires a ranking price source"
            )
        if self.price_quote is not None and (
            not self.endpoint_scope.strip()
            or self.endpoint_scope != self.price_quote.endpoint_scope
            or self.upstream_scope.strip().casefold()
            != self.price_quote.upstream_scope.strip().casefold()
        ):
            raise ValueError(
                "expected-cost cache evidence must bind the quote deployment scope"
            )


@dataclass(frozen=True, slots=True)
class CacheAffinityScoreAdjustment:
    identity: str
    role: CacheRole
    strategy: str
    evidence_kind: CacheEvidenceKind
    decay_factor: float
    score_adjustment: float
    input_tokens: int = 0
    cache_tokens: int = 0
    observed_cache_tokens: int = 0
    hit_probability: float = 0.0
    price_source: str = ""
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0
    baseline_input_cost_usd: float = 0.0
    cache_hit_input_cost_usd: float = 0.0
    cache_miss_input_cost_usd: float = 0.0
    effective_input_per_million: float = 0.0
    cost_normalized_before: float = 0.0
    cost_normalized_after: float = 0.0

    def __post_init__(self) -> None:
        if not self.identity.strip() or self.role not in _CACHE_ROLES:
            raise ValueError("cache affinity adjustment identity/role is invalid")
        if self.strategy not in {"bonus", "expected_cost"}:
            raise ValueError("cache affinity adjustment strategy is invalid")
        if self.evidence_kind not in _CACHE_EVIDENCE_KINDS:
            raise ValueError("cache affinity adjustment evidence is invalid")
        decay = _finite_number(self.decay_factor, name="decay_factor")
        if decay > 1.0:
            raise ValueError("cache affinity adjustment decay_factor must be <= 1")
        for name in (
            "score_adjustment",
            "hit_probability",
            "cache_read_per_million",
            "cache_write_per_million",
            "baseline_input_cost_usd",
            "cache_hit_input_cost_usd",
            "cache_miss_input_cost_usd",
            "effective_input_per_million",
            "cost_normalized_before",
            "cost_normalized_after",
        ):
            minimum = -math.inf if name == "score_adjustment" else 0.0
            _finite_number(getattr(self, name), name=name, minimum=minimum)
        if not 0.0 <= self.hit_probability <= 1.0:
            raise ValueError("cache affinity hit_probability must be in [0, 1]")
        if not 0.0 <= self.cost_normalized_before <= 1.0:
            raise ValueError("cache affinity cost_normalized_before must be in [0, 1]")
        if not 0.0 <= self.cost_normalized_after <= 1.0:
            raise ValueError("cache affinity cost_normalized_after must be in [0, 1]")
        if (
            _exact_nonnegative_int(self.input_tokens) is None
            or _exact_nonnegative_int(self.cache_tokens) is None
            or _exact_nonnegative_int(self.observed_cache_tokens) is None
            or self.cache_tokens > self.input_tokens
        ):
            raise ValueError("cache affinity adjustment token counts are invalid")

    def trace(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "identity": self.identity,
            "role": self.role,
            "strategy": self.strategy,
            "evidence_kind": self.evidence_kind,
            "decay_factor": self.decay_factor,
            "score_adjustment": self.score_adjustment,
        }
        if self.strategy == "expected_cost":
            payload.update(
                {
                    "N": self.input_tokens,
                    "K": self.cache_tokens,
                    "cache_evidence_tokens": self.observed_cache_tokens,
                    "p": self.hit_probability,
                    "price_source": self.price_source,
                    "cache_read_per_million": self.cache_read_per_million,
                    "cache_write_per_million": self.cache_write_per_million,
                    "C0": self.baseline_input_cost_usd,
                    "Chit": self.cache_hit_input_cost_usd,
                    "Cmiss": self.cache_miss_input_cost_usd,
                    "r_input_eff": self.effective_input_per_million,
                    "cost_normalized_before": self.cost_normalized_before,
                    "cost_normalized_after": self.cost_normalized_after,
                }
            )
        return payload


def cache_affinity_score_adjustment(
    *,
    policy: Mapping[str, object],
    evidence: CacheAffinityEvidenceInput,
    role: CacheRole,
    topology: CacheTopology,
    intent_type: str,
    intent_confidence: float,
    intent_confidence_threshold: float,
    estimated_input_tokens: int,
    tool_log_tokens: int,
    candidate_output_tokens: int,
    proposer_count: int,
    ranking_input_per_million: float | None,
    ranking_output_per_million: float | None,
    ranking_price_source: str | None,
    price_input_weight: float,
    price_output_weight: float,
    price_reference_usd_per_million: float,
    cost_weight: float,
) -> CacheAffinityScoreAdjustment:
    """Return the one permitted cache adjustment at a role-specific score seam."""

    strategy = str(policy.get("strategy") or "")
    topologies = policy.get("topologies")
    topology_values = (
        {str(value) for value in topologies}
        if isinstance(topologies, Sequence) and not isinstance(topologies, (str, bytes, bytearray))
        else set()
    )
    zero = CacheAffinityScoreAdjustment(
        identity=evidence.identity,
        role=role,
        strategy=strategy,
        evidence_kind=evidence.evidence_kind,
        decay_factor=evidence.decay_factor,
        score_adjustment=0.0,
    )
    try:
        confidence = _finite_number(
            intent_confidence,
            name="intent_confidence",
        )
        confidence_threshold = _finite_number(
            intent_confidence_threshold,
            name="intent_confidence_threshold",
        )
    except ValueError:
        return zero
    if (
        role != evidence.role
        or topology not in _CACHE_TOPOLOGIES
        or topology not in topology_values
        or (topology == "single") != (role == "single")
        or intent_type != "continue"
        or confidence > 1.0
        or confidence_threshold > 1.0
        or confidence < confidence_threshold
        or evidence.decay_factor <= 0.0
    ):
        return zero
    if strategy == "bonus":
        configured = policy.get("bonus_by_evidence")
        if not isinstance(configured, Mapping):
            return zero
        try:
            bonus = _finite_number(
                configured.get(evidence.evidence_kind),
                name="cache affinity bonus",
            )
        except ValueError:
            return zero
        score_adjustment = bonus * evidence.decay_factor
        if not math.isfinite(score_adjustment):
            return zero
        return CacheAffinityScoreAdjustment(
            identity=evidence.identity,
            role=role,
            strategy=strategy,
            evidence_kind=evidence.evidence_kind,
            decay_factor=evidence.decay_factor,
            score_adjustment=score_adjustment,
        )
    if strategy != "expected_cost" or evidence.price_quote is None:
        return zero
    probabilities = policy.get("hit_probability_by_evidence")
    if not isinstance(probabilities, Mapping):
        return zero
    try:
        base_probability = _finite_number(
            probabilities.get(evidence.evidence_kind),
            name="cache affinity hit probability",
        )
    except ValueError:
        return zero
    if base_probability > 1.0:
        return zero
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (
            estimated_input_tokens,
            tool_log_tokens,
            candidate_output_tokens,
            proposer_count,
        )
    ):
        return zero
    input_tokens = estimated_input_tokens + tool_log_tokens
    if role == "aggregator":
        input_tokens += proposer_count * candidate_output_tokens
    if input_tokens <= 0:
        return zero
    cache_tokens = min(
        input_tokens,
        evidence.cached_tokens
        if evidence.evidence_kind == "read_hit"
        else evidence.cache_write_tokens,
    )
    if cache_tokens <= 0:
        return zero
    quote = evidence.price_quote
    identity_provider, separator, identity_model = evidence.identity.partition(":")
    if (
        not separator
        or quote.provider.casefold() != identity_provider.casefold()
        or quote.canonical_model.casefold() != identity_model.casefold()
        or ranking_input_per_million is None
        or ranking_output_per_million is None
        or not str(ranking_price_source or "").strip()
        or quote.price_source != str(ranking_price_source).strip()
        or evidence.ranking_price_source != str(ranking_price_source).strip()
        or quote.endpoint_scope != evidence.endpoint_scope
        or quote.upstream_scope.strip().casefold()
        != evidence.upstream_scope.strip().casefold()
        or quote.normal_input_per_million != ranking_input_per_million
        or quote.normal_output_per_million != ranking_output_per_million
        or quote.cache_bucket_rates_are_total is not True
    ):
        return zero
    try:
        input_weight = _finite_number(price_input_weight, name="price_input_weight")
        output_weight = _finite_number(price_output_weight, name="price_output_weight")
        reference = _finite_number(
            price_reference_usd_per_million,
            name="price_reference_usd_per_million",
        )
        score_cost_weight = _finite_number(cost_weight, name="cost_weight")
    except ValueError:
        return zero
    if reference <= 0.0:
        return zero
    try:
        probability = base_probability * evidence.decay_factor
        normal_tokens = input_tokens - cache_tokens
        baseline = input_tokens * quote.normal_input_per_million / 1_000_000
        hit = (
            normal_tokens * quote.normal_input_per_million
            + cache_tokens * quote.cache_read_per_million
        ) / 1_000_000
        miss = (
            normal_tokens * quote.normal_input_per_million
            + cache_tokens * quote.cache_write_per_million
        ) / 1_000_000
        expected_input_cost = probability * hit + (1.0 - probability) * miss
        effective_input_rate = expected_input_cost / input_tokens * 1_000_000
        old_rate = (
            input_weight * ranking_input_per_million
            + output_weight * ranking_output_per_million
        )
        new_rate = old_rate + input_weight * (
            effective_input_rate - ranking_input_per_million
        )
        normalized_before = max(0.0, min(1.0, old_rate / reference))
        normalized_after = max(0.0, min(1.0, new_rate / reference))
        adjustment = score_cost_weight * (
            normalized_before - normalized_after
        )
    except (OverflowError, ValueError, ZeroDivisionError):
        return zero
    if not all(
        math.isfinite(value)
        for value in (
            probability,
            baseline,
            hit,
            miss,
            expected_input_cost,
            effective_input_rate,
            old_rate,
            new_rate,
            normalized_before,
            normalized_after,
            adjustment,
        )
    ):
        return zero
    return CacheAffinityScoreAdjustment(
        identity=evidence.identity,
        role=role,
        strategy=strategy,
        evidence_kind=evidence.evidence_kind,
        decay_factor=evidence.decay_factor,
        score_adjustment=adjustment,
        input_tokens=input_tokens,
        cache_tokens=cache_tokens,
        observed_cache_tokens=(
            evidence.cached_tokens
            if evidence.evidence_kind == "read_hit"
            else evidence.cache_write_tokens
        ),
        hit_probability=probability,
        price_source=quote.price_source,
        cache_read_per_million=quote.cache_read_per_million,
        cache_write_per_million=quote.cache_write_per_million,
        baseline_input_cost_usd=baseline,
        cache_hit_input_cost_usd=hit,
        cache_miss_input_cost_usd=miss,
        effective_input_per_million=effective_input_rate,
        cost_normalized_before=normalized_before,
        cost_normalized_after=normalized_after,
    )
