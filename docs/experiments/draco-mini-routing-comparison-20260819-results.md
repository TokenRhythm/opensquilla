# DRACO Mini B0/B1/B2/B4/G1/S4 实验结果

## 完整性

- 冻结任务矩阵已观测 `60/60` 行；其中 Judge/quality 可评分 `57/60`。
- 各臂 completed/scored：`B0 10/10, B1 10/10, B2 9/10, B4 9/10, G1 10/10, S4 9/10`；三项原生 protocol failure 均保留为未评分观测。
- Primary operational utility 将执行失败显式计为 `U=0`（每臂分母 10）；质量矩阵仍写 `EXEC_FAIL`，没有伪造 Judge 0 分。
- 共同 complete-case 诊断统一剔除 f004，六臂都使用相同 9 题；不是仅对失败臂选择性删题。
- Primary resume classifier action counts：`complete=52, metadata_only=5, regenerate=3`。
- Primary 由 `1` 个 initial/resume wave 按因果顺序离线合并；报告生成未调用模型或网络。
- DRACO input SHA-256：`1eb4e618c8df8e7f68bded3d2b6f77a541744aa1072eb338835b776183188a8d`。

## 实验臂定义

| Arm | Frozen definition | Manifest evidence |
|---|---|---|
| B0 | fixed Claude Fable 5 (`anthropic/claude-fable-5`) | label=`fixed_claude_fable5`; group_spec SHA-256=`cf926ab9bbefe9adcadce92fe0f4ce3b1015e1d40174ffa06ec8e032ea21baee` |
| B1 | current SquillaRouter single-model tiers (`router_single`) | label=`single_model_routing`; group_spec SHA-256=`48199edfb9c0b6183ac5c44a178f56a548c935d21d65a865ecbdb4a844ef8b3a` |
| B2 | fixed 4-proposer ensemble + GLM 5.2 aggregator | label=`b2_quality_first_static_openrouter_b5`; group_spec SHA-256=`30db08680de4864bedef3852bcd4cd73414aecec8f1136e3fcc597cc7c09229d` |
| B4 | fixed GPT-5.6-sol (`openai/gpt-5.6-sol`) | label=`fixed_gpt56_sol`; group_spec SHA-256=`ded468347db7cfe454df365e780cba47677d9a6b69e3c33642e8cdcf08fccf65` |
| G1 | current dynamic routing + fusion (`router_dynamic`) | label=`ranking_router_dynamic`; group_spec SHA-256=`892b2689e66be6805a57ea36db22ee4c2bdd1ed742de5e04bfe653828577fc04` |
| S4 | restricted four-model single router: c0=`qwen/qwen3-8b`; c1=`deepseek/deepseek-v4-flash`; c2=`qwen/qwen3.7-plus`; c3=`deepseek/deepseek-v4-pro` | label=`single_model_routing_restricted_4`; group_spec SHA-256=`288955f3602e30b04f22234c73e0f5f49dc6ab630ec083f9dc71b4bcb04963b6` |

## Offline validator-domain correction

- Current resume classifier 将全部 `10` 个 B2 rows 先判为 regenerate，因为 manifest compatibility contract 按设计只保存 `experiment_config.sha256`，classifier 却尝试从该字段内读取 `ensemble`/`routing`，从而产生 `missing_expected_b2_ensemble_contract`。
- 报告器从 expected-manifest 绑定的 effective/resolution artifacts 读取配置，逐字复现 runner compatibility 投影；投影 SHA-256 `sha256:16e9993f177ad234dfb57e0ece81ff57c283441759a3e8743ff3897fda582bb4` 必须精确等于 B2 contract pin `sha256:16e9993f177ad234dfb57e0ece81ff57c283441759a3e8743ff3897fda582bb4`，随后只在内存中的 deep-copy B2 validator contract 注入该投影。
- Hydrate 前后 selected source/line/row SHA 与非 B2 state 必须全等；9 个 surface-complete/Judge-complete B2 rows 的唯一 delta 是删除上述伪 reason 并变为 complete。B2/f004 仍为 regenerate，伪 reason 被真实 `insufficient_b2_configured_quorum` 替换。
- B2 hydrate 前/后 action counts：`{'complete': 37, 'metadata_only': 1, 'regenerate': 22}` → `{'complete': 46, 'metadata_only': 1, 'regenerate': 13}`。
- 另有 `10` 个 surface-complete G1 rows 被未修正 classifier 判为 regenerate；每行唯一 generation reason 均为 `invalid_g1_task_analyzer_execution_contract`。
- 根因是 resume validator 在 `ensemble_call_core_reasons` 及 G1 lifecycle 的多处 `g1_registry_contract_reasons` 调用中遗漏 manifest-authenticated `task_analyzer_execution_contract`；并非 row plan 漂移。
- 每个受影响 row 的 declared plan contract 必须与 manifest expected contract whole-equal（SHA-256 `5b37db44dde82094838edcf86451c1a288387e374ff988908e85a867093680a9`），且 registry SHA-256 精确为 `3c15068ce62c7b3d98bd6aa6725a480e83074afac0ec5bc6676707cf6a83b2c0`。
- 仅在 registry canonical hash 精确匹配时临时包装两个函数并注入 expected analyzer contract：core `49` 次、lifecycle/registry `128` 次；`finally` 双恢复，selected source/line/row SHA 必须不变。
- 最终 patched classifier action counts：`{'complete': 52, 'metadata_only': 5, 'regenerate': 3}`；cost-only `metadata_only` 不会被静默改写为 `complete`。
- 两项均为离线 validator 参数域修正，不修改远端 worktree、result rows、generation、Judge、成本或分数；独立 execution/Judge/quality/fingerprint 门仍全部执行。

## Relaxed cost-metadata audit

- 允许报告的 `metadata_only` pairs 共 `5`：`B0/3b5505fb-dab6-44cb-9a14-746b1cd82d25`, `G1/3b5505fb-dab6-44cb-9a14-746b1cd82d25`, `G1/ca0edd2d-c9b8-4b85-9b40-754f4865579a`, `G1/f6de7687-7cff-4f68-93ea-f632bf6266af`, `G1/aca95495-b0fe-4330-90fc-d2fd1e2c709e`。Primary 中为 `5` 项（`B0/3b5505fb-dab6-44cb-9a14-746b1cd82d25`, `G1/3b5505fb-dab6-44cb-9a14-746b1cd82d25`, `G1/ca0edd2d-c9b8-4b85-9b40-754f4865579a`, `G1/f6de7687-7cff-4f68-93ea-f632bf6266af`, `G1/aca95495-b0fe-4330-90fc-d2fd1e2c709e`）。
- 这是 fail-closed 的 cost-only 例外：每项必须 `generation_valid=true`、`judge_complete=true`，generation/Judge/audit/fatal reasons 全空，且唯一 cost reason 精确为 `cost_metadata_incomplete`；final text、attempt evidence、Judge、quality、fingerprint 与 row error 门保持严格。
- 这些行的 actual LLM account 明确保持 `cost_complete=false` 且 unknown requests > 0；报告器据此将 recorded cost 标为 lower bound。Unknown requests 和 coverage 原样计入下表，不补零、不估成完整成本，也不触发 G1 重跑。
- 行级 lower-bound accounting：`B0/3b5505fb-dab6-44cb-9a14-746b1cd82d25` requests=144, unknown=1, known=99.31%, recorded=$3.076292000 (lower bound); `G1/3b5505fb-dab6-44cb-9a14-746b1cd82d25` requests=167, unknown=1, known=99.40%, recorded=$2.980951300 (lower bound); `G1/ca0edd2d-c9b8-4b85-9b40-754f4865579a` requests=90, unknown=1, known=98.89%, recorded=$1.074044500 (lower bound); `G1/f6de7687-7cff-4f68-93ea-f632bf6266af` requests=159, unknown=1, known=99.37%, recorded=$3.670842680 (lower bound); `G1/aca95495-b0fe-4330-90fc-d2fd1e2c709e` requests=135, unknown=1, known=99.26%, recorded=$2.428001800 (lower bound)。

## 原生 protocol failures

仅下列三个冻结 key 可进入失败路径；每项都已通过 group/task/fingerprint、3/3 budget、prior=0、attempt ordinal/ID、精确错误序列、Judge 缺失、selected spend=0 与 actual accounting 的 closed gate。其他失败仍会终止报告。

| Arm / task | Kind | Budget | Actual req | Unknown | Actual LLM$ | Known% | Cost complete | Source | Row SHA-256 |
|---|---|---:|---:|---:|---:|---:|---|---|---|
| `B2/f004b46b-c0e7-4e86-a072-c7491328d538` | `strict_quorum` | 3/3 | 12 | 1 | 0.534564759 | 91.6667% | `false` | `draco_ensemble_20260819-005239.jsonl:39` | `c16dc8d347b3a72733747c3cb1e4e5c2ed81e90bdfc5e22847722ae0e08820ce` |
| `B4/f004b46b-c0e7-4e86-a072-c7491328d538` | `repeated_empty_response` | 3/3 | 35 | 0 | 6.865969500 | 100.0000% | `true` | `draco_ensemble_20260819-005239.jsonl:41` | `ae8fb527e969e4a3acc42b45df6f452c42a3f78f996817d683f3d3a4b13ce228` |
| `S4/f004b46b-c0e7-4e86-a072-c7491328d538` | `repeated_empty_response` | 3/3 | 25 | 0 | 0.207863128 | 100.0000% | `true` | `draco_ensemble_20260819-005239.jsonl:42` | `44259f1cc3a33110f0d6c7b72c02e30ab557d0e5fe0c8a7fb91ff1a79f5cee47` |

Attempt terminal errors：

- `B2/f004b46b-c0e7-4e86-a072-c7491328d538`：`tool-enabled aggregation requires 3 fully completed proposer draft(s), but only 1 completed; aggregation was not started` → `tool-enabled aggregation requires 3 fully completed proposer draft(s), but only 2 completed; aggregation was not started` → `tool-enabled aggregation requires 3 fully completed proposer draft(s), but only 2 completed; aggregation was not started`；logical attempt IDs `0e7d24c8a0114ae39a488d474e2b2372,2f286a31136b40e899cb5b8bc4090345,31f35814461248c0aecdbc0a5eedcc82`。
- `B4/f004b46b-c0e7-4e86-a072-c7491328d538`：`Provider returned no visible response for a large input. Send the material as an attachment, summarize or shorten the prompt, or use a stronger model.` → `Provider returned no visible response for a large input. Send the material as an attachment, summarize or shorten the prompt, or use a stronger model.` → `Provider returned no visible response for a large input. Send the material as an attachment, summarize or shorten the prompt, or use a stronger model.`；logical attempt IDs `b1f7ae748f2142a3aba0cd570c131232,6b437d5b96cb44fa86096842e72aa17f,4cf1107e37374c4c88bb219b80782812`。
- `S4/f004b46b-c0e7-4e86-a072-c7491328d538`：`Provider returned no visible response for a large input. Send the material as an attachment, summarize or shorten the prompt, or use a stronger model.` → `Provider returned an empty response` → `Provider returned an empty response`；logical attempt IDs `da52b5186a1b43688dc54f1c8188351d,16da737ff9794278a9e0c6cbce141f90,d349d4439f964f34a7370e153c745cc7`。

三项失败的全部 generation attempts 均计入 Actual；Selected 只含成功选中的 generation/Judge，失败 cell 的 selected requests/cost 为 0。Primary 未使用 fresh replacement。

## Post-hoc fresh-budget reruns（不属于 primary）

观察到三项 primary failure 后，分别以相同 frozen contract、无 resume history、fresh `3` 次上限串行重跑。原失败行和原成本均保留；这些结果不改写上面的 57/60 primary。

| Arm / task | Outcome | Fresh attempts | AvgQ | AvgPass | Actual Gen$ | Judge$ | Actual LLM$ | Req X/E/M/U | Result SHA-256 | Manifest SHA-256 |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---|
| `B2/f004b46b-c0e7-4e86-a072-c7491328d538` | EXEC_FAIL | 3/3 | — | — | 0.472376287 | 0.000000000 | 0.472376287 | 12/0/0/0 | `9b2bb7731091781ead930b1b534432f3d8002814693ab0eda8bdaf0542414b5d` | `9791081d16f8ee0a260da8359ab59b6ac172855ee41b160b25f9038b19c6919a` |
| `B4/f004b46b-c0e7-4e86-a072-c7491328d538` | EXEC_FAIL | 3/3 | — | — | 4.689597750 | 0.000000000 | 4.689597750 | 28/0/0/0 | `376373062d17d73c0921ea2ab203ded47a97d771de6980dc3b42e607b7616679` | `9b43691bf539197fb2cfe49afe1bd6e6406363c010afa5dcc7b23dd9b4f36b1d` |
| `S4/f004b46b-c0e7-4e86-a072-c7491328d538` | SCORED | 1/3 | 63.5927 | 64.17% | 0.073797152 | 2.079012000 | 2.152809152 | 123/0/0/0 | `bea4496f10d45f0107b323dacb7f5921311912ec44128eb3430d6b4f072c54e7` | `c80cfb78eff0d2ee95f98bf3e23249651380a6ac384fd27e8e3b37838b5f179f` |
- `B2/f004b46b-c0e7-4e86-a072-c7491328d538` 再次失败：`tool-enabled aggregation requires 3 fully completed proposer draft(s), but only 2 completed; aggregation was not started`。
- `B4/f004b46b-c0e7-4e86-a072-c7491328d538` 再次失败：`Provider returned no visible response for a large input. Send the material as an attachment, summarize or shorten the prompt, or use a stronger model.`。
- Fresh reruns 新增 exact LLM spend `$7.314783189`；它与 primary Actual 分账，不计入 primary 成本表。
- 恢复 `1/3`：`S4/f004b46b-c0e7-4e86-a072-c7491328d538`。该成功是 outcome-conditioned post-hoc 观测，只进入下方 sensitivity。

## 分组指标

| Arm | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$ | Total Gen$ | Gen exact | Avg Input | Avg Output | Avg Reason | Avg Cache | Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | Avg LLMReq | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B0 | 10 | 10 | 69.7675 | 70.87% | 0 | 4.745483 | 47.454830 | 10/10 | 360808.3 | 22748.0 | 17208.5 | 0.0 | 5539.5 | 383556.3 | 15.60 | 100.00% | 23.30 | 7.70 | 233584 | 896788 |
| B1 | 10 | 10 | 57.3393 | 60.04% | 0 | 1.506192 | 15.061918 | 10/10 | 274391.4 | 9305.2 | 4035.9 | 12697.6 | 5269.3 | 283696.6 | 13.10 | 90.00% | 20.00 | 6.90 | 159534 | 299821 |
| B2 | 10 | 9 | 66.9465 | 68.68% | 0 | 0.567998 | 5.679984 | 10/10 | 470931.4 | 70194.8 | 25336.3 | 276218.6 | 44858.5 | 541126.2 | 8.40 | 80.00% | 25.90 | 17.50 | 501703 | 1924432 |
| B4 | 10 | 9 | 62.2350 | 63.20% | 0 | 1.831516 | 18.315158 | 10/10 | 688876.7 | 19191.1 | 14412.7 | 530304.9 | 4778.4 | 708067.8 | 69.00 | 80.00% | 85.90 | 16.90 | 454439 | 1232513 |
| G1 | 10 | 10 | 64.6580 | 65.74% | 0 | ≥2.166452 | ≥21.664519 | 6/10 | 567263.3 | 134818.4 | 89587.3 | 157838.8 | 45231.1 | 702081.7 | 10.80 | 100.00% | 36.10 | 25.30 | 979582 | 2396512 |
| S4 | 10 | 9 | 63.8941 | 65.18% | 0 | 0.045282 | 0.452817 | 10/10 | 238680.9 | 6866.1 | 3844.5 | 192102.4 | 3021.6 | 245547.0 | 14.40 | 90.00% | 22.00 | 7.60 | 152976 | 437940 |

`AvgQ`/`AvgPass` 只平均 `Done` 行；B2/B4/S4 的 EXEC_FAIL 未伪造 Judge 分数。Input/Output/Reason/Cache/Visible/Tokens、Tools、Steps、LLMReq 和 Gen$ 均为 selected-generation scope；失败行因没有 selected attempt 在这些列贡献 0，但其 terminal-attempt latency 仍进入 p50/p95。
逐行 `Visible = max(Output - Reason, 0)` 后再取均值；Cache 是 Input 的子集，不重复加入 Avg Tokens。`≥` 表示 generation 成本存在未知请求、只能作为下界；`Gen exact` 是 selected generation 成本精确的任务数。p50/p95 使用线性插值。所有失败 physical attempts 的真实用量和成本仍完整保留在下方 Actual ledger；failure-adjusted U 分析也保持固定分母 10。

### Post-hoc successful-rerun sensitivity（同格式）

下表只把成功的 fresh rerun 投影到对应失败 cell；本次仅 S4/f004 恢复。它不替代上表 primary。

| Arm | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$ | Total Gen$ | Gen exact | Avg Input | Avg Output | Avg Reason | Avg Cache | Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | Avg LLMReq | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S4 + fresh f004 | 10 | 10 | 63.8640 | 65.08% | 0 | 0.052661 | 0.526614 | 10/10 | 242342.5 | 8398.7 | 4896.9 | 192153.6 | 3501.8 | 250741.2 | 14.90 | 100.00% | 22.80 | 7.90 | 152976 | 318000 |


## 成本与 coverage

| Arm | Selected Gen$ | Actual Gen$ | Judge$ | Selected LLM$ | Selected req X/E/M/U | Known% | Exact% | Actual LLM$ (LB if incomplete) | Actual req X/E/M/U | Known% | LLM complete | Full complete |
|---|---:|---:|---:|---:|---|---:|---:|---:|---|---:|---|---|
| B0 | 47.454830 | 48.156090 | 14.123474 | 61.578304 | 1205/0/0/0 | 100.0% | 100.0% | 62.279564 | 1210/0/0/1 | 99.9% | 10/10 → 9/10 | 0/10 → 0/10 |
| B1 | 15.061918 | 15.061918 | 14.833948 | 29.895866 | 1197/0/0/0 | 100.0% | 100.0% | 29.895866 | 1197/0/0/0 | 100.0% | 10/10 → 10/10 | 1/10 → 1/10 |
| B2 | 5.679984 | 6.214549 | 17.474176 | 23.154160 | 1183/0/0/0 | 100.0% | 100.0% | 23.688725 | 1194/0/0/1 | 99.9% | 10/10 → 9/10 | 2/10 → 1/10 |
| B4 | 18.315158 | 25.181128 | 11.503790 | 29.818948 | 1177/0/0/0 | 100.0% | 100.0% | 36.684918 | 1212/0/0/0 | 100.0% | 10/10 → 10/10 | 2/10 → 1/10 |
| G1 | 21.664519 | 21.664519 | 16.142374 | 37.806893 | 1377/0/0/4 | 99.7% | 99.7% | 37.806893 | 1377/0/0/4 | 99.7% | 6/10 → 6/10 | 0/10 → 0/10 |
| S4 | 0.452817 | 0.660680 | 12.326658 | 12.779475 | 1084/0/0/0 | 100.0% | 100.0% | 12.987338 | 1109/0/0/0 | 100.0% | 10/10 → 10/10 | 1/10 → 0/10 |

`X/E/M/U` 分别为 exact / estimated / mixed / unknown request。Selected LLM 只含成功选中的 generation 与 Judge；三项失败 cell 经 closed gate 证明 selected request/cost 为 0。Actual LLM 包含 primary 每个 observed cell 的全部 generation attempts 与 Judge，因此保留三项失败成本。Unknown 不按 `$0` 处理。Full complete 还受本地 Web 等外部工具成本证据约束；行级 actual 不含无结果 aborted shard 或 preflight，故不是 whole-account/window total。若提供 post-hoc replacement，其成本只在 sensitivity incident 节单列，不进入此 primary 表。
存在 `metadata_only` 时，Actual LLM$ 必为 lower bound，且 LLM/Full complete 分母中的不完整行不会被强制改写为 complete。

## Primary failure-aware 同题配对比较（U，n=10）

| Arm - baseline | Pairs | Mean ΔU | 95% CI | W/T/L | Seed |
|---|---:|---:|---|---|---|
| B1 - B0 | 10 | -12.4282 | [-19.5631, -6.5655] | 1/0/9 | `draco:failure-aware-primary:B1:B0` |
| B2 - B0 | 10 | -9.5156 | [-22.7385, 0.1990] | 2/0/8 | `draco:failure-aware-primary:B2:B0` |
| B4 - B0 | 10 | -13.7560 | [-26.0469, -3.9356] | 2/0/8 | `draco:failure-aware-primary:B4:B0` |
| G1 - B0 | 10 | -5.1095 | [-11.0844, 1.2527] | 3/0/7 | `draco:failure-aware-primary:G1:B0` |
| S4 - B0 | 10 | -12.2628 | [-24.4651, -2.8755] | 1/0/9 | `draco:failure-aware-primary:S4:B0` |
| B2 - B1 | 10 | 2.9125 | [-11.2511, 14.1891] | 6/0/4 | `draco:failure-aware-primary:B2:B1` |
| B4 - B1 | 10 | -1.3278 | [-15.1502, 10.5806] | 5/0/5 | `draco:failure-aware-primary:B4:B1` |
| G1 - B1 | 10 | 7.3186 | [-0.2069, 14.7895] | 6/1/3 | `draco:failure-aware-primary:G1:B1` |
| S4 - B1 | 10 | 0.1653 | [-12.7643, 9.6062] | 7/0/3 | `draco:failure-aware-primary:S4:B1` |

每项比较包含相同 10 题。三项 EXEC_FAIL 的 operational utility `U=0`；其余 U=Judge quality。CI 使用 task-level paired percentile bootstrap，每项固定 seed、`20000` 次重采样；未作多重比较修正。

## Complete-case 共同 9 题诊断

所有六臂统一剔除 f004；该表是共同 complete-case 诊断，不是 primary。

### 共同 9 题完整指标（同格式）

| Arm | Rows | Done | AvgQ | AvgPass | JudgeErr | Avg Gen$ | Total Gen$ | Gen exact | Avg Input | Avg Output | Avg Reason | Avg Cache | Avg Visible | Avg Tokens | Avg Tools | Tool% | Avg Steps | Avg LLMReq | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| B0 | 9 | 9 | 70.9446 | 71.99% | 0 | 5.112116 | 46.009040 | 9/9 | 396884.9 | 22865.3 | 17138.4 | 0.0 | 5726.9 | 419750.2 | 16.78 | 100.00% | 25.00 | 8.22 | 230888 | 916826 |
| B1 | 9 | 9 | 58.1388 | 60.88% | 0 | 1.619798 | 14.578183 | 9/9 | 300168.0 | 9131.4 | 3813.4 | 14108.4 | 5318.0 | 309299.4 | 13.89 | 88.89% | 21.22 | 7.33 | 159304 | 312013 |
| B2 | 9 | 9 | 66.9465 | 68.68% | 0 | 0.631109 | 5.679984 | 9/9 | 523257.1 | 77994.2 | 28151.4 | 306909.6 | 49842.8 | 601251.3 | 9.33 | 88.89% | 28.78 | 19.44 | 607182 | 1985910 |
| B4 | 9 | 9 | 62.2350 | 63.20% | 0 | 2.035018 | 18.315158 | 9/9 | 765418.6 | 21323.4 | 16014.1 | 589227.7 | 5309.3 | 786742.0 | 76.67 | 88.89% | 95.44 | 18.78 | 442957 | 738217 |
| G1 | 9 | 9 | 65.3741 | 66.38% | 0 | ≥1.996321 | ≥17.966888 | 5/9 | 581024.9 | 119393.4 | 74107.4 | 167696.4 | 45286.0 | 700418.3 | 10.67 | 100.00% | 36.00 | 25.33 | 904581 | 1970819 |
| S4 | 9 | 9 | 63.8941 | 65.18% | 0 | 0.050313 | 0.452817 | 9/9 | 265201.0 | 7629.0 | 4271.7 | 213447.1 | 3357.3 | 272830.0 | 16.00 | 100.00% | 24.44 | 8.44 | 128009 | 318894 |

该表从每个臂同时删除同一个 f004 cell 后重新计算；Rows/Done 均为 9，成本、token、工具、步骤、请求与延迟也全部只使用这 9 题，而不是沿用主表的 10 题分母。`≥` 与 `Gen exact` 的定义同主表。

### 共同 9 题同题配对比较

| Arm - baseline | Pairs | Mean ΔQ | 95% CI | W/T/L | Seed |
|---|---:|---:|---|---|---|
| B1 - B0 | 9 | -12.8058 | [-20.5286, -6.3425] | 1/0/8 | `draco:complete-case-common-9-task:B1:B0` |
| B2 - B0 | 9 | -3.9981 | [-9.5238, 1.9057] | 2/0/7 | `draco:complete-case-common-9-task:B2:B0` |
| B4 - B0 | 9 | -8.7096 | [-14.7835, -2.0676] | 2/0/7 | `draco:complete-case-common-9-task:B4:B0` |
| G1 - B0 | 9 | -5.5705 | [-12.0645, 1.5798] | 3/0/6 | `draco:complete-case-common-9-task:G1:B0` |
| S4 - B0 | 9 | -7.0505 | [-13.0244, -1.3345] | 1/0/8 | `draco:complete-case-common-9-task:S4:B0` |
| B2 - B1 | 9 | 8.8077 | [2.0942, 16.6953] | 6/0/3 | `draco:complete-case-common-9-task:B2:B1` |
| B4 - B1 | 9 | 4.0962 | [-4.1092, 13.1210] | 5/0/4 | `draco:complete-case-common-9-task:B4:B1` |
| G1 - B1 | 9 | 7.2353 | [-1.1607, 15.5118] | 5/1/3 | `draco:complete-case-common-9-task:G1:B1` |
| S4 - B1 | 9 | 5.7553 | [0.9920, 11.4343] | 7/0/2 | `draco:complete-case-common-9-task:S4:B1` |

### 共同 9 题 AvgQ 排名

| Rank | Arm | Tasks | AvgQ |
|---:|---|---:|---:|
| 1 | B0 | 9 | 70.9446 |
| 2 | B2 | 9 | 66.9465 |
| 3 | G1 | 9 | 65.3741 |
| 4 | S4 | 9 | 63.8941 |
| 5 | B4 | 9 | 62.2350 |
| 6 | B1 | 9 | 58.1388 |

## Post-hoc sensitivity（含 successful rerun）

| Arm - baseline | Pairs | Mean ΔU | 95% CI | W/T/L | Seed |
|---|---:|---:|---|---|---|
| S4 - B0 | 10 | -5.9036 | [-11.7559, -0.3566] | 2/0/8 | `draco:post-hoc-replacement:S4:B0` |
| S4 - B1 | 10 | 6.5246 | [1.9619, 11.8324] | 8/0/2 | `draco:post-hoc-replacement:S4:B1` |

该表在观察到 primary failure 后才纳入成功的 targeted fresh rerun；仅用于 sensitivity，不得替代 57/60 primary、failure-aware n=10 或共同 9 题诊断。未恢复的原生失败仍按 U=0。

## 同题质量矩阵

| Domain | Task | B0 | B1 | B2 | B4 | G1 | S4 |
|---|---|---:|---:|---:|---:|---:|---:|
| Academic | `0c2c668a-c3b` | 90.02 | 86.12 | 85.54 | 72.65 | 75.06 | 85.20 |
| Finance | `a78eed67-ebe` | 45.52 | 28.05 | 53.40 | 37.11 | 46.60 | 22.87 |
| General Knowledge | `ce522bc2-e8a` | 72.58 | 61.88 | 71.46 | 53.24 | 56.81 | 68.26 |
| Law | `3b5505fb-dab` | 82.93 | 69.88 | 68.98 | 72.09 | 75.30 | 77.71 |
| Medicine | `cd19ee22-ded` | 85.26 | 46.58 | 77.78 | 63.46 | 69.87 | 71.15 |
| Needle in a Haystack | `ca0edd2d-c9b` | 62.59 | 63.27 | 74.60 | 59.86 | 76.87 | 70.07 |
| Personalized Assistant | `f004b46b-c0e` | 59.17 | 50.14 | EXEC_FAIL‡ | EXEC_FAIL‡ | 58.21 | EXEC_FAIL‡ |
| Shopping/Product Comparison | `f6de7687-7cf` | 74.05 | 71.50 | 70.65 | 63.44 | 71.50 | 73.37 |
| Technology | `e1f2c310-d31` | 76.99 | 57.82 | 60.47 | 86.73 | 83.48 | 58.41 |
| UX Design | `aca95495-b0f` | 48.56 | 38.16 | 39.65 | 51.53 | 32.87 | 48.00 |

‡ `EXEC_FAIL` 表示 generation 预算 3/3 耗尽且 Judge 未运行；它不是 Judge 0 分。只有 failure-aware U 分析将其操作性效用计为 0。

## Wave 取证

| Ordinal | Stamp | Status | Rows | Groups | Result SHA-256 | Manifest SHA-256 |
|---:|---|---|---:|---|---|---|
| 1 | `20260819-005239` | `result_incomplete` | 60 | B0,B1,B2,B4,G1,S4 | `2c8cff41396b0cccae10a71381b26e1765d46699a41e57fc32a43964e5c86823` | `638e418faf87e5721afe7b5cc288660742608a6d9519b290427dab02e01ddad8` |

No-history post-hoc fresh-budget reruns（不属于 causal resume wave）：

| Arm | Stamp | Status | Rows | Groups | Result SHA-256 | Manifest SHA-256 |
|---|---|---|---:|---|---|---|
| B2 | `20260819-144706` | `resume_repair_incomplete` | 1 | B2,G1 | `9b2bb7731091781ead930b1b534432f3d8002814693ab0eda8bdaf0542414b5d` | `9791081d16f8ee0a260da8359ab59b6ac172855ee41b160b25f9038b19c6919a` |
| B4 | `20260819-151905` | `resume_repair_incomplete` | 1 | B4,G1 | `376373062d17d73c0921ea2ab203ded47a97d771de6980dc3b42e607b7616679` | `9b43691bf539197fb2cfe49afe1bd6e6406363c010afa5dcc7b23dd9b4f36b1d` |
| S4 | `20260819-161404` | `complete` | 1 | S4,G1 | `bea4496f10d45f0107b323dacb7f5921311912ec44128eb3430d6b4f072c54e7` | `c80cfb78eff0d2ee95f98bf3e23249651380a6ac384fd27e8e3b37838b5f179f` |

## 来源与限制

- Git HEAD：`797987b705d8e484e9c7975f56964631fd8752b4`；source tree SHA-256：`ea9f83938a07bc573c287f3ab409d2f6956aeb3d101016d2964363153283990f`；manifest dirty：`True`。
- Primary 多 wave 选择使用 runner resume classifier；没有拼接 JSONL、累加 wave summary 或 simple last-row-wins。Relaxed gate 仅限上文逐项披露的 closed cost-only `metadata_only`；三项失败使用独立 exact allowlist gate。
- Primary 是 60 observed / 57 scored；失败成本保留、Judge 缺失不被改写。Fresh reruns 是 outcome-conditioned post-hoc 证据，只在独立成本与 sensitivity 章节披露，不回填 primary。
- 若各臂未逐题交错执行，paired ΔQ 仍可能混入 provider/time drift；DRACO Mini 仅用于 10 题诊断。

生成时间：`2026-08-19T08:52:57.894584+00:00`。
