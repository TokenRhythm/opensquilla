# anti_downgrade holds sessions at c3 (ensemble route) for the whole 600s window, overriding high-confidence low-tier classification

## Symptom

Once a turn is routed to `c3`, `anti_downgrade` holds the session at `c3` for the entire KV-cache window. Every subsequent turn that classifies into a lower tier — even with high classifier confidence — is overridden, and each of those turns is executed as a full `c3` route. In an active conversation the effective tier becomes `max(recent high tier, current classification)` until the window lapses, and the hold self-extends as long as turns keep arriving inside the window.

## Evidence from router decision trails

Single case (webchat session, 2026-08-27 14:13:41):

```json
{"stage": "classify", "tier": "c0", "route_class": "R0"},
{"stage": "confidence_gate", "applied": false, "threshold": 0.5, "default_tier": "c2"},
{"stage": "complaint_upgrade", "applied": false, "terms_count": 0},
{"stage": "anti_downgrade", "applied": true, "previous_tier": "c3", "window_seconds": 600.0},
{"stage": "final", "tier": "c3", "route_class": "R3"}
```

Classifier said `c0` with confidence 0.76; final tier was `c3`.

Chain (one session, 2026-08-22 20:12–20:50, 8 consecutive turns held at c3):

| turn | proposed | confidence | final |
|---|---|---|---|
| 18 | c1 | 0.56 | c3 |
| 19 | c3 | 0.40 | c3 |
| 20 | c2 | 0.42 | c3 |
| 21 | c1 | 0.49 | c3 |
| 23 | c0 | 0.68 | c3 |
| 24 | c1 | 0.69 | c3 |
| 25 | c0 | 0.53 | c3 |
| 26 | c1 | 0.59 | c3 |

Aggregate (single machine, 2026-07-22 → 2026-08-27): 124 of 801 decisions had `anti_downgrade` applied; 38 of those lifted the turn to `c3`.

## Why the c3-ensemble case is the core problem

The gate's stated purpose is KV-cache protection (`policy.py` `anti_downgrade`: "Hold the previous turn's tier when routing would drop below it"). That rationale assumes the held tier corresponds to a single reusable model whose prefix cache is still warm.

When `c3` is configured as an ensemble / multi-model fusion route (tier `ensemble_enabled = true`), the held tier no longer corresponds to one reusable model — and executing a trivial follow-up turn as a full fusion round costs multiples of a single-model route. The gate does not distinguish the two cases: one complex turn locks all trivial turns in the window into the most expensive execution mode, so task-difficulty routing stops being the deciding factor for the life of the window.

## Environment

- upstream `v0.5.4-20` (main `g9f0739e3f`), `squilla_router` enabled, custom preset, text tiers `c0`–`c3` with `c3` ensemble enabled
- defaults from `opensquilla.toml.example`: `kv_cache_anti_downgrade_enabled = true`, `kv_cache_anti_downgrade_window_seconds = 600`
