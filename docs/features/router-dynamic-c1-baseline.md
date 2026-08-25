# C1 dynamic single-route baseline

C1 is the formal default baseline from 2026-08-25 onward. Its authoritative
runtime values live in
`src/opensquilla/provider/router_dynamic_ranking_config.json`; the frozen
benchmark projection is
`configs/benchmarks/draco_c1_baseline_20260825.json`.

The shared ranking weights apply to every dynamic route. The empirical
`model_prior_weight=0.25` calibration is active only when all eligible models
belong to the measured DeepSeek V4/Qwen3.5 four-model pool. A larger pool keeps
the C1 shared weights but bypasses the empirical prior, preventing calibrated
and uncalibrated quality scores from being mixed.

Historical experiment inputs and reports remain immutable. New single-route
experiments must identify C1 as their baseline and freeze the four-model pool
when they compare changes to the empirical prior.
