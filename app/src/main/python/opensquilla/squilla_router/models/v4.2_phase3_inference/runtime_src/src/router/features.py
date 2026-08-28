"""Feature extraction for the squilla-router.

Three channels:
  1. Hand-crafted features (~41 dims) — always available
  2. TF-IDF + TruncatedSVD (100 dims) — requires fit()
  3. BGE embedding + PCA (64 dims) — requires fit(), optional
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

# ---------------------------------------------------------------------------
# Channel 1: Hand-crafted features
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_JSON_RE = re.compile(r"\{[\s\S]*?[\"'][\w]+[\"']\s*:")
_YAML_RE = re.compile(r"^[\w_]+:\s+\S", re.MULTILINE)
_CSV_RE = re.compile(r"^[^,\n]+,[^,\n]+,[^,\n]+", re.MULTILINE)
_TABLE_RE = re.compile(r"\|.*\|.*\|")
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])([a-zA-Z_][\w.-]*/[\w./-]+\.[\w]+)", re.MULTILINE,
)
_URL_RE = re.compile(r"https?://\S+")
_LOG_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}"
    r"|"
    r"(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
_SHELL_RE = re.compile(r"^\$\s+\w|^>\s+\w|```(?:bash|sh|shell)", re.MULTILINE)
_TRACEBACK_RE = re.compile(r"Traceback \(most recent|stderr:|\.py\", line \d+")
_BULLET_RE = re.compile(r"^[\s]*[-*]\s", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^[\s]*\d+[.)]\s", re.MULTILINE)

_DEBUG_KW = ["error", "bug", "exception", "traceback", "failed", "root cause",
             "报错", "根因", "修复", "stack trace", "debug"]
_RESEARCH_KW = ["调研", "research", "对比", "compare", "survey", "分析报告",
                "competitive analysis", "综述"]
_ARCH_KW = ["architecture", "架构", "重构", "refactor", "monorepo", "codebase",
            "module", "dependency"]
_COMPARE_KW = ["对比", "compare", "audit", "审计", "review", "评估"]
_PLANNING_KW = ["plan", "规划", "roadmap", "设计方案", "workflow", "pipeline",
                "步骤", "step by step"]
_STRICT_FMT_KW = ["JSON", "YAML", "CSV", "schema", "只返回", "不要解释",
                  "按格式", "only return", "no explanation"]
_HIGH_RISK_KW = ["deploy", "rollback", "migration", "delete", "overwrite",
                 "production", "生产", "部署", "删除", "客户", "法务", "财务"]
_PRODUCTION_KW = ["production", "生产", "prod", "线上", "正式环境"]
_CUSTOMER_KW = ["customer", "客户", "用户邮件", "client"]
_DELETE_KW = ["delete", "remove", "drop", "truncate", "删除", "清空", "覆盖",
              "overwrite"]
_FORMAL_KW = ["formal", "正式", "official", "公文", "合同", "法律"]
_CONSTRAINT_KW = ["必须", "不能", "不要", "只能", "must", "shall",
                  "required", "forbidden", "不允许", "至少", "最多"]

# R1-specific keyword lists
_TEACHING_KW = ["how does", "explain", "what is", "why does", "how to",
                "教我", "解释", "为什么", "怎么", "是什么", "how can",
                "tell me about", "walk me through", "介绍", "说明"]
_IMPLEMENT_KW = ["implement", "write function", "write a", "create a",
                 "写个", "实现", "用法", "帮我写", "生成代码", "add a",
                 "build a", "make a", "写一个", "编写"]


def _char_type_ratios(text: str) -> tuple[float, float, float]:
    if not text:
        return 0.0, 0.0, 0.0
    n = len(text)
    zh = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    en = sum(1 for c in text if c.isascii() and c.isalpha())
    code = sum(1 for c in text if c in "{}[]();=<>|&!@#$%^*~`\\")
    return zh / n, en / n, code / n


def _keyword_count(text: str, keywords: list[str]) -> int:
    text_lower = text.lower()
    return sum(1 for kw in keywords if kw.lower() in text_lower)


HANDCRAFTED_DIMS = 51

# ---------------------------------------------------------------------------
# Channel 4: Context features
# ---------------------------------------------------------------------------

CONTEXT_DIMS = 10

# ---------------------------------------------------------------------------
# Channel 5: History features
# ---------------------------------------------------------------------------

HIST_DIMS = 16
_HIST_ONEHOT_SLOTS = 8  # must match len(Trajectory); checked at first call
_ROUTE_TO_IDX = {"R0": 0, "R1": 1, "R2": 2, "R3": 3}


def extract_hist_features(
    history: list | None = None,
    trajectory: object | None = None,
) -> np.ndarray:
    """Extract 16-dimensional history feature vector.

    Layout:
      [0] prev_route_idx   [1] prev_difficulty  [2] prev_margin
      [3] max_route_idx    [4] turn_index       [5] history_len
      [6] dominant_route   [7] switches
      [8..15] trajectory one-hot (8 Trajectory enum values)
    """
    from src.router.trajectory import Trajectory, classify
    assert len(Trajectory) <= _HIST_ONEHOT_SLOTS, \
        f"Trajectory enum has {len(Trajectory)} members but only {_HIST_ONEHOT_SLOTS} one-hot slots"

    vec = np.zeros(HIST_DIMS, dtype=np.float32)

    if not history:
        history = []

    if trajectory is None:
        trajectory = classify(history)

    if history:
        last = history[-1]
        vec[0] = _ROUTE_TO_IDX.get(last.route_class, -1)
        vec[1] = last.difficulty
        vec[2] = last.margin
        vec[3] = max(_ROUTE_TO_IDX.get(h.route_class, -1) for h in history)
        vec[4] = len(history) + 1
        vec[5] = len(history)
        ridx = [_ROUTE_TO_IDX.get(h.route_class, 0) for h in history]
        counts = Counter(ridx)
        max_count = max(counts.values())
        # Tie-break: highest route index wins (prefer over-routing)
        vec[6] = max(route_idx for route_idx, c in counts.items() if c == max_count)
        vec[7] = sum(1 for a, b in zip(ridx, ridx[1:]) if a != b)
    else:
        vec[0] = -1
        vec[3] = -1
        vec[4] = 1
        vec[6] = -1
        vec[7] = 0

    traj_names = [t.value for t in Trajectory]
    tval = trajectory.value if isinstance(trajectory, Trajectory) else str(trajectory)
    try:
        vec[8 + traj_names.index(tval)] = 1.0
    except ValueError:
        vec[8 + traj_names.index(Trajectory.UNCLEAR.value)] = 1.0

    return vec


@dataclass
class ContextMetadata:
    """Session and tool context for context-aware routing."""
    turn_index: int = 0
    context_tokens_est: int = 0
    n_tools: int = 0
    tool_result_length: int = 0
    has_code_block: bool = False
    has_file_reference: bool = False
    has_url: bool = False
    has_tool_results: bool = False

    @classmethod
    def from_sample(cls, sample: dict) -> ContextMetadata:
        """Parse from a training data JSONL sample dict."""
        sc = sample.get("session_context", {})
        tc = sample.get("tool_context", {})
        return cls(
            turn_index=sc.get("turn_index", 0),
            context_tokens_est=sc.get("context_tokens_est", 0),
            n_tools=len(tc.get("available_tools", [])),
            tool_result_length=tc.get("tool_result_length", 0),
            has_code_block=bool(sc.get("has_code_block", False)),
            has_file_reference=bool(sc.get("has_file_reference", False)),
            has_url=bool(sc.get("has_url", False)),
            has_tool_results=bool(tc.get("has_tool_results", False)),
        )


def extract_context_features(ctx: ContextMetadata | None) -> np.ndarray:
    """Extract 10-dimensional context feature vector.

    Returns all zeros when ctx is None (backward compat).
    """
    feats = np.zeros(CONTEXT_DIMS, dtype=np.float32)
    if ctx is None:
        return feats

    # Normalized numerics (clamped to handle dirty data)
    feats[0] = min(max(ctx.turn_index, 0), 20) / 20.0
    feats[1] = min(max(ctx.context_tokens_est, 0), 20000) / 20000.0
    feats[2] = min(max(ctx.n_tools, 0), 5) / 5.0
    feats[3] = min(max(ctx.tool_result_length, 0), 5000) / 5000.0

    # Boolean flags
    feats[4] = float(ctx.has_code_block)
    feats[5] = float(ctx.has_file_reference)
    feats[6] = float(ctx.has_url)
    feats[7] = float(ctx.has_tool_results)

    # Derived signals
    feats[8] = float(ctx.turn_index >= 4)   # is_deep_conversation
    feats[9] = float(ctx.context_tokens_est > 2000)  # is_heavy_context

    return feats


def extract_handcrafted(text: str) -> np.ndarray:
    """Extract 51-dimensional hand-crafted feature vector from text."""
    feats = np.zeros(HANDCRAFTED_DIMS, dtype=np.float32)

    # Basic (0-3)
    feats[0] = len(text)
    words = text.split()
    feats[1] = len(words)
    lines = text.split("\n")
    feats[2] = len(lines)
    feats[3] = feats[0] / max(feats[2], 1)

    # Language (4-7)
    zh, en, code = _char_type_ratios(text)
    feats[4] = zh
    feats[5] = en
    feats[6] = code
    feats[7] = 1.0 if (zh > 0.1 and en > 0.1) else 0.0

    # Structure (8-14)
    code_blocks = _CODE_BLOCK_RE.findall(text)
    feats[8] = 1.0 if code_blocks else 0.0
    feats[9] = len(code_blocks)
    feats[10] = sum(len(b) for b in code_blocks)
    feats[11] = 1.0 if _JSON_RE.search(text) else 0.0
    feats[12] = 1.0 if _YAML_RE.search(text) else 0.0
    feats[13] = 1.0 if _CSV_RE.search(text) else 0.0
    feats[14] = 1.0 if _TABLE_RE.search(text) else 0.0

    # Punctuation (15-18)
    feats[15] = text.count("?") + text.count("\uff1f")
    feats[16] = text.count("!") + text.count("\uff01")
    feats[17] = len(_BULLET_RE.findall(text))
    feats[18] = len(_NUMBERED_RE.findall(text))

    # 19-21 reserved

    # Keyword signals (22-27)
    feats[22] = _keyword_count(text, _DEBUG_KW)
    feats[23] = _keyword_count(text, _RESEARCH_KW)
    feats[24] = _keyword_count(text, _ARCH_KW)
    feats[25] = _keyword_count(text, _COMPARE_KW)
    feats[26] = _keyword_count(text, _PLANNING_KW)
    feats[27] = _keyword_count(text, _STRICT_FMT_KW)

    # Risk (28-32)
    feats[28] = _keyword_count(text, _HIGH_RISK_KW)
    feats[29] = _keyword_count(text, _PRODUCTION_KW)
    feats[30] = _keyword_count(text, _CUSTOMER_KW)
    feats[31] = _keyword_count(text, _DELETE_KW)
    feats[32] = _keyword_count(text, _FORMAL_KW)

    # File/tool (33-37)
    feats[33] = 1.0 if _FILE_PATH_RE.search(text) else 0.0
    feats[34] = 1.0 if _URL_RE.search(text) else 0.0
    feats[35] = 1.0 if _LOG_RE.search(text) else 0.0
    feats[36] = 1.0 if _SHELL_RE.search(text) else 0.0
    feats[37] = 1.0 if _TRACEBACK_RE.search(text) else 0.0

    # Intensity (38-40)
    feats[38] = _keyword_count(text, _CONSTRAINT_KW)
    quoted = re.findall(r"[\"'`](.*?)[\"'`]", text)
    feats[39] = sum(len(q) for q in quoted) / max(len(text), 1)
    words_lower = [w.lower() for w in words]
    feats[40] = len(set(words_lower)) / max(len(words_lower), 1)

    # R1-specific signals (41-50)
    # Teaching intent (41)
    feats[41] = _keyword_count(text, _TEACHING_KW)
    # Implementation intent (42)
    feats[42] = _keyword_count(text, _IMPLEMENT_KW)
    # File reference count bucketing (43-45): 0 / 1-2 / 3+
    file_refs = _FILE_PATH_RE.findall(text)
    n_files = len(set(file_refs))
    feats[43] = 1.0 if n_files == 0 else 0.0
    feats[44] = 1.0 if 1 <= n_files <= 2 else 0.0
    feats[45] = 1.0 if n_files >= 3 else 0.0
    # Code without debug (46): has code block but no debug keywords
    has_debug = _keyword_count(text, _DEBUG_KW) > 0
    feats[46] = 1.0 if (code_blocks and not has_debug) else 0.0
    # Length bucketing (47-49): <200 / 200-1000 / >1000
    text_len = len(text)
    feats[47] = 1.0 if text_len < 200 else 0.0
    feats[48] = 1.0 if 200 <= text_len <= 1000 else 0.0
    feats[49] = 1.0 if text_len > 1000 else 0.0
    # Low keyword density (50): total keyword signals < 2
    total_kw = (feats[22] + feats[23] + feats[24] + feats[25]
                + feats[26] + feats[27] + feats[28])
    feats[50] = 1.0 if total_kw < 2 else 0.0

    return feats


# ---------------------------------------------------------------------------
# Channel 2 + 3: TF-IDF/SVD + BGE/PCA via FeatureExtractor
# ---------------------------------------------------------------------------

_TFIDF_SVD_DIMS = 100
_BGE_PCA_DIMS = 64


class FeatureExtractor:
    """Three-channel feature extractor: hand-crafted + TF-IDF/SVD + BGE/PCA.

    Args:
        use_bge: Whether to include BGE embedding channel.
    """

    def __init__(self, use_bge: bool = True):
        self.use_bge = use_bge
        self.use_context: bool = False
        self.use_hist: bool = False
        self._fitted = False
        self._tfidf: TfidfVectorizer | None = None
        self._svd: TruncatedSVD | None = None
        self._bge_model = None
        self._pca = None

    def fit(self, texts: list[str]) -> FeatureExtractor:
        self._tfidf = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
            max_features=10000,
            sublinear_tf=True,
        )
        tfidf_matrix = self._tfidf.fit_transform(texts)

        n_svd = min(_TFIDF_SVD_DIMS, tfidf_matrix.shape[1])
        self._svd = TruncatedSVD(n_components=n_svd, random_state=42)
        self._svd.fit(tfidf_matrix)

        if self.use_bge:
            self._fit_bge(texts)

        self._fitted = True
        return self

    def _fit_bge(self, texts: list[str]) -> None:
        from sentence_transformers import SentenceTransformer
        from sklearn.decomposition import PCA

        self._bge_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        embeddings = self._bge_model.encode(texts, show_progress_bar=True,
                                            batch_size=256)
        self._pca = PCA(n_components=_BGE_PCA_DIMS, random_state=42)
        self._pca.fit(embeddings)

    def transform(self, text: str, context: ContextMetadata | None = None,
                  history: list | None = None, trajectory: object | None = None) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("FeatureExtractor not fitted. Call fit() first.")
        contexts = [context] if context is not None else None
        histories = [history] if history is not None else None
        trajectories = [trajectory] if trajectory is not None else None
        return self.transform_batch([text], contexts=contexts,
                                    histories=histories, trajectories=trajectories)[0]

    def transform_batch(self, texts: list[str], contexts: list[ContextMetadata] | None = None,
                         histories: list[list] | None = None,
                         trajectories: list | None = None) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("FeatureExtractor not fitted. Call fit() first.")

        n = len(texts)
        if histories is not None and len(histories) != n:
            raise ValueError(f"histories length {len(histories)} != texts length {n}")
        if trajectories is not None and len(trajectories) != n:
            raise ValueError(f"trajectories length {len(trajectories)} != texts length {n}")

        hand = np.array([extract_handcrafted(t) for t in texts])
        tfidf_raw = self._tfidf.transform(texts)
        tfidf_svd = self._svd.transform(tfidf_raw)

        channels = [hand]

        # Context channel: included when contexts passed (backward compat) OR use_context is True
        if contexts is not None or self.use_context:
            if contexts is not None:
                ctx_feats = np.array([extract_context_features(c) for c in contexts])
            else:
                ctx_feats = np.zeros((len(texts), CONTEXT_DIMS), dtype=np.float32)
            channels.append(ctx_feats)

        channels.append(tfidf_svd)

        if self.use_bge and self._bge_model is not None:
            embeddings = self._bge_model.encode(texts, show_progress_bar=False,
                                                 batch_size=256)
            bge_pca = self._pca.transform(embeddings)
            channels.append(bge_pca)

        if self.use_hist:
            hist_feats = np.array([
                extract_hist_features(
                    histories[i] if histories else None,
                    trajectories[i] if trajectories else None,
                )
                for i in range(len(texts))
            ])
            channels.append(hist_feats)

        return np.hstack(channels).astype(np.float32)

    def save(self, path: str) -> None:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        if not self._fitted:
            raise RuntimeError("Cannot save unfitted extractor.")
        joblib.dump(self._tfidf, p / "tfidf.pkl")
        joblib.dump(self._svd, p / "svd.pkl")
        if self._pca is not None:
            joblib.dump(self._pca, p / "pca.pkl")
        joblib.dump({"use_bge": self.use_bge}, p / "config.pkl")

        # Write meta.json schema descriptor
        dim = HANDCRAFTED_DIMS
        ch = ["HC"]
        if self.use_context:
            dim += CONTEXT_DIMS
            ch.append("context")
        dim += _TFIDF_SVD_DIMS
        ch.append("TFIDF")
        if self.use_bge:
            dim += _BGE_PCA_DIMS
            ch.append("BGE")
        if self.use_hist:
            dim += HIST_DIMS
            ch.append("hist")
        meta = {
            "schema_version": 2,
            "use_bge": self.use_bge,
            "use_context": self.use_context,
            "use_hist": self.use_hist,
            "feature_dim": dim,
            "channel_order": ch,
        }
        (p / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str) -> FeatureExtractor:
        p = Path(path)
        config = joblib.load(p / "config.pkl")
        ext = cls(use_bge=config["use_bge"])
        ext._tfidf = joblib.load(p / "tfidf.pkl")
        ext._svd = joblib.load(p / "svd.pkl")
        if ext.use_bge and (p / "pca.pkl").exists():
            ext._pca = joblib.load(p / "pca.pkl")
            from sentence_transformers import SentenceTransformer
            ext._bge_model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        # Restore v2 flags from meta.json if present
        meta_path = p / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            ext.use_context = meta.get("use_context", False)
            ext.use_hist = meta.get("use_hist", False)
        ext._fitted = True
        return ext
