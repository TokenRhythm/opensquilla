This package contains the exact LightGBM pair selected by the 2026-10-20 model-policy experiment:

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
helpers were removed; encoder construction, close, and encode retain the
original memory-cache path with only the unused persistence branches removed.
Tokenization, batching, ordering, and normalization are unchanged. ONNX session
configuration additionally enables `session.x64quantprecision=1` to avoid x64
U8S8 saturation on CPUs without VNNI; the encoder file is unchanged.
Other retained function/method definitions preserve the original arithmetic;
imports, module/class descriptions, and type-only annotations/casts were adjusted.
`features/text.py` and `input_contract.py` retain the original extracted definitions.
The original training package declares `LicenseRef-Proprietary`; that declaration
and its original package metadata are retained in `assets/upstream-pyproject.toml`.
The frozen snapshot contains no separate license file. These source files retain
their upstream licensing and are not relicensed by the surrounding project.

The local adapter removes the registry/database dependency. It verifies the
original manifest and the selected inference closure before native model loading
or pickle deserialization. The two model text files are distributed as gzip;
verification hashes the decompressed original bytes. The encoder, tokenizer,
three joblib transformers, calibration files, bundles, and feature metadata are
unchanged. The two BGE files use short installed names (`bge/model.onnx` and
`bge/tokenizer.json`) to avoid the Windows packaged path length that caused a
resource-read failure; their original paths and hashes remain in the source
manifests. Training run manifests and training metrics remain referenced in the
original manifest but are omitted from the inference distribution.

The 380 float32 features are current-request BGE/PCA64, current-request
character TF-IDF/SVD100, rules48, history BGE/PCA64, route-history16,
previous-answer BGE/PCA64, previous-signals19, and context5. BGE uses CLS pooling,
float32 L2 normalization, a fixed 512-token input, and frozen segment truncation.
An empty history or previous answer produces a zero semantic block. There is no
extra scaling step. Both heads use the original temperature calibration.

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

Cross-CPU parity is not fully resolved: AVX2 passes the other five cases at the
existing tolerances, but `long_three_segments` still differs by up to 0.0033131
in features and 0.0064231 in tier probabilities. Intermediate tensor inspection
located a 1.788e-7 floating-point difference before dynamic quantization:
input/scale is 44.5 on AVX-512 and 44.500019 on AVX2, producing adjacent integers.
This is separate from U8S8 saturation and remains after explicit unsigned-weight
conversion. The strict reference tests and all golden values remain unchanged;
they continue to expose this unresolved AVX2 limitation. The measured CPU
profiles and first divergent tensor are recorded in `runtime_compatibility.json`.
