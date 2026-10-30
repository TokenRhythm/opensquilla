This package contains the exact LightGBM pair selected by the 2026-10-20 model-policy experiment:

- Model set: `router-lightgbm-99d6ff75-d444135-a1`.
- Original manifest SHA-256: `29fb79337458c4f35d3e0d1148fbdb1175c20069a5fa8ee6441633b0e2eee357`.
- Training code identity: `git:d4441356584423be07361495e55b488c95c5db43`.
- Runtime snapshot: `router-training-a34e5ae` beside the experiment's frozen OpenSquilla source.

`assets/provenance.json` records the original source paths and hashes. Files under
`_vendor/features/`, `_vendor/ids.py`, and `_vendor/errors.py` are unchanged copies.
`input_contract.py` and `calibration.py` contain verbatim extracted definitions.
The original training package declares `LicenseRef-Proprietary`; that declaration
and its original package metadata are retained in `assets/upstream-pyproject.toml`.
The frozen snapshot contains no separate license file. These source files retain
their upstream licensing and are not relicensed by the surrounding project.

The local adapter removes the registry/database dependency. It verifies the
original manifest and the selected inference closure before native model loading
or pickle deserialization. The two model text files are distributed as gzip;
verification hashes the decompressed original bytes. The encoder, tokenizer,
three joblib transformers, calibration files, bundles, and feature metadata are
unchanged. Training run manifests and training metrics remain referenced in the
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
exactly, and all sixteen original golden cases agree within 2.23e-16.
`assets/runtime_compatibility.json` records the measured results. ONNX Runtime
1.30.0 changed the code-debug fixture's features by up to 0.006055 and tier
probabilities by up to 0.004872, so it is not interchangeable for this bundle.
