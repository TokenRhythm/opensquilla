"""Squilla Router self-learning loop.

Online side only: capture per-turn routing decisions plus the feature vectors
the model actually computed, so an offline trainer can later realign labels
from negative feedback and incrementally retrain the classifier.

* Raw prompt text is never stored. The training payload is the float16
  ``features_390`` vector the model already produced at inference time.
* All capture is opt-in and best-effort; nothing here may break a turn.
"""

from __future__ import annotations

from opensquilla.squilla_router.self_learning.alignment import (
    AlignedSample,
    align_session,
    route_index,
)
from opensquilla.squilla_router.self_learning.dataset import (
    TrainingDataset,
    build_training_dataset,
    export_training_dataset,
)
from opensquilla.squilla_router.self_learning.schema import (
    SCHEMA_VERSION,
    RouterTrainSample,
    decode_features,
    encode_features,
)
from opensquilla.squilla_router.self_learning.store import (
    agent_data_dir,
    iter_samples,
    router_data_root,
    self_learning_disabled_by_env,
    write_sample,
)

__all__ = [
    "SCHEMA_VERSION",
    "AlignedSample",
    "RouterTrainSample",
    "TrainingDataset",
    "agent_data_dir",
    "align_session",
    "build_training_dataset",
    "decode_features",
    "encode_features",
    "export_training_dataset",
    "iter_samples",
    "route_index",
    "router_data_root",
    "self_learning_disabled_by_env",
    "write_sample",
]
