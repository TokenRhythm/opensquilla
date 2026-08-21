from __future__ import annotations

import hashlib
import json

import pytest
from lxml import html as lxml_html  # type: ignore[import-untyped]

from opensquilla.artifact_session.html_anchors import (
    canonical_element_at_path,
    canonical_element_proof_sha256,
    canonical_opening_anchor,
    canonical_selection_proofs,
    contextual_candidate,
    enrich_anchor_context,
    remap_html_anchor,
    target_projection,
)
from opensquilla.artifact_session.models import (
    Anchor,
    AnchorKind,
    AnchorState,
)


def _anchor(source: str, opening: str, *, anchor_id: str = "anchor-old") -> Anchor:
    start = source.index(opening)
    locator = {
        "start_offset": start,
        "start_tag_end_offset": start + len(opening),
        "tag_name": opening.split("<", 1)[1].split(None, 1)[0].split(">", 1)[0],
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "offset_encoding": "unicode-code-point",
    }
    return Anchor(
        anchor_id=anchor_id,
        document_id="doc-1",
        revision_id="rev-1",
        kind=AnchorKind.DOM_SOURCE,
        locator=locator,
        quote=opening,
        context=enrich_anchor_context(source, locator=locator, context={}),
        state=AnchorState.RESOLVED,
        remapped_from_anchor_id=None,
        created_at=1,
    )


def test_remap_follows_unique_identity_after_move_and_style_change() -> None:
    old = '<main><button id="save" style="color:red">Save</button><p>Other</p></main>'
    current = (
        '<main><p>Other</p><section><button id="save" style="color:blue">Save</button>'
        "</section></main>"
    )

    resolution = remap_html_anchor(
        old_source=old,
        current_source=current,
        anchor=_anchor(old, '<button id="save" style="color:red">'),
    )

    assert resolution.status == "ready"
    assert resolution.state is AnchorState.RESOLVED
    assert resolution.quote == '<button id="save" style="color:blue">'
    assert resolution.locator["start_offset"] == current.index(resolution.quote)
    assert resolution.target_kind == "button"
    assert resolution.target_text == "Save"


def test_create_remap_focus_and_contextual_candidate_share_one_locator() -> None:
    source = '<html><body><main><button id="save">Save</button></main></body></html>'
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "button", 1]],
        separators=(",", ":"),
    )
    root = lxml_html.document_fromstring(source)
    selected = canonical_element_at_path(root, element_path)
    proof = canonical_element_proof_sha256(root, selected=selected)
    locator, opening, context = canonical_opening_anchor(
        source,
        element_path=element_path,
        expected_element_proof_sha256=proof,
        expected_tag_name="button",
    )
    anchor = Anchor(
        anchor_id="anchor-shared",
        document_id="doc-1",
        revision_id="rev-1",
        kind=AnchorKind.DOM_SOURCE,
        locator=locator,
        quote=opening,
        context=context,
        state=AnchorState.RESOLVED,
        remapped_from_anchor_id=None,
        created_at=1,
    )

    remapped = remap_html_anchor(
        old_source=source,
        current_source=source,
        anchor=anchor,
    )
    candidate = contextual_candidate(
        source=source,
        candidate_source=opening,
        expected_tag_name="button",
    )

    assert remapped.locator == locator
    assert candidate.locator == locator
    assert remapped.context["element_path"] == element_path
    assert remapped.context["element_proof_sha256"] == proof


def test_canonical_selection_proofs_safely_parse_source_and_path() -> None:
    source = '<html><body><main><button id="save">Save</button></main></body></html>'
    element_path = json.dumps(
        [["", "html", 1], ["", "body", 1], ["", "main", 1], ["", "button", 1]],
        separators=(",", ":"),
    )

    dom_sha256, proof = canonical_selection_proofs(source, element_path=element_path)

    assert len(dom_sha256) == 64
    assert len(proof) == 64


def test_remap_never_chooses_first_ambiguous_candidate() -> None:
    old = "<main><button>Save</button></main>"
    current = "<main><button>Save</button><button>Save</button></main>"

    resolution = remap_html_anchor(
        old_source=old,
        current_source=current,
        anchor=_anchor(old, "<button>"),
    )

    assert resolution.status == "contextual"
    assert resolution.reason == "ambiguous"
    assert resolution.state is AnchorState.ORPHANED
    assert "start_offset" not in resolution.locator
    assert target_projection(
        Anchor(
            anchor_id="new",
            document_id="doc-1",
            revision_id="rev-2",
            kind=resolution.kind,
            locator=resolution.locator,
            quote=resolution.quote,
            context=resolution.context,
            state=resolution.state,
            remapped_from_anchor_id="anchor-old",
            created_at=2,
        )
    )[:2] == ("contextual", "ambiguous")


def test_remap_deleted_target_becomes_contextual_no_match() -> None:
    old = '<main><button id="save">Save</button></main>'
    resolution = remap_html_anchor(
        old_source=old,
        current_source="<main><p>Done</p></main>",
        anchor=_anchor(old, '<button id="save">'),
    )

    assert resolution.status == "contextual"
    assert resolution.reason == "no_match"


def test_semantic_profile_does_not_capture_sensitive_input_values() -> None:
    source = (
        '<form><input type="password" value="secret-value" aria-label="Password">'
        '<input type="hidden" value="hidden-secret">'
        '<input type="submit" value="Sign in"></form>'
    )
    password = _anchor(
        source,
        '<input type="password" value="secret-value" aria-label="Password">',
    )
    submit = _anchor(source, '<input type="submit" value="Sign in">', anchor_id="submit")
    hidden = _anchor(source, '<input type="hidden" value="hidden-secret">', anchor_id="hidden")

    password_profile = password.context["semantic_profile_v1"]
    submit_profile = submit.context["semantic_profile_v1"]
    hidden_profile = hidden.context["semantic_profile_v1"]
    assert "secret-value" not in str(password_profile)
    assert "hidden-secret" not in str(hidden_profile)
    assert password_profile["safe_attributes"]["aria-label"] == "Password"
    assert submit_profile["safe_attributes"]["value"] == "Sign in"
    assert target_projection(submit)[3] == "Sign in"


def test_contextual_candidate_requires_unique_complete_same_tag() -> None:
    source = '<main><button id="first">One</button><button id="second">Two</button></main>'
    candidate = contextual_candidate(
        source=source,
        candidate_source='<button id="second">',
        expected_tag_name="button",
    )
    assert candidate.locator["start_offset"] == source.index('<button id="second">')

    with pytest.raises(ValueError):
        contextual_candidate(
            source="<main><button>One</button><button>One</button></main>",
            candidate_source="<button>",
            expected_tag_name="button",
        )
    with pytest.raises(ValueError):
        contextual_candidate(
            source=source,
            candidate_source='<button id="second">Two',
            expected_tag_name="button",
        )
    with pytest.raises(ValueError):
        contextual_candidate(
            source=source,
            candidate_source='<button id="second">',
            expected_tag_name="a",
        )


@pytest.mark.parametrize("input_type", ["password", "hidden"])
def test_contextual_candidate_rejects_sensitive_input(input_type: str) -> None:
    source = f'<form><input type="{input_type}" value="secret"></form>'
    with pytest.raises(ValueError):
        contextual_candidate(
            source=source,
            candidate_source=f'<input type="{input_type}" value="secret">',
            expected_tag_name="input",
        )
