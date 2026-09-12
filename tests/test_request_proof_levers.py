"""Compaction safety levers, including explicit rollback compatibility.

Covers OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS,
OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_ERROR_RESULTS,
OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS,
OPENSQUILLA_PROVIDER_COMPACTION_SKIP_PROJECTED,
OPENSQUILLA_PROVIDER_COMPACTION_NEVER_WORSE.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from opensquilla.provider import request_proof as request_proof_module
from opensquilla.provider.request_proof import (
    ProviderRequestBudgetExceededError,
    _compact_argument_string,
    _compact_tool_arguments,
    _compact_tool_arguments_for_final_cap,
    _compact_tool_input,
    _compact_tool_payload_once,
    _payload_chars,
    prove_or_compact_provider_payload,
)

PROTECT_RECENT_RESULTS_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS"
PROTECT_ERROR_RESULTS_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_ERROR_RESULTS"
PROTECT_UNRESOLVED_RESULTS_ENV = (
    "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS"
)
SKIP_PROJECTED_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_SKIP_PROJECTED"
STUB_PREVIEW_CHARS_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_STUB_PREVIEW_CHARS"
NEVER_WORSE_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_NEVER_WORSE"

_ALL_LEVER_ENVS = (
    "OPENSQUILLA_PROVIDER_COMPACTION_TINY_GUARD_CHARS",
    "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT",
    PROTECT_RECENT_RESULTS_ENV,
    PROTECT_ERROR_RESULTS_ENV,
    PROTECT_UNRESOLVED_RESULTS_ENV,
    SKIP_PROJECTED_ENV,
    STUB_PREVIEW_CHARS_ENV,
    NEVER_WORSE_ENV,
    "OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS",
)


@pytest.fixture(autouse=True)
def _clean_lever_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL_LEVER_ENVS:
        monkeypatch.delenv(name, raising=False)
    for name in (
        "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT",
        PROTECT_RECENT_RESULTS_ENV,
        PROTECT_ERROR_RESULTS_ENV,
        PROTECT_UNRESOLVED_RESULTS_ENV,
        SKIP_PROJECTED_ENV,
        NEVER_WORSE_ENV,
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setattr(
        request_proof_module,
        "_serialized_token_estimate",
        lambda serialized: (max(1, len(serialized) // 4), "legacy_test_estimate"),
    )


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _legacy_proof_view(proof: dict[str, Any]) -> dict[str, Any]:
    """Remove token-aware additions before comparing rollback goldens."""

    projected = dict(proof)
    for key in (
        "estimated_text_tokens",
        "raw_proof_token_budget",
        "effective_proof_token_budget",
        "fits_char_budget",
        "fits_token_budget",
        "usage_source",
        "token_estimate_source",
        "usage_confidence",
    ):
        projected.pop(key, None)
    projected["estimated_tokens"] = max(1, int(projected["estimated_chars"]) // 4)
    projected["provider_window_mismatch"] = False
    return projected


def _golden_payload() -> dict[str, Any]:
    filler = "0123456789abcdef"
    result_a = "alpha result line " + filler * 100
    result_b = "bravo result line " + filler * 110
    result_c = "charlie result line " + filler * 90
    long_command = "run build step && inspect artifacts " * 26
    reasoning = "weighing tradeoffs before acting " + filler * 55
    note = "context note " + filler * 45
    return {
        "model": "synthetic-model",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a synthetic conversation fixture."},
            {"role": "user", "content": "Summarise the build results. " + filler * 20},
            {
                "role": "assistant",
                "content": "Starting with the build.",
                "reasoning_content": reasoning,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps(
                                {"command": long_command, "workdir": "/srv/project"}
                            ),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": result_a},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Reviewing the output. " + filler * 30},
                    {
                        "type": "tool_use",
                        "id": "call-2",
                        "name": "read_file",
                        "input": {"path": "/srv/project/main.py", "note": note},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-2",
                        "content": result_b,
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-3", "content": result_c},
            {"role": "user", "content": "Now write the summary. " + filler * 12},
        ],
    }


# --- begin baseline goldens ---
_GOLDEN_TIER_BUDGETS: dict[int, int] = {0: 10300, 1: 8800, 2: 8500, 3: 5500, 4: 3600}
_GOLDEN_PAYLOAD_JSON: dict[int, str] = {
    10300: (
        '{"model":"synthetic-model","temperature":0,"messages":[{"role":"system","content":"You a'
        're a synthetic conversation fixture."},{"role":"user","content":"Summarise the build res'
        'ults. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"role":"assistant","co'
        'ntent":"Starting with the build.","reasoning_content":"weighing tradeoffs before acting '
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '","tool_calls":[{"id":"call-1","type":"function","function":{"name":"run_command","argum'
        'ents":"{\\"command\\": \\"run build step && inspect artifacts run build step && inspect'
        ' artifacts run build step && inspect artifacts run build step && inspect artifacts run b'
        'uild step && inspect artifacts run build step && inspect artifacts run build step && ins'
        'pect artifacts run build step && inspect artifacts run build step && inspect artifacts r'
        'un build step && inspect artifacts run build step && inspect artifacts run build step &&'
        ' inspect artifacts run build step && inspect artifacts run build step && inspect artifac'
        'ts run build step && inspect artifacts run build step && inspect artifacts run build ste'
        'p && inspect artifacts run build step && inspect artifacts run build step && inspect art'
        'ifacts run build step && inspect artifacts run build step && inspect artifacts run build'
        ' step && inspect artifacts run build step && inspect artifacts run build step && inspect'
        ' artifacts run build step && inspect artifacts run build step && inspect artifacts \\'
        '", \\"workdir\\": \\"/srv/project\\"}"}}]},{"role":"tool","tool_call_id":"call-1","conte'
        'nt":"alpha result line 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef"},{"role":"assistant","content":[{"type":"text",'
        '"text":"Reviewing the output. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"type":"tool_u'
        'se","id":"call-2","name":"read_file","input":{"path":"/srv/project/main.py","note":"cont'
        'ext note 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456\\n\\n[provider_reques'
        't_tool_input_compacted: omitted 193 chars; original_chars=733; sha256=2fe12f2503fe311664'
        'e4674f956f0fd497a5265e4a572c68022a627d2161a966]\\n\\n89abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"'
        '}}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"call-2","content":"b'
        'ravo result line 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef"}]},{"role":"tool","tool_call_id":"call-3","content":"charlie result l'
        'ine 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef"},{"role":"user","content":"Now write the summary. '
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef"}]}'
    ),
    8800: (
        '{"model":"synthetic-model","temperature":0,"messages":[{"role":"system","content":"You a'
        're a synthetic conversation fixture."},{"role":"user","content":"Summarise the build res'
        'ults. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"role":"assistant","co'
        'ntent":"Starting with the build.","reasoning_content":"weighing tradeoffs before acting '
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '","tool_calls":[{"id":"call-1","type":"function","function":{"name":"run_command","argum'
        'ents":"{\\"command\\": \\"run build step && inspect artifacts run build step && inspect'
        ' artifacts run build step && inspect artifacts run build step && inspect artifacts run b'
        'uild step && inspect artifacts run build step && inspect artifacts run build step && ins'
        'pect artifacts run build step && inspect artifacts run build step && inspect artifacts r'
        'un build step && inspect artifacts run build step && inspect artifacts run build step &&'
        ' inspect artifacts run build step && inspect artifacts run build step && inspect artifac'
        'ts run build step && inspect artifacts run build step && inspect artifacts run build ste'
        'p && inspect artifacts run build step && inspect artifacts run build step && inspect art'
        'ifacts run build step && inspect artifacts run build step && inspect artifacts run build'
        ' step && inspect artifacts run build step && inspect artifacts run build step && inspect'
        ' artifacts run build step && inspect artifacts run build step && inspect artifacts \\'
        '", \\"workdir\\": \\"/srv/project\\"}"}}]},{"role":"tool","tool_call_id":"call-1","conte'
        'nt":"alpha result line 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef01\\n\\n[provider_request_compacted: omitted 518 chars]\\n\\n89ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef"},{"role":"assistant","content":[{"type":"text","text":"Reviewing t'
        'he output. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456789abcdef0123456789abcdef0123456789abcdef"},{"type":"tool_use","id":"call-2","'
        'name":"read_file","input":{"path":"/srv/project/main.py","note":"context note 0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456\\n\\n[provider_request_tool_input_compac'
        'ted: omitted 193 chars; original_chars=733; sha256=2fe12f2503fe311664e4674f956f0fd497a52'
        '65e4a572c68022a627d2161a966]\\n\\n89abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}]},{"role":"user"'
        ',"content":[{"type":"tool_result","tool_use_id":"call-2","content":"bravo result line 01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '\\n\\n[provider_request_compacted: omitted 678 chars]\\n\\n89abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]},'
        '{"role":"tool","tool_call_id":"call-3","content":"charlie result line 0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\\n\\n[provider_'
        'request_compacted: omitted 360 chars]\\n\\n89abcdef0123456789abcdef0123456789abcdef012'
        '3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a'
        'bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"role":"user","'
        'content":"Now write the summary. 0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef"}]}'
    ),
    8500: (
        '{"model":"synthetic-model","temperature":0,"messages":[{"role":"system","content":"You'
        ' are a synthetic conversation fixture."},{"role":"user","content":"Summarise the build'
        ' results. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"role":"as'
        'sistant","content":"Starting with the build.","reasoning_content":"weighing tradeoffs '
        'before acting 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef","tool_calls":[{"id":"call-1","type":"function","fu'
        'nction":{"name":"run_command","arguments":"{\\"command\\":\\"run build step && inspect ar'
        'tifacts run build step && inspect artifacts run build step && inspect artifacts run bu'
        'ild step && inspect artifacts run build step && inspect artifacts run build step && in'
        'spect artifacts run build step && inspect artifacts run build step && inspect artifact'
        's run build step && inspect artifacts run build step && inspect artifacts run build st'
        'ep && inspect artifacts run build step && inspec\\\\n\\\\n[provider_request_tool_input_com'
        'pacted: omitted 396 chars; original_chars=936; sha256=03fe833d8d0ffb378d437a3e2a5f2341'
        'df51d5e8db67fdeddce81126a20ccd3e]\\\\n\\\\nt artifacts run build step && inspect artifacts'
        ' run build step && inspect artifacts run build step && inspect artifacts \\",\\"workdir\\'
        '":\\"/srv/project\\"}"}}]},{"role":"tool","tool_call_id":"call-1","content":"alpha resul'
        't line 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a'
        'bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012'
        '3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef01\\n\\n[provider_request_compacted: omitted 518 chars]\\n\\n89'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef"},{"role":"assistant","content":[{"type":"text","text":"Rev'
        'iewing the output. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012'
        '3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a'
        'bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"type":"tool_'
        'use","id":"call-2","name":"read_file","input":{"path":"/srv/project/main.py","note":"c'
        'ontext note 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456\\n\\n[provid'
        'er_request_tool_input_compacted: omitted 155 chars; original_chars=695; sha256=2733247'
        'a9bc6e597f6630ddcddb84eba87be620c047ba001f7238d4af8477d85]\\n\\n89abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef"}}]},{"role":"user","content":[{"type":"tool_result","tool_use_id":"call-2"'
        ',"content":"bravo result line 0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef01\\n\\n[provider_request_compacted: o'
        'mitted 678 chars]\\n\\n89abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde'
        'f0123456789abcdef0123456789abcdef0123456789abcdef"}]},{"role":"tool","tool_call_id":"c'
        'all-3","content":"charlie result line 0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\\n\\n[provider_request_compac'
        'ted: omitted 360 chars]\\n\\n89abcdef0123456789abcdef0123456789abcdef0123456789abcdef012'
        '3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcdef"},{"role":"user","content":"No'
        'w write the summary. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef"}]}'
    ),
    5500: (
        '{"model":"synthetic-model","temperature":0,"messages":[{"role":"system","content":"You'
        ' are a synthetic conversation fixture."},{"role":"user","content":"Summarise the build'
        ' results. 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456\\n\\n[provid'
        'er_request_user_context_emergency_compacted: omitted 129 chars; original_chars=349; sh'
        'a256=1b95b386d19eac8884426943ac35590ace8b2e23a6034abe983121b27e6913da]\\n\\n89abcdef0123'
        '456789abcdef0123456789abcdef"},{"role":"assistant","content":"Starting with the build.'
        '","reasoning_content":"weighing tradeoffs before acting 0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","tool_ca'
        'lls":[{"id":"call-1","type":"function","function":{"name":"run_command","arguments":"{'
        '\\"command\\":\\"run build step && inspect artifacts run build step && inspect artifacts '
        'run build step && inspect artifacts run build step && inspect artifacts run build step'
        ' && inspec\\n\\n[provider_request_tool_arguments_emergency_compacted: omitted 518 chars;'
        ' original_chars=738; sha256=d5a4e74e6f2d1d7dd0bd4b91fe5365393822c7fd638fc84fc1e2e465b5'
        '1aee7e]\\n\\nct artifacts \\",\\"workdir\\":\\"/s'
        'rv/project\\"}"}}]},{"role":"tool","tool_cal'
        'l_id":"call-1","content":"alpha result line 0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef01\\n\\n[provider_request_tool_content_emergency_compact'
        'ed: omitted 931 chars; original_chars=1151; sha256=7883df0b8b724e54d021df559dd8d888503'
        '065a326d6aed48a41801aaf32b7d5]\\n\\n89abcdef0123456789abcdef0123456789abcdef"},{"role":"'
        'assistant","content":[{"type":"text","text":"Reviewing the output. 0123456789abcdef012'
        '3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef0123456789abcd\\n\\n[provider_request_text_block_'
        'emergency_compacted: omitted 282 chars; original_chars=502; sha256=376f702641d0b312ad0'
        '8bf169e28cabcfbc48f1f2f21bd3fbb004def27505f6a]\\n\\n89abcdef0123456789abcdef0123456789ab'
        'cdef"},{"type":"tool_use","id":"call-2","name":"read_file","input":{"path":"/srv/proje'
        'ct/main.py","note":"context note 0123456789abcdef0123456789abcdef0123456789abcdef01234'
        '56789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789a'
        'bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456\\n\\n[provider_request_tool_input_compacted: omitted 155 chars; original_char'
        's=695; sha256=2733247a9bc6e597f6630ddcddb84eba87be620c047ba001f7238d4af8477d85]\\n\\n89a'
        'bcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0'
        '123456789abcdef0123456789abcdef"}}]},{"role":"user","content":[{"type":"tool_result","'
        'tool_use_id":"call-2","content":"bravo result line 0123456789abcdef0123456789abcdef012'
        '3456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345678'
        '9abcdef0123456789abcdef0123456789abcdef01\\n\\n[provider_request_tool_result_emergency_c'
        'ompacted: omitted 931 chars; original_chars=1151; sha256=ebd1bb15e93761c5f48ae723bbdef'
        'd10062608a036da688accbd42ddd4b26a4c]\\n\\n89abcdef0123456789abcdef0123456789abcdef"}]},{'
        '"role":"tool","tool_call_id":"call-3","content":"charlie result line 0123456789abcdef0'
        '123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\\n\\n[provider_request_tool_co'
        'ntent_emergency_compacted: omitted 931 chars; original_chars=1151; sha256=9320226914fa'
        'db3009965ea28902e8b5f4b175ebccc33282452a3d062038ddce]\\n\\n89abcdef0123456789abcdef01234'
        '56789abcdef"},{"role":"user","content":"Now write the summary. 0123456789abcdef0123456'
        '789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abc'
        'def0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}]'
        '}'
    ),
    3600: (
        '{"model":"synthetic-model","temperature":0,"messages":[{"role":"system","content":"You'
        ' are a synthetic conversation fixture."},{"role":"user","content":"[opensquilla_compac'
        'ted:user_context:387:4ff918646ea190e7]"},{"role":"assistant","content":"Starting with '
        'the build.","reasoning_content":"weighing tradeoffs before acting 0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01'
        '23456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '","tool_calls":[{"id":"call-1","type":"function","function":{"name":"run_command","arg'
        'uments":"{\\"_invalid_provider_context_arguments\\":true}"}}]},{"role":"tool","tool_call'
        '_id":"call-1","content":"[opensquilla_compacted:tool_result:388:3469a086a9891767]"},{"'
        'role":"assistant","content":[{"type":"text","text":"[opensquilla_compacted:assistant_c'
        'ontent_text:385:52ac6665cc08c154]"},{"type":"tool_use","id":"call-2","name":"read_file'
        '","input":{"path":"/srv/project/main.py","note":"context note 0123456789abcdef01234567'
        '89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd'
        'ef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef0123456\\n\\n[provider_request_tool_input_compacted: omi'
        'tted 155 chars; original_chars=695; sha256=2733247a9bc6e597f6630ddcddb84eba87be620c047'
        'ba001f7238d4af8477d85]\\n\\n89abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123'
        '456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}}]},{"role":"user","cont'
        'ent":[{"type":"tool_result","tool_use_id":"call-2","content":"[opensquilla_compacted:u'
        'ser_context_content:387:8d46a9cf6db53c83]"}]},{"role":"tool","tool_call_id":"call-3","'
        'content":"[opensquilla_compacted:tool_result:388:85514ec80c03d75f]"},{"role":"user","c'
        'ontent":"Now write the summary. 0123456789abcdef0123456789abcdef0123456789abcdef012345'
        '6789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab'
        'cdef0123456789abcdef0123456789abcdef0123456789abcdef"}]}'
    ),
}
_GOLDEN_PROOF_JSON: dict[int, str] = {
    10300: (
        '{"projection_adapter":"synthetic_adapter","execution_status_version":1,"status_projectio'
        'n_mode":"native_or_none","estimated_chars":9240,"estimated_tokens":2310,"proof_budget":1'
        '0300,"raw_proof_budget":10300,"effective_proof_budget":9270,"proof_headroom_chars":1030,'
        '"fits":true,"compact_needed":true,"compaction_tier":0,"c'
        'ompaction_protect_recent_assistant":false,"recent_tail_too_large":false,"compaction_not_'
        'smaller":false,"provider_window_mismatch":false,"fallback_reason":null,"top_contributors'
        '":[{"path":"$.messages[5].content[0].content","chars":1778},{"path":"$.messages[3].conte'
        'nt","chars":1618},{"path":"$.messages[6].content","chars":1460},{"path":"$.messages[2].t'
        'ool_calls[0].function.arguments","chars":978},{"path":"$.messages[2].reasoning_content",'
        '"chars":913}],"retry_count":0,"messages_chars":9185,"tools_chars":0,"system_chars":73,"t'
        'op_level_chars":43,"tool_schema_too_large":false,"tool_argument_projection_scrubbed":tru'
        'e}'
    ),
    8800: (
        '{"projection_adapter":"synthetic_adapter","execution_status_version":1,"status_projectio'
        'n_mode":"native_or_none","estimated_chars":7849,"estimated_tokens":1962,"proof_budget":8'
        '800,"raw_proof_budget":8800,"effective_proof_budget":7920,"proof_headroom_chars":880,"fi'
        'ts":true,"compact_needed":true,"compaction_tier":1,"comp'
        'action_protect_recent_assistant":false,"recent_tail_too_large":false,"compaction_not_sma'
        'ller":false,"provider_window_mismatch":false,"fallback_reason":null,"top_contributors":['
        '{"path":"$.messages[3].content","chars":1151},{"path":"$.messages[5].content[0].content"'
        ',"chars":1151},{"path":"$.messages[6].content","chars":1151},{"path":"$.messages[2].tool'
        '_calls[0].function.arguments","chars":978},{"path":"$.messages[2].reasoning_content","ch'
        'ars":913}],"retry_count":1,"messages_chars":7794,"tools_chars":0,"system_chars":73,"top_'
        'level_chars":43,"tool_schema_too_large":false}'
    ),
    8500: (
        '{"projection_adapter":"synthetic_adapter","execution_status_version":1,"status_proje'
        'ction_mode":"native_or_none","estimated_chars":7613,"estimated_tokens":1903,"proof_b'
        'udget":8500,"raw_proof_budget":8500,"effective_proof_budget":7650,"proof_headroom_ch'
        'ars":850,"fits":true,"compact_needed":true,"compaction_tier":2,"compaction_protect_r'
        'ecent_assistant":false,"recent_tail_too_large":false,"compaction_not_smaller":false,'
        '"provider_window_mismatch":false,"fallback_reason":null,"top_contributors":[{"path":'
        '"$.messages[3].content","chars":1151},{"path":"$.messages[5].content[0].content","ch'
        'ars":1151},{"path":"$.messages[6].content","chars":1151},{"path":"$.messages[2].reas'
        'oning_content","chars":913},{"path":"$.messages[2].tool_calls[0].function.arguments"'
        ',"chars":738}],"retry_count":2,"messages_chars":7558,"tools_chars":0,"system_chars":'
        '73,"top_level_chars":43,"tool_schema_too_large":false,"tool_payload_compaction_not_s'
        'maller":false,"tail_compaction_not_smaller":false,"aggregate_tool_arguments_compacte'
        'd":false,"tool_call_arguments_summarized":false}'
    ),
    5500: (
        '{"projection_adapter":"synthetic_adapter","execution_status_version":1,"status_proje'
        'ction_mode":"native_or_none","estimated_chars":4903,"estimated_tokens":1225,"proof_b'
        'udget":5500,"raw_proof_budget":5500,"effective_proof_budget":4950,"proof_headroom_ch'
        'ars":550,"fits":true,"compact_needed":true,"compaction_tier":3,"compaction_protect_r'
        'ecent_assistant":false,"recent_tail_too_large":false,"compaction_not_smaller":false,'
        '"provider_window_mismatch":false,"fallback_reason":null,"top_contributors":[{"path":'
        '"$.messages[2].reasoning_content","chars":913},{"path":"$.messages[4].content[1].inp'
        'ut.note","chars":695},{"path":"$.messages[2].tool_calls[0].function.arguments","char'
        's":389},{"path":"$.messages[3].content","chars":388},{"path":"$.messages[6].content"'
        ',"chars":388}],"retry_count":3,"messages_chars":4848,"tools_chars":0,"system_chars":'
        '73,"top_level_chars":43,"tool_schema_too_large":false,"tool_payload_compaction_not_s'
        'maller":false,"tail_compaction_not_smaller":false,"emergency_current_turn_compacted"'
        ':true,"emergency_compaction_not_smaller":false,"aggregate_tool_arguments_compacted":'
        'false,"tool_call_arguments_summarized":false}'
    ),
    3600: (
        '{"projection_adapter":"synthetic_adapter","execution_status_version":1,"status_proje'
        'ction_mode":"native_or_none","estimated_chars":2894,"estimated_tokens":723,"proof_bu'
        'dget":3600,"raw_proof_budget":3600,"effective_proof_budget":3088,"proof_headroom_cha'
        'rs":512,"fits":true,"compact_needed":true,"compaction_tier":4,"compaction_protect_re'
        'cent_assistant":false,"recent_tail_too_large":false,"compaction_not_smaller":false,"'
        'provider_window_mismatch":false,"fallback_reason":null,"top_contributors":[{"path":"'
        '$.messages[2].reasoning_content","chars":913},{"path":"$.messages[4].content[1].inpu'
        't.note","chars":695},{"path":"$.messages[7].content","chars":215},{"path":"$.message'
        's[4].content[0].text","chars":67},{"path":"$.messages[5].content[0].content","chars"'
        ':65}],"retry_count":4,"messages_chars":2839,"tools_chars":0,"system_chars":73,"top_l'
        'evel_chars":43,"tool_schema_too_large":false,"tool_payload_compaction_not_smaller":f'
        'alse,"tail_compaction_not_smaller":false,"emergency_current_turn_compacted":true,"em'
        'ergency_compaction_not_smaller":false,"final_hard_cap_compacted":true,"final_hard_ca'
        'p_not_smaller":false,"aggregate_tool_arguments_compacted":false,"tool_call_arguments'
        '_summarized":false}'
    ),
}
_GOLDEN_RAISE_BUDGET = 400
_GOLDEN_RAISE_PROOF_JSON = (
    '{"projection_adapter":"synthetic_adapter","execution_status_version":1,"status_proje'
    'ction_mode":"native_or_none","estimated_chars":4903,"estimated_tokens":1225,"proof_b'
    'udget":400,"raw_proof_budget":400,"effective_proof_budget":300,"proof_headroom_chars'
    '":100,"fits":false,"compact_needed":true,"compaction_tier":4,"compaction_protect_rec'
    'ent_assistant":false,"recent_tail_too_large":true,"compaction_not_smaller":false,"pr'
    'ovider_window_mismatch":false,"fallback_reason":"provider_request_budget_exhausted",'
    '"top_contributors":[{"path":"$.messages[2].reasoning_content","chars":913},{"path":"'
    '$.messages[4].content[1].input.note","chars":695},{"path":"$.messages[2].tool_calls['
    '0].function.arguments","chars":389},{"path":"$.messages[3].content","chars":388},{"p'
    'ath":"$.messages[6].content","chars":388}],"retry_count":4,"messages_chars":4848,"to'
    'ols_chars":0,"system_chars":73,"top_level_chars":43,"tool_schema_too_large":false,"t'
    'ool_payload_compaction_not_smaller":false,"tail_compaction_not_smaller":false,"emerg'
    'ency_current_turn_compacted":true,"emergency_compaction_not_smaller":false,"final_ha'
    'rd_cap_compacted":true,"final_hard_cap_not_smaller":false}'
)
# --- end baseline goldens ---


def _run_ladder(budget: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return prove_or_compact_provider_payload(
        _golden_payload(),
        projection_adapter="synthetic_adapter",
        proof_budget=budget,
    )


@pytest.mark.parametrize("tier", sorted(_GOLDEN_TIER_BUDGETS))
def test_explicit_rollbacks_reproduce_baseline_payload(tier: int) -> None:
    budget = _GOLDEN_TIER_BUDGETS[tier]
    compacted, proof = _run_ladder(budget)
    assert proof is not None
    assert proof["compaction_tier"] == tier
    assert _canon(compacted) == _GOLDEN_PAYLOAD_JSON[budget]
    assert _canon(_legacy_proof_view(proof)) == _GOLDEN_PROOF_JSON[budget]


def test_explicit_rollbacks_reproduce_baseline_raise_proof() -> None:
    with pytest.raises(ProviderRequestBudgetExceededError) as excinfo:
        _run_ladder(_GOLDEN_RAISE_BUDGET)
    assert _canon(_legacy_proof_view(excinfo.value.proof)) == _GOLDEN_RAISE_PROOF_JSON


@pytest.mark.parametrize("off_value", ["0", "false", "off", "disabled"])
def test_off_values_match_unset(monkeypatch: pytest.MonkeyPatch, off_value: str) -> None:
    for env_name in (
        PROTECT_RECENT_RESULTS_ENV,
        PROTECT_ERROR_RESULTS_ENV,
        PROTECT_UNRESOLVED_RESULTS_ENV,
        SKIP_PROJECTED_ENV,
        STUB_PREVIEW_CHARS_ENV,
        NEVER_WORSE_ENV,
    ):
        monkeypatch.setenv(env_name, off_value)
    budget = _GOLDEN_TIER_BUDGETS[4]
    compacted, proof = _run_ladder(budget)
    assert proof is not None
    assert _canon(compacted) == _GOLDEN_PAYLOAD_JSON[budget]
    assert _canon(_legacy_proof_view(proof)) == _GOLDEN_PROOF_JSON[budget]


def _tier1_entries_payload() -> dict[str, Any]:
    """Four oversized tool-result entries, oldest first."""
    return {
        "model": "synthetic-model",
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "tool", "tool_call_id": "c1", "content": "old entry zero " + "a" * 2000},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c2",
                        "content": "old entry one " + "b" * 2000,
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c3", "content": "recent entry two " + "c" * 2000},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c4",
                        "content": [
                            {"type": "text", "text": "recent entry three " + "d" * 2000}
                        ],
                    }
                ],
            },
        ],
    }


def _entry_contents(payload: dict[str, Any]) -> list[str]:
    contents: list[str] = []
    for message in payload["messages"]:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            contents.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            block_content = block.get("content")
            if isinstance(block_content, str):
                contents.append(block_content)
            elif isinstance(block_content, list):
                contents.append(block_content[0]["text"])
    return contents


def test_protect_recent_results_can_be_rolled_back() -> None:
    compacted = _compact_tool_payload_once(_tier1_entries_payload())
    for content in _entry_contents(compacted):
        assert "[provider_request_compacted:" in content


def test_recent_results_default_protects_latest_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROTECT_RECENT_RESULTS_ENV)
    original = _entry_contents(_tier1_entries_payload())
    compacted = _entry_contents(_compact_tool_payload_once(_tier1_entries_payload()))

    assert "[provider_request_compacted:" in compacted[0]
    assert "[provider_request_compacted:" in compacted[1]
    assert compacted[2:] == original[2:]


def test_error_and_unresolved_results_are_protected_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_RESULTS_ENV, "0")
    monkeypatch.delenv(PROTECT_ERROR_RESULTS_ENV)
    monkeypatch.delenv(PROTECT_UNRESOLVED_RESULTS_ENV)
    error = json.dumps({"execution_status": {"status": "error"}, "body": "e" * 2000})
    unresolved = json.dumps(
        {"execution_status": {"status": "unknown"}, "body": "u" * 2000}
    )
    payload = {
        "messages": [
            {"role": "tool", "tool_call_id": "error", "content": error},
            {"role": "tool", "tool_call_id": "pending", "content": unresolved},
            {"role": "tool", "tool_call_id": "plain", "content": "p" * 2000},
        ]
    }

    contents = _entry_contents(_compact_tool_payload_once(payload))

    assert contents[0] == error
    assert contents[1] == unresolved
    assert "[provider_request_compacted:" in contents[2]


def test_protect_recent_results_exempts_last_n_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_RESULTS_ENV, "2")
    original = _entry_contents(_tier1_entries_payload())
    compacted = _entry_contents(_compact_tool_payload_once(_tier1_entries_payload()))
    assert "[provider_request_compacted:" in compacted[0]
    assert "[provider_request_compacted:" in compacted[1]
    assert compacted[2] == original[2]
    assert compacted[3] == original[3]


def test_protect_recent_results_end_to_end_tier1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECT_RECENT_RESULTS_ENV, "1")
    fresh = "freshest result " + "f" * 1500
    payload = {
        "model": "synthetic-model",
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "tool", "tool_call_id": "c1", "content": "old " + "a" * 8000},
            {"role": "tool", "tool_call_id": "c2", "content": "old " + "b" * 8000},
            {"role": "tool", "tool_call_id": "c3", "content": fresh},
        ],
    }
    compacted, proof = prove_or_compact_provider_payload(
        payload,
        projection_adapter="synthetic_adapter",
        proof_budget=8000,
    )
    assert proof is not None
    assert proof["compaction_tier"] == 1
    assert proof["compaction_protect_recent_results"] == 1
    messages = compacted["messages"]
    assert "[provider_request_compacted:" in messages[1]["content"]
    assert "[provider_request_compacted:" in messages[2]["content"]
    assert messages[3]["content"] == fresh


def test_protect_error_results_exempts_detectable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_json = json.dumps(
        {"execution_status": {"status": "error"}, "output": "boom " * 400}
    )
    payload = {
        "model": "synthetic-model",
        "messages": [
            {"role": "tool", "tool_call_id": "c1", "content": error_json},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c2",
                        "is_error": True,
                        "content": "traceback " + "e" * 2000,
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c3", "content": "plain " + "p" * 2000},
        ],
    }
    default = _entry_contents(_compact_tool_payload_once(payload))
    assert all("[provider_request_compacted:" in content for content in default)
    monkeypatch.setenv(PROTECT_ERROR_RESULTS_ENV, "1")
    protected = _entry_contents(_compact_tool_payload_once(payload))
    assert protected[0] == error_json
    assert protected[1] == "traceback " + "e" * 2000
    assert "[provider_request_compacted:" in protected[2]


def test_skip_projected_exempts_boundary_projected_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projected_a = "[tool_result_projection]\n" + "x" * 2000
    projected_b = "[aggregate_tool_result_compacted]\n" + "y" * 2000
    projected_c = "[duplicate_tool_result_elided]\n" + "z" * 2000
    payload = {
        "model": "synthetic-model",
        "messages": [
            {"role": "tool", "tool_call_id": "c1", "content": projected_a},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c2", "content": projected_b},
                    {
                        "type": "tool_result",
                        "tool_use_id": "c3",
                        "content": [{"type": "text", "text": projected_c}],
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c4", "content": "plain " + "p" * 2000},
        ],
    }
    monkeypatch.setenv(SKIP_PROJECTED_ENV, "1")
    compacted = _compact_tool_payload_once(payload)
    messages = compacted["messages"]
    assert messages[0]["content"] == projected_a
    assert messages[1]["content"][0]["content"] == projected_b
    assert messages[1]["content"][1]["content"][0]["text"] == projected_c
    assert "[provider_request_compacted:" in messages[2]["content"]


def test_skip_projected_can_be_rolled_back() -> None:
    projected = "[tool_result_projection]\n" + "x" * 2000
    payload = {
        "model": "synthetic-model",
        "messages": [{"role": "tool", "tool_call_id": "c1", "content": projected}],
    }
    compacted = _compact_tool_payload_once(payload)
    assert "[provider_request_compacted:" in compacted["messages"][0]["content"]


def test_stub_preview_never_leaks_scrubbed_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(STUB_PREVIEW_CHARS_ENV, "11")
    arguments = json.dumps({"text": "[tool_use_argument_projection]\nsecret body"})
    stub = json.loads(_compact_tool_arguments_for_final_cap(arguments))
    assert stub == {"_invalid_provider_context_arguments": True}


@pytest.mark.parametrize("legacy_value", ["0", "11", "500", "5000", "invalid"])
def test_retired_stub_preview_cannot_change_default_stub_payloads(
    monkeypatch: pytest.MonkeyPatch, legacy_value: str
) -> None:
    value = "plain text arguments that are not json " * 40
    tool_input = {f"key_{index:02d}": "v" * 400 for index in range(20)}
    sites = (
        lambda: _compact_argument_string(value, preview=False),
        lambda: _compact_tool_arguments(value, preview=False),
        lambda: _compact_tool_arguments_for_final_cap(value),
        lambda: _compact_tool_input(tool_input),
    )
    baseline = [site() for site in sites]
    raw = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert baseline[-1]["head"] == raw[:360]
    assert baseline[-1]["tail"] == raw[-120:]
    monkeypatch.setenv(STUB_PREVIEW_CHARS_ENV, legacy_value)
    assert [site() for site in sites] == baseline


def test_never_worse_keeps_tiny_argument_value(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(_compact_argument_string("okay", preview=False)) > len("okay")
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    assert _compact_argument_string("okay", preview=False) == "okay"


def test_never_worse_governs_preview_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(STUB_PREVIEW_CHARS_ENV, "40")
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    value = "short argument value under preview size"
    assert _compact_argument_string(value, preview=False) == value


def test_never_worse_keeps_small_final_cap_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = '{"a":"b"}'
    assert _compact_tool_arguments_for_final_cap(arguments) != arguments
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    assert _compact_tool_arguments_for_final_cap(arguments) == arguments


def test_never_worse_keeps_parsed_arguments_when_rewrites_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = json.dumps({"cmd": "q" * 660, "path": "/p"})
    baseline = json.loads(_compact_tool_arguments(value))
    assert "[provider_request_tool_input_compacted:" in baseline["cmd"]
    assert baseline["path"] == "/p"
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    assert _compact_tool_arguments(value) == value


def test_never_worse_still_stubs_unparseable_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    value = "plain text arguments that are not json " * 40
    stub = json.loads(_compact_tool_arguments(value))
    assert stub["original_chars"] == len(value)


def test_never_worse_budget_sweep_never_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    input_chars = _payload_chars(_golden_payload())
    succeeded = 0
    for budget in range(600, 12001, 400):
        try:
            compacted, proof = _run_ladder(budget)
        except ProviderRequestBudgetExceededError:
            continue
        succeeded += 1
        assert proof is not None
        assert _payload_chars(compacted) <= input_chars
    assert succeeded > 0


def test_never_worse_ladder_terminates_on_impossible_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NEVER_WORSE_ENV, "1")
    with pytest.raises(ProviderRequestBudgetExceededError):
        _run_ladder(350)


def test_protect_recent_results_explicit_off_values_roll_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for off_value in ("0", "false", "off", "disabled", "-3"):
        monkeypatch.setenv(PROTECT_RECENT_RESULTS_ENV, off_value)
        compacted = _compact_tool_payload_once(_tier1_entries_payload())
        for content in _entry_contents(compacted):
            assert "[provider_request_compacted:" in content


def test_protect_error_results_ignores_quoted_error_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Successful output that merely quotes error fragments (grep/read over
    # fixtures) must not be exempted from the gentlest tier.
    monkeypatch.setenv(PROTECT_ERROR_RESULTS_ENV, "1")
    quoting = (
        'src/provider/status.py:12: if payload.get("execution_status"):\n'
        'tests/fixtures/errors.json:3: "status": "error",\n'
        + "plain successful output line\n" * 80
    )
    quoting_json = json.dumps(
        {"name": "sample fixture", "is_error": True, "body": "z" * 1500}
    )
    payload = {
        "model": "synthetic-model",
        "messages": [
            {"role": "tool", "tool_call_id": "c1", "content": quoting},
            {"role": "tool", "tool_call_id": "c2", "content": "plain " + "p" * 2000},
        ],
    }
    compacted = _compact_tool_payload_once(payload)
    for content in _entry_contents(compacted):
        assert "[provider_request_compacted:" in content
    parsed_error_payload = {
        "model": "synthetic-model",
        "messages": [{"role": "tool", "tool_call_id": "c3", "content": quoting_json}],
    }
    protected = _compact_tool_payload_once(parsed_error_payload)
    assert protected["messages"][0]["content"] == quoting_json


def test_skip_projected_compacts_unprojected_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SKIP_PROJECTED_ENV, "1")
    projected = "[tool_result_projection]\ncompact summary"
    sibling = "plain build log line\n" * 200
    payload = {
        "model": "synthetic-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c1",
                        "content": [
                            {"type": "text", "text": sibling},
                            {"type": "text", "text": projected},
                        ],
                    }
                ],
            }
        ],
    }
    compacted = _compact_tool_payload_once(payload)
    items = compacted["messages"][0]["content"][0]["content"]
    assert "[provider_request_compacted:" in items[0]["text"]
    assert items[1]["text"] == projected
