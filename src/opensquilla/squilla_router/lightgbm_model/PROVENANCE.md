The single-model policy now uses the selected BERT-60M Encoder + Embedding
INT8 intent classifier, with the original LightGBM tier classifier unchanged.
The `lightgbm_model` configuration name is retained for compatibility; legacy,
direct and Ensemble routing do not use this model pair.

The intent weight is exactly 59,844,613 bytes, SHA-256
`4bb16d564a672f892502a909522bb0ad5ad1ecee87bba72094a9f0203258fd35`, from
`20261103-bert60-intent-compression-v2/model.encoder-int8.embedding-int8.onnx`.
It uses the original BERT numeric88 StandardScaler and temperature
`1.349285881307928`, and reuses the byte-identical existing BGE tokenizer.
Text budgets are 128/80/48 including CLS/SEP, padded to 128 per segment;
empty history/answer use `[EMPTY]`. Numeric features are scaled then clipped to
[-5, 5]. No transformers dependency or extra tokenizer is required.
The ONNX file and 2,695-byte scaler are ordinary binary Git resources.

`assets/intent/golden_vectors.json` records synthetic source-runtime input
hashes and exact quantized-candidate outputs. These are not FP32 equivalence
claims: the experiment's original FP32 logit tolerance was not met.
The six candidate cases also include separately measured AVX2, Apple M5 and
Apple M1 (Virtual) ARM64 probability vectors under ORT 1.24.4, CPU-only, one
intra/inter-op thread and `session.x64quantprecision=1`. M5 and AVX2 repeated
runs reproduced their vectors exactly. The complete M1 matrix was captured on
macOS 26.6.2 with numpy 2.5.3 in
[CI job 125986706486](https://github.com/TokenRhythm/opensquilla/actions/runs/41655999956/job/125986706486).
It differs from both M5 and the original source, despite identical input hashes.
Tests require the entire six-case matrix to match one measured kernel profile
within 1e-6; they do not equate ARM64 with a single CPU implementation or select
a different profile per case. Prepared-input hashes, intent labels and final
policy decisions remain strict. Maximum native-reference differences were
0.0061261 on AVX2, 0.080843 on M5 and 0.045091 on M1. These finite fixtures do
not guarantee identical probabilities or accuracy on arbitrary inputs or
unmeasured CPU implementations.
`assets/source_parity_cases.json` and `runtime_compatibility.json` retain the
historical LightGBM pair measurements below; their intent distributions are
historical only. The tier fixtures, weights, feature pipeline and calibration
are unchanged. The local manifest now identifies and verifies the mixed pair.

The original LightGBM pair selected by the 2026-10-20 experiment was:

- Model set: `router-lightgbm-99d6ff75-d444135-a1`.
- Original manifest SHA-256: `29fb79337458c4f35d3e0d1148fbdb1175c20069a5fa8ee6441633b0e2eee357`.
- Training code identity: `git:d4441356584423be07361495e55b488c95c5db43`.
- Runtime snapshot: `router-training-a34e5ae` beside the experiment's frozen OpenSquilla source.

`assets/provenance.json` records the upstream source paths and original file
hashes; these identify the source snapshot, not checksums of the local extracts.
`_vendor/` retains only the inference subset. Training, artifact export/loading,
BERT, registry CLI, unused batch transforms, training-only transformer
constructors, and unused encoder inspection/context-manager methods were removed.
The local loader owns the encoder and featurizer directly, without the redundant
feature-runtime wrapper. The unused SQLite embedding cache and its identity
helpers were removed. The memory cache now retains at most 2,048 normalized
embedding rows in least-recently-used order (4 MiB of vector data for this model,
plus keys and container overhead). Cached rows own their storage, and close
releases them. Evicted inputs are recomputed using the same arithmetic.
Tokenization, batching, ordering, and normalization are unchanged. ONNX session
configuration additionally enables `session.x64quantprecision=1` to avoid x64
U8S8 saturation on CPUs without VNNI; the encoder file is unchanged. On ARM64,
`MatMulIntegerToFloatFusion` and `GeluFusionL2` are disabled to preserve the
unfused operations' numerical behavior; all other optimizations remain enabled.
Other retained function/method definitions preserve the original arithmetic;
imports, module/class descriptions, and type-only annotations/casts were adjusted.
`features/text.py` and `input_contract.py` retain the original extracted definitions.
The original training package declares `LicenseRef-Proprietary`; that declaration
and its original package metadata are retained in `assets/upstream-pyproject.toml`.
The frozen snapshot contains no separate license file. These source files retain
their upstream licensing and are not relicensed by the surrounding project.

The local adapter removes the registry/database dependency. It verifies the
local manifest and the selected inference closure before native model loading
or pickle deserialization. The retained tier model text is distributed as gzip;
verification hashes the decompressed original bytes. The encoder, tokenizer,
three tier transformers, tier calibration and feature metadata are unchanged. The packaged model reuses the byte-identical ONNX model and tokenizer
in `../models/v4.2_phase3_inference/bge_onnx/`; the manifest still verifies both before loading. External `lightgbm_model_dir` bundles remain
self-contained, with `preprocess/feature/artifacts/bge/model.onnx` and
`preprocess/feature/artifacts/bge/tokenizer.json`. Both layouts avoid the long
content-addressed Windows paths; the tier source manifests and hashes are unchanged.
Training run manifests and training metrics remain referenced in the original
manifest but are omitted from the inference distribution.

The 380 float32 features are current-request BGE/PCA64, current-request
character TF-IDF/SVD100, rules48, history BGE/PCA64, route-history16,
previous-answer BGE/PCA64, previous-signals19, and context5. BGE uses CLS pooling,
float32 L2 normalization, a fixed 512-token input, and frozen segment truncation.
An empty history or previous answer produces a zero semantic block. There is no
extra scaling step for the tier head. Each head uses its original calibration.

The runtime is CPU-only and uses local files exclusively. Installation needs
numpy, LightGBM, ONNX Runtime, tokenizers, scikit-learn, joblib and pydantic.
Validation used the deployed reference versions: numpy 2.4.4, LightGBM 4.6.0,
ONNX Runtime 1.24.4, tokenizers 0.22.2, scikit-learn 1.8.0, joblib 1.5.3.
The feature-training environment is recorded separately in `feature_recipe.json`.

`assets/source_parity_cases.json` contains only synthetic test inputs, captured
from the frozen reference runtime. It records raw features, their hashes, both
calibrated output distributions, and the source runner identity. The original
classifier golden vectors are also retained and exercised by the tests.

The recommended installation preserves the frozen scikit-learn persistence
version (1.8.0) and ONNX Runtime 1.24.4. Compatibility was also checked using
numpy 2.5.3, LightGBM 4.7.0, joblib 1.6.0, and tokenizers 0.23.2: all six
source feature hashes and full-pipeline probability distributions match
exactly on the native AVX-512/VNNI host, and all sixteen original classifier
golden cases agree within 2.23e-16.
`assets/runtime_compatibility.json` records the measured results. ONNX Runtime
1.30.0 changed the code-debug fixture's features by up to 0.006055 and tier
probabilities by up to 0.004872, so it is not interchangeable for this bundle.

The CI cold-start tier probability discrepancy (maximum 0.019415810857340232)
was reproduced exactly by masking AVX-512/VNNI/AMX capabilities on that host.
The encoder contains dynamically quantized U8S8 matrix multiplications.
[ONNX Runtime documents](https://github.com/microsoft/onnxruntime/blob/v1.24.4/include/onnxruntime/core/session/onnxruntime_session_options_config_keys.h#L157-L161)
that the non-VNNI kernels can saturate; its precision option converts the signed
weights to mathematically equivalent unsigned values at session initialization.
With this option, the cold-start features and probabilities match exactly on
AVX2, and all six reference cases match exactly with AVX-512 but VNNI disabled.

AVX2 differs on `long_three_segments` by up to 0.0033131 in BGE/PCA features,
0.0026307 in intent probabilities, and 0.0064231 in tier probabilities. The other
five inputs match at the original tolerances. Intermediate tensor inspection
located a 1.788e-7 floating-point difference before dynamic quantization:
input/scale is 44.5 on AVX-512 and 44.500019 on AVX2, producing adjacent integers.
This is separate from U8S8 saturation and remains after explicit unsigned-weight
conversion. The measured CPU profiles and first divergent tensor are recorded in
`runtime_compatibility.json`.

On x86, only the `long_three_segments` full-pipeline reference fixture therefore
uses absolute error bounds of 0.0035 for the three BGE/PCA feature blocks, 0.003
for intent probabilities, and 0.007 for tier probabilities, modestly above its
measured maxima. Its non-BGE features and all five other fixtures retain their
original feature and probability tolerances. Across all six fixtures, both
classifier labels and the final policy intent, tier, thinking, prompt, reset,
and persisted state must still match the reference exactly. All six inputs retain their labels on the measured native
and AVX2 profiles; this does not guarantee unchanged decisions for arbitrary
requests or unmeasured hardware. The sixteen fixed-feature classifier golden
cases keep the original tight numerical checks (relative 1e-10, absolute 1e-12).
Original golden data, trained assets, and their pinned hashes are unchanged.

Native Apple M5 ARM64 validation used the same pinned runtime versions above
(without Rosetta). The default fused graph differed from the source by as much
as 0.095450 in tier probabilities. Disabling only
`MatMulIntegerToFloatFusion;GeluFusionL2` reduces that maximum to 0.002824;
four fixtures then match the original feature and probability tolerances.
These are the optimizer names in
[ONNX Runtime 1.24.4](https://github.com/microsoft/onnxruntime/blob/v1.24.4/onnxruntime/core/optimizer/gelu_fusion.h#L20-L27),
and the session's optimizer list is semicolon-delimited.

On this ARM64 profile, `code_debug` differs by at most 0.0063972 in BGE/PCA
features, 0.0006392 in intent probabilities and 0.0017490 in tier probabilities.
Only ARM64 uses corresponding fixture bounds of 0.0065, 0.00065 and 0.0018.
`long_three_segments` differs by at most 0.0060652 in BGE/PCA features,
0.0014952 in intent probabilities and 0.0028239 in tier probabilities; its
ARM64 semantic bound is 0.0062, with the existing probability bounds unchanged.
All non-BGE features and the other four ARM64 fixtures keep their strict checks.
The full native ARM64 tests require exactly the same classifier labels, final
policy decisions and next states as the frozen source, plus the unchanged
sixteen fixed-feature goldens. These measured fixture bounds are not a general
accuracy guarantee for unseen inputs or other ARM implementations.

Instrumenting the original graph with basic optimizations reproduces the ARM
`code_debug` rounding boundary: a 1.4902e-8 input difference at layer 1 dynamic
quantization changes input/scale from 9.49999905 to 9.5, producing 151 versus
152. This is the same discrete amplification mechanism observed on AVX2;
`runtime_compatibility.json` records the tensor location and values.
