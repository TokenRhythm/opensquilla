# SquillaRouter V4 Phase 3 Bundle Provenance

This directory contains the local inference bundle used by
`opensquilla.squilla_router.v4_phase3.V4Phase3Strategy`.

## Purpose

The bundle provides the V4 Phase 3 local model router used to classify a turn
into route classes `R0` through `R3`, which are then mapped to configured model
tiers by OpenSquilla gateway configuration. This provenance file does not change
runtime behavior; it records the assets that the existing runtime loads.

## Bundled Asset Groups

- `lgbm_main.bin` and `lgbm_aux.bin`: LightGBM booster files for router heads.
- `mlp/model.onnx` and `mlp/scaler.joblib`: MLP head model and scaler.
- `features/tfidf.pkl`, `features/svd.pkl`, `features/config.pkl`, and
  `features/bge_pca.joblib`: scikit-learn/joblib feature extraction artifacts.
- `bge_onnx/*`: ONNX export and tokenizer files derived from
  `BAAI/bge-small-zh-v1.5`.
- `router.runtime.yaml`, `version.json`, and `inference_manifest.json`: router
  runtime configuration and inference metadata.

## Upstream Model Attribution

The BGE assets are derived from `BAAI/bge-small-zh-v1.5`:

- Hugging Face model: https://huggingface.co/BAAI/bge-small-zh-v1.5
- Upstream project: https://github.com/FlagOpen/FlagEmbedding
- License: MIT

The upstream MIT notice is recorded in the repository root
`THIRD_PARTY_NOTICES.md`.

## Conversion Notes

The repository contains runtime router metadata including feature dimensions,
route classes, and the BGE model name. Runtime behavior is defined by the
checked-in artifacts and configuration listed below.

Current known metadata:

- `version.json` records the router version, feature channels,
  `BAAI/bge-small-zh-v1.5`, and backend `onnx`.
- `inference_manifest.json` records feature dimensions, route classes,
  temperature, class alpha values, BGE backend, and BGE ONNX directory.

## Safety Notes

The current runtime deserializes `.pkl` and `.joblib` artifacts through
`joblib.load`. Treat those files as executable-code-equivalent inputs. Only use
assets shipped with a trusted OpenSquilla release or assets whose size and
sha256 match `artifact_manifest.json`.

## Update Procedure

When any router asset changes:

1. Re-run `python scripts/update_router_artifact_manifest.py`.
2. Review the changed `artifact_manifest.json` entries.
3. Run `uv run pytest tests/test_ci/test_router_artifact_manifest.py -q`.
4. Include any required notice or provenance changes in the same commit.
