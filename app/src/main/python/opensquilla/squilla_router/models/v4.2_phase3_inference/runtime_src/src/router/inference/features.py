from __future__ import annotations

import numpy as np

from src.router.inference.types import FeatureBundle, InferenceRequest
from src.router.v4_features import (
    extract_assistant_handcrafted,
    extract_continuation_features,
    extract_reasoning_features,
    make_history_user_text,
)


def build_feature_bundle(
    request: InferenceRequest,
    v3_extractor,
    bge_extractor,
) -> FeatureBundle:
    prev_assistant_text = (
        request.prev_assistant_text if request.prev_assistant_text else None
    )
    history_text = make_history_user_text(request.history_user_texts)
    pca_192, raw_1536 = bge_extractor.transform_triplet(
        request.current_user_text,
        history_text,
        prev_assistant_text,
    )
    hc = v3_extractor.extract_handcrafted(request.current_user_text)
    tfidf = v3_extractor.extract_tfidf(request.current_user_text)
    ctx = v3_extractor.extract_context(request.context_metadata)
    hist = v3_extractor.extract_hist(request.prev_route_decisions)
    asst = extract_assistant_handcrafted(
        prev_assistant_text,
        request.prev_assistant_usage,
        request.current_user_text,
    )
    cont = extract_continuation_features(
        request.prev_assistant_usage,
        request.current_user_text,
    )
    reasoning = extract_reasoning_features(
        request.prev_assistant_usage,
        request.current_user_text,
    )
    features_390 = np.concatenate(
        [hc, tfidf, ctx, hist, pca_192, asst, cont, reasoning]
    ).astype(np.float32)

    if features_390.shape != (390,):
        raise ValueError(
            f"feature dim mismatch: expected 390, got {features_390.shape}"
        )
    if raw_1536.shape != (1536,):
        raise ValueError(
            f"raw BGE dim mismatch: expected 1536, got {raw_1536.shape}"
        )

    bge_channels = ["user_curr", "user_hist"]
    if prev_assistant_text is not None:
        bge_channels.append("asst")

    return FeatureBundle(
        features_390=features_390,
        raw_bge_1536=raw_1536.astype(np.float32),
        bge_channels_used=bge_channels,
        asst_signal_present=prev_assistant_text is not None,
        history_user_text_compacted=history_text,
    )
