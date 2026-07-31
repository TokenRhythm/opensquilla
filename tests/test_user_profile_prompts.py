"""Prompt building + fail-open parsing of one batch's LLM reply.

The parser is the trust boundary: the LLM output is untrusted, so a malformed
reply must degrade to a failed batch (skipped, never aborting) and individual
bad items must be dropped rather than poison the batch. It also enforces the
denominator anchor — a label for a session that was never sent is discarded.
"""

from __future__ import annotations

import json

from opensquilla.squilla_router.user_profile.prompts import (
    SYSTEM_PROMPT,
    build_batch_prompt,
    parse_batch_response,
)
from opensquilla.squilla_router.user_profile.schema import SessionTranscript

_SENT = ("s1", "s2", "s3")


def _reply(**overrides: object) -> str:
    payload = {
        "session_labels": [
            {"session_id": "s1", "capability": "code_generation", "confidence": 0.9},
            {"session_id": "s2", "capability": "reasoning", "confidence": 0.7},
        ],
        "quality_latency_tradeoff": {
            "value": "quality_first",
            "confidence": 0.8,
            "session_ids": ["s1", "ghost", "s2"],
        },
        "model_mentions": [
            {
                "model_id": "deepseek-v4",
                "direction": "praise",
                "session_ids": ["s1"],
                "confidence": 0.9,
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_build_batch_prompt_carries_every_session_and_the_vocab() -> None:
    batch = [SessionTranscript("s1", "user: hi"), SessionTranscript("s2", "user: yo")]
    payload = json.loads(build_batch_prompt(batch))
    assert [s["session_id"] for s in payload["sessions"]] == ["s1", "s2"]
    assert "code_generation" in payload["allowed_capabilities"]
    assert "quality_first" in payload["allowed_tradeoffs"]


def test_system_prompt_forbids_continuing_the_task_and_quoting() -> None:
    assert "do not continue" in SYSTEM_PROMPT
    assert "never quote" in SYSTEM_PROMPT


def test_a_clean_reply_parses_into_a_batch_analysis() -> None:
    analysis = parse_batch_response(_reply(), _SENT)
    assert analysis.ok
    caps = {label.session_id: label.capability for label in analysis.session_labels}
    assert caps == {"s1": "code_generation", "s2": "reasoning", "s3": "unknown"}
    assert analysis.tradeoff == "quality_first"
    assert analysis.tradeoff_confidence == 0.8
    assert analysis.tradeoff_session_ids == ("s1", "s2")
    assert analysis.model_mentions[0].model_id == "deepseek-v4"


def test_prose_around_the_json_is_tolerated() -> None:
    text = "Sure, here is the analysis:\n" + _reply() + "\nHope that helps!"
    assert parse_batch_response(text, _SENT).ok


def test_non_json_is_a_failed_batch_not_a_raise() -> None:
    analysis = parse_batch_response("I cannot help with that.", _SENT)
    assert analysis.ok is False
    assert analysis.session_ids == _SENT


def test_labels_are_exactly_one_per_sent_session_with_missing_as_unknown() -> None:
    reply = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "reasoning"},
            {"session_id": "not-sent", "capability": "writing"},
        ]
    )
    labels = parse_batch_response(reply, _SENT).session_labels
    assert [(label.session_id, label.capability) for label in labels] == [
        ("s1", "reasoning"),
        ("s2", "unknown"),
        ("s3", "unknown"),
    ]


def test_non_list_labels_still_emit_unknown_for_every_sent_session() -> None:
    labels = parse_batch_response(_reply(session_labels={}), _SENT).session_labels
    assert [(label.session_id, label.capability) for label in labels] == [
        ("s1", "unknown"),
        ("s2", "unknown"),
        ("s3", "unknown"),
    ]


def test_an_unknown_capability_coerces_to_unknown_not_dropped() -> None:
    reply = _reply(session_labels=[{"session_id": "s1", "capability": "telepathy"}])
    labels = parse_batch_response(reply, _SENT).session_labels
    assert labels[0].capability == "unknown"


def test_a_duplicate_session_label_keeps_the_first() -> None:
    reply = _reply(
        session_labels=[
            {"session_id": "s1", "capability": "reasoning"},
            {"session_id": "s1", "capability": "writing"},
        ]
    )
    labels = parse_batch_response(reply, _SENT).session_labels
    assert len(labels) == 3
    assert labels[0].capability == "reasoning"
    assert labels[1].capability == "unknown"
    assert labels[2].capability == "unknown"


def test_a_mention_of_an_unrated_direction_is_dropped() -> None:
    reply = _reply(model_mentions=[{"model_id": "x", "direction": "meh", "session_ids": ["s1"]}])
    assert parse_batch_response(reply, _SENT).model_mentions == ()


def test_mention_session_ids_are_filtered_to_the_sent_set() -> None:
    reply = _reply(
        model_mentions=[
            {
                "model_id": "x",
                "direction": "praise",
                "session_ids": ["s1", "ghost"],
            }
        ]
    )
    mention = parse_batch_response(reply, _SENT).model_mentions[0]
    assert mention.session_ids == ("s1",)


def test_model_mentions_without_valid_session_ids_are_dropped() -> None:
    reply = _reply(
        model_mentions=[
            {
                "model_id": "x",
                "direction": "praise",
                "session_ids": ["ghost"],
            }
        ]
    )
    assert parse_batch_response(reply, _SENT).model_mentions == ()


def test_an_unknown_tradeoff_is_no_batch_vote() -> None:
    reply = _reply(quality_latency_tradeoff={"value": "unknown", "confidence": 0.2})
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.tradeoff == "unknown"  # excluded from the builder's vote


def test_a_real_tradeoff_without_valid_evidence_is_no_batch_vote() -> None:
    reply = _reply(
        quality_latency_tradeoff={
            "value": "quality_first",
            "confidence": 0.9,
            "session_ids": ["ghost"],
        }
    )
    analysis = parse_batch_response(reply, _SENT)
    assert analysis.tradeoff == "unknown"
    assert analysis.tradeoff_session_ids == ()
