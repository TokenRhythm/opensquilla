from __future__ import annotations

from opensquilla.provider import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ErrorEvent,
    ImageFailureKind,
    ImageMarkerState,
    ImageProjectionMode,
    ImageProjectionPolicy,
    Message,
    VisionSupportEvidence,
    VisionSupportSource,
    assert_text_only_messages,
    classify_image_failure,
    classify_image_input_error,
    count_image_blocks,
    image_marker,
    normalize_vision_support,
    project_messages,
    projection_mode_for_support,
)


def _image(payload: str = "c3ludGhldGlj") -> ContentBlockImage:
    return ContentBlockImage(media_type="image/png", data=payload)


def test_native_projection_deep_copies_without_mutating_canonical_messages() -> None:
    canonical = [
        Message(
            role="user",
            content=[
                ContentBlockText(text="look"),
                _image(),
                ContentBlockToolResult(
                    tool_use_id="tool-1",
                    content=[ContentBlockText(text="result"), _image("b"), {"x": [1]}],
                ),
            ],
        )
    ]

    result = project_messages(
        canonical,
        vision_support="supported",
        attachment_ids=("att-1", "att-2"),
    )

    assert result.mode is ImageProjectionMode.NATIVE
    assert result.input_image_count == 2
    assert result.output_image_count == 2
    assert result.marker_count == 0
    assert result.messages is not canonical
    assert result.messages[0] is not canonical[0]
    assert result.messages[0].content is not canonical[0].content
    assert count_image_blocks(canonical) == 2
    assert count_image_blocks(result.messages) == 2

    # Mutating the provider view must not mutate the canonical transcript.
    assert isinstance(result.messages[0].content, list)
    first_text = result.messages[0].content[0]
    assert isinstance(first_text, ContentBlockText)
    first_text.text = "changed in request"
    assert isinstance(canonical[0].content, list)
    assert isinstance(canonical[0].content[0], ContentBlockText)
    assert canonical[0].content[0].text == "look"


def test_marker_projection_recursively_replaces_typed_and_mapping_images() -> None:
    canonical = [
        Message(
            role="user",
            content=[
                ContentBlockText(text="inspect"),
                _image(),
                ContentBlockToolResult(
                    tool_use_id="tool-1",
                    content=[
                        {"type": "image", "source_type": "base64", "data": "a"},
                        ContentBlockToolResult(
                            tool_use_id="nested",
                            content=[_image("b")],
                        ),
                    ],
                ),
            ],
        )
    ]

    result = project_messages(
        canonical,
        mode="marker",
        attachment_ids=("att-1", "att-2", "att-3"),
        marker_states={"att-2": ImageMarkerState.ANALYSIS_FAILED},
    )

    assert result.mode is ImageProjectionMode.MARKER
    assert result.input_image_count == 3
    assert result.output_image_count == 0
    assert result.marker_count == 3
    assert len(result.decisions) == 3
    assert all(decision.marker for decision in result.decisions)
    assert all("att-" in (decision.marker or "") for decision in result.decisions)
    assert any("图片未分析" in (decision.marker or "") for decision in result.decisions)
    assert any("图片分析失败" in (decision.marker or "") for decision in result.decisions)
    assert_text_only_messages(result.messages)

    # The original graph remains image-bearing and unchanged.
    assert count_image_blocks(canonical) == 3
    assert isinstance(canonical[0].content, list)
    assert isinstance(canonical[0].content[1], ContentBlockImage)


def test_tool_use_arguments_are_not_mistaken_for_content_images() -> None:
    message = Message(
        role="assistant",
        content=[
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "fake",
                "input": {"type": "image", "data": "not-a-content-block"},
            }
        ],
    )

    result = project_messages(message_list := [message], mode="marker")

    assert result.input_image_count == 0
    assert result.output_image_count == 0
    assert result.marker_count == 0
    assert result.messages[0].content == message_list[0].content


def test_policy_surrogate_is_bounded_to_the_matching_attachment() -> None:
    messages = [Message(role="user", content=[_image("a"), _image("b")])]
    policy = ImageProjectionPolicy(
        mode=ImageProjectionMode.SURROGATE,
        attachment_ids=("att-a", "att-b"),
        surrogate_by_attachment_id={"att-b": "a small blue square"},
    )

    result = project_messages(messages, policy=policy)

    assert result.output_image_count == 0
    assert result.marker_count == 2
    assert isinstance(result.messages[0].content, list)
    assert isinstance(result.messages[0].content[0], ContentBlockText)
    assert "图片未分析" in result.messages[0].content[0].text
    assert isinstance(result.messages[0].content[1], ContentBlockText)
    assert "图片派生描述" in result.messages[0].content[1].text


def test_capability_normalization_preserves_omitted_vs_explicit_false() -> None:
    assert normalize_vision_support(False) == "unsupported"
    assert normalize_vision_support(None) == "unknown"
    assert normalize_vision_support(False, field_present=False) == "unknown"
    assert projection_mode_for_support("supported") is ImageProjectionMode.NATIVE
    assert projection_mode_for_support("unknown") is ImageProjectionMode.NATIVE
    assert projection_mode_for_support("unsupported") is ImageProjectionMode.MARKER
    assert (
        projection_mode_for_support("supported", force_text_only=True)
        is ImageProjectionMode.MARKER
    )

    evidence = VisionSupportEvidence(
        status=False,
        source=VisionSupportSource.USER_CONFIG,
        deployment="openai:test",
    )
    assert evidence.status == "unsupported"
    assert evidence.rejects_images
    assert evidence.deployment == "openai:test"


def test_marker_state_text_is_truthful_and_id_is_sanitized() -> None:
    not_read = image_marker(ImageMarkerState.NOT_REREAD, attachment_id="att/unsafe\n1")
    failed = image_marker("failed", attachment_id="att-2")
    unavailable = image_marker("missing", attachment_id="att-3")

    assert "历史图片本回合未重新读取" in not_read
    assert "att_unsafe_1" in not_read
    assert "图片分析失败" in failed
    assert "历史图片不可用" in unavailable
    assert not_read.endswith("]")
    assert failed.endswith("]")


def test_image_error_classifier_only_caches_precise_unsupported_evidence() -> None:
    assert (
        classify_image_input_error(
            ErrorEvent(code="image_input_unsupported", message="vision unavailable")
        )
        is ImageFailureKind.UNSUPPORTED_INPUT
    )
    assert (
        classify_image_input_error(
            {"code": "bad_request", "message": "this model does not support image input"}
        )
        is ImageFailureKind.UNSUPPORTED_INPUT
    )
    assert (
        classify_image_input_error(
            {"status_code": 429, "code": "rate_limit", "message": "try later"},
            provider_name="openai",
        )
        is ImageFailureKind.RATE_LIMITED
    )
    assert (
        classify_image_input_error(
            {"status_code": 401, "code": "unauthorized", "message": "bad key"},
            provider_name="openai",
        )
        is ImageFailureKind.AUTHENTICATION
    )
    assert (
        classify_image_input_error(
            {"code": "invalid_image", "message": "image decode failed"}
        )
        is ImageFailureKind.INVALID_MEDIA
    )
    # Generic unsupported feature text does not prove image capability.
    assert (
        classify_image_input_error(
            {"code": "unsupported_feature", "message": "feature unsupported"},
            provider_name="openai",
        )
        is ImageFailureKind.UNKNOWN
    )

    classified = classify_image_failure(
        ErrorEvent(code="image_input_unsupported", message="images unsupported")
    )
    assert classified.is_unsupported
    assert classified.caches_unsupported
    assert classified.retry_without_image


def test_chat_config_remains_importable_with_projection_types() -> None:
    # Sanity check that importing the additive module does not create a provider
    # package cycle or alter the existing ChatConfig contract.
    config = ChatConfig()
    assert config.model_vision_support == "unknown"
