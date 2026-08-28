"""V4 router feature extraction: helper surface + legacy pre-Phase3 assembly.

Three logical channels added to the legacy v4 baseline:
  * BGE × 3 segments (current_user, history_user, prev_assistant) → PCA(64) each
  * 12-dim assistant handcrafted features (refusal/clarification/usage stats)
  * History-user concatenation helper

Phase 3's 390-dim online assembly lives in `src.router.inference.features`.
This module still contains the legacy 383-dim `V4FeatureExtractor` for
backward compatibility while exposing the low-level helper functions reused
by the new inference package.
"""
from __future__ import annotations

import re

import joblib
import numpy as np
from sklearn.decomposition import PCA

__all__ = [
    "extract_assistant_handcrafted",
    "extract_continuation_features",
    "extract_reasoning_features",
    "make_history_user_text",
    "BGEChannelExtractor",
    "V4FeatureExtractor",
]


# ---------------------------------------------------------------------------
# Channel: assistant handcrafted features (12 dims)
# ---------------------------------------------------------------------------

_RE_CLAR = re.compile(
    r"(?:能否|请\s*提供|需要(?:更多|具体).{0,8}信息"
    r"|could you (?:clarify|provide)|please (?:specify|provide)|clarify which)",
    re.I,
)
_RE_REFUSAL = re.compile(
    r"(?:I cannot|I can't help|对不起.{0,5}无法|抱歉.{0,5}不能"
    r"|作为(?:AI|大语言模型))",
    re.I,
)
_RE_SELF_DOUBT = re.compile(
    r"(?:我不(?:确定|清楚)|可能(?:不太|不一定)"
    r"|not sure|might not be|I'm not entirely)",
    re.I,
)
_RE_CODE_INLINE = re.compile(r"`[^`]{4,}`")
_RE_NUMBERED_LIST = re.compile(r"^\s*\d+[\.、]\s", re.M)
_RE_CONTINUATION = re.compile(
    r"(?:请继续|继续|接着|续写|展开一下|再说|more|continue|go on|carry on|next)",
    re.I,
)
_RE_REASONING = re.compile(
    r"(?:why|compare|trade[ -]?off|analy[sz]e|architecture|reasoning|design"
    r"|解释|原因|对比|分析|架构|设计|权衡)",
    re.I,
)


def _zh_char_ratio(text: str) -> float:
    if not text:
        return 0.0
    zh = sum(1 for c in text if "一" <= c <= "鿿")
    return zh / max(len(text), 1)


def _normalize_log_usage(usage: dict | None, key: str, divisor: float = 10.0) -> float:
    value = (usage or {}).get(key, 0) or 0
    return float(np.log1p(max(value, 0)) / divisor)


def extract_assistant_handcrafted(prev_assistant_text: str | None,
                                   prev_assistant_usage: dict | None,
                                   current_user_text: str) -> np.ndarray:
    """Return a 12-dim float32 vector of assistant signal features.

    Layout:
      0:  has_prev_asst              (0/1)
      1:  has_clarification_question (0/1)
      2:  has_refusal                (0/1)
      3:  self_doubt                 (0/1)
      4:  has_code_block             (0/1)
      5:  has_steps_list             (0/1)
      6:  log_output_tokens          (log1p / 10, soft-bounded)
      7:  log_reasoning_tokens       (log1p / 10)
      8:  log_duration_ms            (log1p / 10)
      9:  ans_user_ratio             (clip [0, 1])
      10: zh_ratio                   ([0, 1])
      11: cached_token_ratio         ([0, 1])
    """
    if prev_assistant_text is None:
        return np.zeros(12, dtype=np.float32)
    t = prev_assistant_text
    u = prev_assistant_usage or {}
    return np.array([
        1.0,
        float(_RE_CLAR.search(t) is not None),
        float(_RE_REFUSAL.search(t) is not None),
        float(_RE_SELF_DOUBT.search(t) is not None),
        float("```" in t or _RE_CODE_INLINE.search(t) is not None),
        float(_RE_NUMBERED_LIST.search(t) is not None),
        np.log1p(u.get("output_tokens", 0) or 0) / 10.0,
        np.log1p(u.get("reasoning_tokens", 0) or 0) / 10.0,
        np.log1p(u.get("duration_ms", 0) or 0) / 10.0,
        min(len(t) / max(len(current_user_text), 1), 5.0) / 5.0,
        _zh_char_ratio(t),
        (u.get("cached_tokens", 0) or 0) / max(u.get("input_tokens", 1) or 1, 1),
    ], dtype=np.float32)


def extract_continuation_features(prev_assistant_usage: dict | None,
                                  current_user_text: str) -> np.ndarray:
    """Return a 2-dim float32 vector for short continuation prompts.

    Layout:
      0: has_continuation_cue     (0/1)
      1: prev_output_tokens_log   (log1p / 10)
    """
    text = (current_user_text or "").strip()
    is_short = len(text) <= 24
    has_cue = bool(text) and is_short and _RE_CONTINUATION.search(text) is not None
    return np.array([
        float(has_cue),
        _normalize_log_usage(prev_assistant_usage, "output_tokens"),
    ], dtype=np.float32)


def extract_reasoning_features(prev_assistant_usage: dict | None,
                               current_user_text: str) -> np.ndarray:
    """Return a 5-dim float32 vector for reasoning-heavy prompt cues.

    Layout:
      0: has_reasoning_cue         (0/1)
      1: question_density          (clipped [0, 1])
      2: prompt_length_log         (log1p / 10)
      3: prev_reasoning_tokens_log (log1p / 10)
      4: prev_duration_ms_log      (log1p / 10)
    """
    text = (current_user_text or "").strip()
    qmarks = text.count("?") + text.count("？")
    return np.array([
        float(_RE_REASONING.search(text) is not None),
        min(qmarks / max(len(text), 1) * 20.0, 1.0),
        float(np.log1p(len(text)) / 10.0),
        _normalize_log_usage(prev_assistant_usage, "reasoning_tokens"),
        _normalize_log_usage(prev_assistant_usage, "duration_ms"),
    ], dtype=np.float32)


# ---------------------------------------------------------------------------
# Helper: history user text concatenation
# ---------------------------------------------------------------------------

_HISTORY_SEP = "\n[SEP]\n"


def make_history_user_text(prior_user_turns: list[str], max_turns: int = 4,
                           max_chars: int = 1500) -> str:
    """Concatenate up to max_turns prior user turns, oldest→newest, [SEP]-separated.

    BGE tokenizer has a 512-token limit. Empirically 1500 chars is a safe
    upper bound (zh ~750 tokens at worst, en ~375). If the result exceeds
    max_chars, truncate from the front (drop oldest turns first).
    """
    if not prior_user_turns:
        return ""
    selected = list(prior_user_turns[-max_turns:])  # oldest→newest of the window
    text = _HISTORY_SEP.join(selected)
    while len(text) > max_chars and len(selected) > 1:
        selected = selected[1:]   # drop the oldest
        text = _HISTORY_SEP.join(selected)
    if len(text) > max_chars:
        # only one turn left and still too long: hard truncate from the front
        text = text[-max_chars:]
    return text


# ---------------------------------------------------------------------------
# Channel: BGE × 3 segments + shared PCA(64)
# ---------------------------------------------------------------------------

class BGEChannelExtractor:
    """Shared BGE encoder + shared PCA(64) for three text segments.

    Each call to transform_one runs the BGE encoder three times on
    [current_user, history_user, prev_assistant] (None → empty string).
    PCA is fitted once on the union of all three text types.

    Output shape: (192,) = concat of 3 × PCA(64).
    """

    def __init__(self, bge_model_name: str = "BAAI/bge-small-zh-v1.5",
                 pca_dim: int = 64, seed: int = 42,
                 backend: str = "sentence_transformers",
                 onnx_model_dir: str | None = None):
        self.bge_model_name = bge_model_name
        self.pca_dim = pca_dim
        self.seed = seed
        self.backend = backend
        self.onnx_model_dir = onnx_model_dir
        if backend == "onnx" and not onnx_model_dir:
            raise ValueError("backend='onnx' requires onnx_model_dir")
        self._bge = None
        self.pca: PCA | None = None
        self.fitted = False

    def _ensure_bge(self):
        if self._bge is None:
            if self.backend == "onnx":
                from src.router.bge_onnx import OnnxBGE

                self._bge = OnnxBGE(self.onnx_model_dir)
            else:
                from sentence_transformers import SentenceTransformer

                self._bge = SentenceTransformer(self.bge_model_name)
        return self._bge

    def _encode_triplet(
        self,
        current_user: str | None,
        history_user: str | None,
        prev_assistant: str | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        if not self.fitted:
            raise RuntimeError("Call fit() before transform_one().")
        bge = self._ensure_bge()
        texts = [current_user or "", history_user or "", prev_assistant or ""]
        raw = bge.encode(texts, batch_size=3, show_progress_bar=False,
                         convert_to_numpy=True).astype(np.float32)   # (3, 512)
        reduced = self.pca.transform(raw)                            # (3, k)
        if reduced.shape[1] < self.pca_dim:
            pad = np.zeros((reduced.shape[0], self.pca_dim - reduced.shape[1]),
                           dtype=reduced.dtype)
            reduced = np.concatenate([reduced, pad], axis=1)
        return reduced.astype(np.float32), raw

    def fit(self, sessions: list[dict]) -> None:
        """Fit the shared PCA on all three text channels' BGE embeddings.

        sessions: list of dicts with `turns: [{text, prev_assistant_text, context.turn_index}, ...]`

        If the union of all texts has fewer samples than `pca_dim`, the PCA is
        fitted with a clamped n_components and the transform output is padded
        with zeros to preserve the (3 * pca_dim,) output contract.
        """
        bge = self._ensure_bge()
        all_texts: list[str] = []
        for s in sessions:
            user_history: list[str] = []
            for turn in s["turns"]:
                cur = turn.get("text", "") or ""
                hist = make_history_user_text(user_history)
                prev_asst = turn.get("prev_assistant_text") or ""
                all_texts.extend([cur, hist, prev_asst])
                user_history.append(cur)
        # BGE handles empty strings fine (returns near-constant vector)
        embs = bge.encode(all_texts, batch_size=64, show_progress_bar=False,
                          convert_to_numpy=True)
        # PCA needs n_components <= min(n_samples, n_features). Clamp for tiny
        # fit corpora; transform_one pads back to self.pca_dim with zeros.
        n_components = min(self.pca_dim, embs.shape[0], embs.shape[1])
        self.pca = PCA(n_components=n_components, random_state=self.seed).fit(embs)
        self.fitted = True

    def transform_one(self, current_user: str | None, history_user: str | None,
                      prev_assistant: str | None) -> np.ndarray:
        reduced, _ = self._encode_triplet(
            current_user,
            history_user,
            prev_assistant,
        )
        return np.concatenate(reduced, axis=0).astype(np.float32)

    def transform_triplet(self, current_user: str | None, history_user: str | None,
                          prev_assistant: str | None) -> tuple[np.ndarray, np.ndarray]:
        reduced, raw = self._encode_triplet(
            current_user,
            history_user,
            prev_assistant,
        )
        return (
            np.concatenate(reduced, axis=0).astype(np.float32),
            raw.reshape(-1).astype(np.float32),
        )

    def transform_batch(self, triplets: list[tuple[str | None, str | None, str | None]],
                        bge_batch_size: int = 128,
                        show_progress: bool = False) -> np.ndarray:
        """Batched 3-segment BGE encoding for training-time featurization.

        Flattens N triplets to 3N texts, encodes in large batches (much
        faster than calling transform_one N times), applies PCA, and
        reshapes back to (N, 192).
        """
        if not self.fitted:
            raise RuntimeError("Call fit() before transform_batch().")
        bge = self._ensure_bge()
        flat_texts: list[str] = []
        for cur, hist, prev in triplets:
            flat_texts.extend([cur or "", hist or "", prev or ""])
        embs = bge.encode(flat_texts, batch_size=bge_batch_size,
                          show_progress_bar=show_progress,
                          convert_to_numpy=True)            # (3N, 512)
        reduced = self.pca.transform(embs)                  # (3N, k)
        if reduced.shape[1] < self.pca_dim:
            pad = np.zeros((reduced.shape[0], self.pca_dim - reduced.shape[1]),
                           dtype=reduced.dtype)
            reduced = np.concatenate([reduced, pad], axis=1)  # (3N, pca_dim)
        # Reshape (3N, pca_dim) → (N, 3, pca_dim) → (N, 3*pca_dim=192)
        n_triplets = len(triplets)
        return reduced.reshape(n_triplets, 3 * self.pca_dim).astype(np.float32)

    def save(self, path) -> None:
        joblib.dump({
            "bge_model_name": self.bge_model_name,
            "pca_dim": self.pca_dim,
            "seed": self.seed,
            "backend": self.backend,
            "onnx_model_dir": self.onnx_model_dir,
            "pca": self.pca,
            "fitted": self.fitted,
        }, path)

    @classmethod
    def load(cls, path) -> BGEChannelExtractor:
        state = joblib.load(path)
        ex = cls(
            state["bge_model_name"],
            state["pca_dim"],
            state["seed"],
            state.get("backend", "sentence_transformers"),
            state.get("onnx_model_dir"),
        )
        ex.pca = state["pca"]
        ex.fitted = state["fitted"]
        return ex


# ---------------------------------------------------------------------------
# Top-level: V4FeatureExtractor (orchestrator)
# ---------------------------------------------------------------------------

# Plan-defined v4 TFIDF dim is 102; v3 baseline uses 100. We zero-pad
# (or truncate) v3's TFIDF SVD output to this fixed width so the v4 layout
# is stable across v3 version drift.
_V4_TFIDF_DIMS = 102


class V4FeatureExtractor:
    """Legacy pre-Phase3 v4 feature assembly.

    Wraps the v3 FeatureExtractor (HC + TFIDF + ctx + hist) and adds
    the v4 BGE 3-channel + asst HC channels.

    Output: 383-dim float32 vector.

    Phase 3's online inference path is 390-dim and is assembled in
    `src.router.inference.features.build_feature_bundle`. This class remains
    only to preserve the older v4 path until the adapter migration is complete.

    Layout:
       [0:51]    HC                                    (51 dims)
       [51:153]  TFIDF SVD (zero-padded to 102)        (102 dims)
       [153:163] context                               (10 dims)
       [163:179] history stats (last route, traj, ...) (16 dims)
       [179:243] BGE current_user                      (64 dims)
       [243:307] BGE history_user                      (64 dims)
       [307:371] BGE prev_assistant                    (64 dims)
       [371:383] assistant HC                          (12 dims)
    """

    FEATURE_DIM = 383
    HC_SLICE        = slice(0, 51)
    TFIDF_SLICE     = slice(51, 153)
    CTX_SLICE       = slice(153, 163)
    HIST_SLICE      = slice(163, 179)
    BGE_CURR_SLICE  = slice(179, 243)
    BGE_HIST_SLICE  = slice(243, 307)
    BGE_ASST_SLICE  = slice(307, 371)
    ASST_HC_SLICE   = slice(371, 383)

    def __init__(self, v3_extractor, bge_extractor: BGEChannelExtractor):
        self.v3 = v3_extractor
        self.bge = bge_extractor

    def build_feature_vector(self, *,
                              current_user_text: str,
                              prior_user_turns: list[str],
                              prev_assistant_text: str | None,
                              prev_assistant_usage: dict | None,
                              context_metadata: dict,
                              prev_route_decisions: list) -> np.ndarray:
        # Lazy imports to avoid circular ones
        from src.router.features import (
            ContextMetadata,
            extract_context_features,
            extract_handcrafted,
            extract_hist_features,
        )

        # v3 channels — built individually to match v4 layout (HC + TFIDF + ctx + hist).
        # v3.transform() returns a different layout (HC + ctx + TFIDF + ...), so we
        # don't use it; we recompute each block ourselves.
        hc = extract_handcrafted(current_user_text)                      # (51,)

        # TFIDF: access v3's private _tfidf + _svd directly. Pad/truncate to
        # _V4_TFIDF_DIMS (102) so the v4 layout is stable even if v3's SVD width
        # drifts (currently 100; clamped further when fit corpus is tiny).
        tfidf_raw = self.v3._tfidf.transform([current_user_text])
        tfidf_svd = self.v3._svd.transform(tfidf_raw)[0]                 # (k,) k <= 100
        tfidf = np.zeros(_V4_TFIDF_DIMS, dtype=np.float32)
        k = min(tfidf_svd.shape[0], _V4_TFIDF_DIMS)
        tfidf[:k] = tfidf_svd[:k]

        # Context: build a ContextMetadata if a dict was passed
        if isinstance(context_metadata, dict):
            if context_metadata:
                # Filter keys to defined dataclass fields to be tolerant of extras
                allowed = ContextMetadata.__dataclass_fields__.keys()
                ctx_obj = ContextMetadata(
                    **{k: v for k, v in context_metadata.items() if k in allowed}
                )
            else:
                ctx_obj = None
        else:
            ctx_obj = context_metadata
        ctx = extract_context_features(ctx_obj)                          # (10,)

        # History stats: pass prev_route_decisions as the history list
        hist = extract_hist_features(prev_route_decisions or None)       # (16,)

        # v4 BGE 3-channel: build history_user_text from prior_user_turns
        history_user_text = make_history_user_text(prior_user_turns or [])
        bge = self.bge.transform_one(current_user_text, history_user_text,
                                     prev_assistant_text)                # (192,)

        # v4 assistant HC channel
        asst_hc = extract_assistant_handcrafted(prev_assistant_text,
                                                prev_assistant_usage,
                                                current_user_text)       # (12,)

        out = np.concatenate([hc, tfidf, ctx, hist, bge, asst_hc]).astype(np.float32)
        if out.shape[0] != self.FEATURE_DIM:
            raise ValueError(
                f"feature dim mismatch: expected {self.FEATURE_DIM}, got {out.shape[0]}"
            )
        return out
