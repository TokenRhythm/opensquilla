# OpenSquilla Contributors

OpenSquilla uses GitHub pull requests, commits, release notes, and this
human-readable ledger together for contributor attribution. This file records
release-surface community work that can be harder to see when a release is
squash-merged or replayed onto `main`.

## Attribution Repairs

### PR #46 release-candidate sync

The release-candidate sync in [#46](https://github.com/opensquilla/opensquilla/pull/46)
collapsed community-authored work from `dev` into
[2158f56](https://github.com/opensquilla/opensquilla/commit/2158f56c1a0684168a013b4b4846233977d0067b)
without co-author trailers for the human commit authors below. This ledger entry
repairs the project-facing attribution record without rewriting protected branch
history. Later suspected cases were rechecked separately; their human authors
are represented on `main` by equivalent authored commits and/or co-author
trailers.

| Contributor | Repaired attribution | Evidence |
| --- | --- | --- |
| [@openvictory](https://github.com/openvictory) | README update carried into the 0.2.0rc1 release candidate. | [#46](https://github.com/opensquilla/opensquilla/pull/46), [`ff4bbec9`](https://github.com/opensquilla/opensquilla/pull/46/commits/ff4bbec93523582d893c7123421f7dc292bb6e38) |
| [@nice-code-la](https://github.com/nice-code-la) | DeepSeek reasoning replay fixes, Moonshot/Kimi routing defaults, and migration-importer replay work. | [#46](https://github.com/opensquilla/opensquilla/pull/46), [`4791ca5e`](https://github.com/opensquilla/opensquilla/pull/46/commits/4791ca5e04cf959a1dd57a0b076d2945677b89ed), [`80d2c17e`](https://github.com/opensquilla/opensquilla/pull/46/commits/80d2c17e9a62cf7d1d0a77b90fb7780e602eb425), [`15db2577`](https://github.com/opensquilla/opensquilla/pull/46/commits/15db25776f2233819f4ac229dd04ad807c584e23), [`04999013`](https://github.com/opensquilla/opensquilla/pull/46/commits/049990130d3607f076d9650db3d8bd92addf5a48), [`0aa075ac`](https://github.com/opensquilla/opensquilla/pull/46/commits/0aa075ac9aa758ac7d8c07793199e50ddaaae59a), [`3edc56a6`](https://github.com/opensquilla/opensquilla/pull/46/commits/3edc56a66a1392cf029ca926ff101ebbf784b3df) |
| cwan0785 (commit author name; GitHub account: [@Anonymous-4427](https://github.com/Anonymous-4427)) | Host-execution grant handling, quoted chat attachment parsing, and provider/tool edge hardening. | [#46](https://github.com/opensquilla/opensquilla/pull/46), [`af600aa5`](https://github.com/opensquilla/opensquilla/pull/46/commits/af600aa5eacbd8e3264b7f4258402acbdaaa8c36), [`eacbb2fb`](https://github.com/opensquilla/opensquilla/pull/46/commits/eacbb2fbe08e67231b5c793090726693a327b769), [`e301ec76`](https://github.com/opensquilla/opensquilla/pull/46/commits/e301ec764c560f57bfd0e39f3387e42369d73a01) |
| [@ab2ence](https://github.com/ab2ence) | macOS Seatbelt backend execution, denial escalation, and release-candidate type-check cleanup. | [#46](https://github.com/opensquilla/opensquilla/pull/46), [`fb1e6225`](https://github.com/opensquilla/opensquilla/pull/46/commits/fb1e6225e4db9cb0801ea347a89c2066e3e0601b), [`f73ac3eb`](https://github.com/opensquilla/opensquilla/pull/46/commits/f73ac3eb0044c64c79cfd18f9ec03d1bba9128ff), [`cf3b046f`](https://github.com/opensquilla/opensquilla/pull/46/commits/cf3b046f42a42efc951320b0af80e9d066dcf7d2) |
| [@kimjune01](https://github.com/kimjune01) | Provider stream timeout cleanup fix that prevents double-closing provider streams. | [#46](https://github.com/opensquilla/opensquilla/pull/46), [`06e3126d`](https://github.com/opensquilla/opensquilla/pull/46/commits/06e3126d8ebda4ad4cf349ca7be0d0804e0c008d) |

## OpenSquilla 0.5.0

The 0.5.0 stable release records new human contributor work after the 0.5.0
Preview 4 release. The published 0.5.0 release notes additionally thank every
contributor across the full 0.4.1 → 0.5.0 range; the per-preview sections
below keep the detailed evidence for work that shipped in Previews 1–4.

| Contributor | 0.5.0 contribution | Evidence |
| --- | --- | --- |
| [@jiaoqingrui](https://github.com/jiaoqingrui) | Unified channel platform workflows with hardened heartbeat delivery, and added daily usage reporting. | [#763](https://github.com/opensquilla/opensquilla/pull/763), [#736](https://github.com/opensquilla/opensquilla/pull/736) |
| [@Liu-RK](https://github.com/Liu-RK) | Aligned managed sandbox elevation, Full Host propagation, and workspace sessions, and aligned CodeTask with sandbox-off host access. | [#544](https://github.com/opensquilla/opensquilla/pull/544), [#669](https://github.com/opensquilla/opensquilla/pull/669) |
| [@LiuXinchen1997](https://github.com/LiuXinchen1997) | Optimized Model Ensemble quorum latency and timeouts. | [#704](https://github.com/opensquilla/opensquilla/pull/704) |
| [@nankingjing](https://github.com/nankingjing) | Hardened atomic file writes and failure cleanup. | [#594](https://github.com/opensquilla/opensquilla/pull/594) |
| [@nice-code-la](https://github.com/nice-code-la) | Published stable OSS installer aliases for the release mirror. | [#664](https://github.com/opensquilla/opensquilla/pull/664) |
| [@openvictory](https://github.com/openvictory) | Announced the Agentic Routing report's arXiv release in the README News section. | [#667](https://github.com/opensquilla/opensquilla/pull/667) |

## OpenSquilla 0.5.0rc4

The 0.5.0 Preview 4 release records new human contributor work after the
0.5.0 Preview 3 release. Contributors who also appeared in an earlier release
are included only for new work in this release range.

| Contributor | 0.5.0 Preview 4 contribution | Evidence |
| --- | --- | --- |
| [@HuaXiawithMoon](https://github.com/HuaXiawithMoon) | Replaced competing WeCom WebSocket heartbeats with the application-level heartbeat so connections remain stable. | [#582](https://github.com/opensquilla/opensquilla/pull/582) |
| [@ab2ence](https://github.com/ab2ence) | Restructured Model Ensemble guidance around clear static lineups and dynamic routing choices. | [#586](https://github.com/opensquilla/opensquilla/pull/586) |
| [@nice-code-la](https://github.com/nice-code-la) | Added the Alibaba Cloud OSS release mirror and its stable browser download page. | [#588](https://github.com/opensquilla/opensquilla/pull/588), [#636](https://github.com/opensquilla/opensquilla/pull/636) |
| [@nankingjing](https://github.com/nankingjing) | Hardened low-level transport, child-process, SQLite extension, and checkpoint failure handling. | [#598](https://github.com/opensquilla/opensquilla/pull/598) |

## OpenSquilla 0.5.0rc3

The 0.5.0 Preview 3 release records new human contributor work after the
0.5.0 Preview 2 release. It intentionally does not repeat contributors whose
new work is not present in this release range.

| Contributor | 0.5.0 Preview 3 contribution | Evidence |
| --- | --- | --- |
| [@ab2ence](https://github.com/ab2ence) | Fixed desktop gateway boot recovery so the packaged client can recover cleanly from failed startup. | [#491](https://github.com/opensquilla/opensquilla/pull/491) |
| [@JarvisPei](https://github.com/JarvisPei) | Corrected desktop OS-language resolution and added the opt-in Control UI background-music player. | [#550](https://github.com/opensquilla/opensquilla/pull/550), [#556](https://github.com/opensquilla/opensquilla/pull/556) |
| [@labulalala](https://github.com/labulalala) | Improved Windows source-installer PATH setup and added actionable shell guidance. | [#502](https://github.com/opensquilla/opensquilla/pull/502) |
| [@Liu-RK](https://github.com/Liu-RK) | Fixed Control UI token deep links and zero-output chat turns, and aligned sandbox file-access approvals. | [#486](https://github.com/opensquilla/opensquilla/pull/486), [#506](https://github.com/opensquilla/opensquilla/pull/506), [#526](https://github.com/opensquilla/opensquilla/pull/526) |
| [@lyteen](https://github.com/lyteen) | Contributed the original router self-learning work adopted and hardened for the opt-in feedback and retraining loop. | [#212](https://github.com/opensquilla/opensquilla/pull/212), [#511](https://github.com/opensquilla/opensquilla/pull/511) |
| [@nice-code-la](https://github.com/nice-code-la) | Added verified Squilla Router presets for coding providers. | [#560](https://github.com/opensquilla/opensquilla/pull/560) |
| [@TUOXI293](https://github.com/TUOXI293) | Improved chat scroll retention, code and skill-detail copy/inspection, compact tool traces, the Electron dark title bar, and Windows native-theme behavior. | [#487](https://github.com/opensquilla/opensquilla/pull/487), [#488](https://github.com/opensquilla/opensquilla/pull/488), [#509](https://github.com/opensquilla/opensquilla/pull/509), [#516](https://github.com/opensquilla/opensquilla/pull/516), [#524](https://github.com/opensquilla/opensquilla/pull/524), [#545](https://github.com/opensquilla/opensquilla/pull/545) |

## OpenSquilla 0.5.0rc2

The 0.5.0 Preview 2 release records new human contributor work after the
0.5.0 Preview 1 release. It intentionally does not repeat the earlier 0.5.0rc1
or 0.4.x contributor lists.

| Contributor | 0.5.0 Preview 2 contribution | Evidence |
| --- | --- | --- |
| [@HuaXiawithMoon](https://github.com/HuaXiawithMoon) | Kept `code-task` build scaffolding non-interactive by switching the runner-owned Electron/Vite scaffold to the package-supported skip flag. | [#473](https://github.com/opensquilla/opensquilla/pull/473) |

## OpenSquilla 0.5.0rc1

The 0.5.0 Preview 1 release records new human contributor work after the
0.4.1 release. It intentionally does not repeat the earlier 0.4.x contributor
lists.

| Contributor | 0.5.0 Preview 1 contribution | Evidence |
| --- | --- | --- |
| [@ab2ence](https://github.com/ab2ence) | Added drag-and-drop attachments, dynamic Model Ensemble routing, and ensemble timeout tuning. | [#388](https://github.com/opensquilla/opensquilla/pull/388), [`bc9ab2fe`](https://github.com/opensquilla/opensquilla/commit/bc9ab2fe), [#454](https://github.com/opensquilla/opensquilla/pull/454) |
| [@Liu-RK](https://github.com/Liu-RK) | Aligned sandbox run-mode authorization and approval behavior, then fixed managed execution host routing. | [#412](https://github.com/opensquilla/opensquilla/pull/412), [#450](https://github.com/opensquilla/opensquilla/pull/450) |
| [@TUOXI293](https://github.com/TUOXI293) | Added image preview navigation. | [#447](https://github.com/opensquilla/opensquilla/pull/447) |
| [@tqangxl](https://github.com/tqangxl) | Improved gateway lifecycle conflict diagnostics and promoted SQLAlchemy to a core dependency. | [`1fede3ea`](https://github.com/opensquilla/opensquilla/commit/1fede3ea), [`eb6776f2`](https://github.com/opensquilla/opensquilla/commit/eb6776f2) |
| [@HuaXiawithMoon](https://github.com/HuaXiawithMoon) | Fixed WeCom AI Bot websocket mode. | [`94e4b1c1`](https://github.com/opensquilla/opensquilla/commit/94e4b1c1) |

## OpenSquilla 0.4.1

The 0.4.1 release records new human contributor work after the 0.4.0
attribution ledger was merged. It intentionally does not repeat the larger
0.4.0 contributor list.

| Contributor | 0.4.1 contribution | Evidence |
| --- | --- | --- |
| [@ab2ence](https://github.com/ab2ence) | Hardened install telemetry so CI and test environments are not counted as installs, and allowed desktop HTML artifacts to open natively. | [#348](https://github.com/opensquilla/opensquilla/pull/348), [#355](https://github.com/opensquilla/opensquilla/pull/355) |

## OpenSquilla 0.4.0

The 0.4.0 release is prepared from current `dev` after `v0.3.1`. This section
records non-Open-Squilla contributor work with pull-request evidence in that
range. Some work was replayed or carried through Open-Squilla integration pull
requests; those rows name the original contributor and cite both the original
pull request and the integration pull request when useful.

| Contributor | 0.4.0 contribution | Evidence |
| --- | --- | --- |
| [@ab2ence](https://github.com/ab2ence) | Control UI migration and stabilization work, share-image export, Web Chat slash-input handling, bundled AwesomeWebpage MetaSkill work, the Coding mode toggle, and desktop gateway startup plus install telemetry hardening carried into `dev`. | [#264](https://github.com/opensquilla/opensquilla/pull/264), [#274](https://github.com/opensquilla/opensquilla/pull/274), [#177](https://github.com/opensquilla/opensquilla/pull/177), [#173](https://github.com/opensquilla/opensquilla/pull/173), [#313](https://github.com/opensquilla/opensquilla/pull/313), [#320](https://github.com/opensquilla/opensquilla/pull/320) |
| [@myz-ah](https://github.com/myz-ah) | Added the `code-task` workflow for isolated, runner-verified code changes behind Coding mode and improved Web UI LaTeX formula rendering. | [#311](https://github.com/opensquilla/opensquilla/pull/311), [#318](https://github.com/opensquilla/opensquilla/pull/318) |
| [@nice-code-la](https://github.com/nice-code-la) | Skills readiness in the Web UI, MetaSkill progress and clarification UX, manual-only `/meta` behavior, scoped MetaSkill run-history reads, router fallback/default refresh work, image follow-up routing gates, from-scratch `code-task` build support, and MetaSkill clarify resume feedback. | [#184](https://github.com/opensquilla/opensquilla/pull/184), [#222](https://github.com/opensquilla/opensquilla/pull/222), [#243](https://github.com/opensquilla/opensquilla/pull/243), [#253](https://github.com/opensquilla/opensquilla/pull/253), [#261](https://github.com/opensquilla/opensquilla/pull/261) carried through [#297](https://github.com/opensquilla/opensquilla/pull/297), [#272](https://github.com/opensquilla/opensquilla/pull/272), [#279](https://github.com/opensquilla/opensquilla/pull/279) carried through [#297](https://github.com/opensquilla/opensquilla/pull/297), [#321](https://github.com/opensquilla/opensquilla/pull/321), [#323](https://github.com/opensquilla/opensquilla/pull/323) |
| [@openvictory](https://github.com/openvictory) | Skill API-key resolution fallback plus MetaSkill run-history and rescue-action Control UI work carried through the session-contract Control UI integration. | [#183](https://github.com/opensquilla/opensquilla/pull/183), [#264](https://github.com/opensquilla/opensquilla/pull/264) |
| [@Liu-RK](https://github.com/Liu-RK) | Overhauled sandbox run modes and managed access controls, then refactored sandbox run modes across Windows and Linux. | [#189](https://github.com/opensquilla/opensquilla/pull/189), [#230](https://github.com/opensquilla/opensquilla/pull/230) |
| [@weiconghe](https://github.com/weiconghe) | Preserved and replayed Gemini `thought_signature` metadata across provider tool-call turns. | [#312](https://github.com/opensquilla/opensquilla/pull/312) |
| [@changquanyou](https://github.com/changquanyou) | Accepted no-space SSE `data:` lines and improved managed-layer MetaSkill inspection. | [#214](https://github.com/opensquilla/opensquilla/pull/214) |
| [@nkgotcode](https://github.com/nkgotcode) | Fixed DOCX `skill_exec` export behavior. | [#262](https://github.com/opensquilla/opensquilla/pull/262) |
| [@C1-BA-B1-F3](https://github.com/C1-BA-B1-F3) | Made SSRF fake-IP DNS blocks actionable for operators. | [#298](https://github.com/opensquilla/opensquilla/pull/298) carried through [#309](https://github.com/opensquilla/opensquilla/pull/309) and [#310](https://github.com/opensquilla/opensquilla/pull/310) |
| [@BlueOcean223](https://github.com/BlueOcean223) | Reset TUI EOF state on cached reentry. | [#203](https://github.com/opensquilla/opensquilla/pull/203) |
| [@szdtzpj](https://github.com/szdtzpj) | Fixed environment test precedence and the TUI abort hook. | [#176](https://github.com/opensquilla/opensquilla/pull/176) |
| [@lose4578](https://github.com/lose4578) | Submitted the OpenTUI scrollback-native frontend work carried into the 0.4.0 preview backend. | [#182](https://github.com/opensquilla/opensquilla/pull/182) carried through [#277](https://github.com/opensquilla/opensquilla/pull/277) |
| cwan0785 (commit author name; GitHub account: [@Anonymous-4427](https://github.com/Anonymous-4427)) | Authored OpenTUI preview backend implementation commits carried into the 0.4.0 preview backend. | [#182](https://github.com/opensquilla/opensquilla/pull/182) carried through [#277](https://github.com/opensquilla/opensquilla/pull/277) |

## OpenSquilla 0.3.1

The 0.3.1 release is prepared as a release-surface replay from `dev` onto the
stable `main` release ledger. Some community work in the release window was
already represented by earlier `main` attribution work; this section records
the 0.3.1-specific community contributions acknowledged in the release notes.

| Contributor | 0.3.1 contribution | Evidence |
| --- | --- | --- |
| [@openvictory](https://github.com/openvictory) | Visible running-state feedback plus short-drama and media helper workflows. | [#123](https://github.com/opensquilla/opensquilla/pull/123), [#133](https://github.com/opensquilla/opensquilla/pull/133), [#137](https://github.com/opensquilla/opensquilla/pull/137) |
| [@freeaccount-create](https://github.com/freeaccount-create) | Slack Socket Mode and self-targeting replies for channel workflows. | [#142](https://github.com/opensquilla/opensquilla/pull/142) |
| [@ruhook](https://github.com/ruhook) | Submitted the WebChat user-message newline preservation pull request. | [#124](https://github.com/opensquilla/opensquilla/pull/124) |
| [@qq712696307](https://github.com/qq712696307) | Authored the commit in #124 that preserved user-message newlines in WebChat. | [#124](https://github.com/opensquilla/opensquilla/pull/124) |
| [@Cola-Alex](https://github.com/Cola-Alex) | Increased tokenjuice summarize and failure-context windows for fallback tool-result projection. | [#143](https://github.com/opensquilla/opensquilla/pull/143) |
| [@nice-code-la](https://github.com/nice-code-la) | Voice workflow usability and clarification-pause resume behavior. | [#165](https://github.com/opensquilla/opensquilla/pull/165), [#166](https://github.com/opensquilla/opensquilla/pull/166) |

## OpenSquilla 0.3.0

The 0.3.0 release reached `main` through release synchronization after work had
landed through `dev` and integration branches. That compressed the default
branch commit history, so the following community contributions are recorded
explicitly here.

| Contributor | 0.3.0 contribution | Evidence |
| --- | --- | --- |
| [@ab2ence](https://github.com/ab2ence) | Tokenjuice tool-result compression and canonical projection, memory dream consolidation, chat streaming restore work, and cross-platform CI hardening. | [#56](https://github.com/opensquilla/opensquilla/pull/56), [#61](https://github.com/opensquilla/opensquilla/pull/61), [#81](https://github.com/opensquilla/opensquilla/pull/81), [#88](https://github.com/opensquilla/opensquilla/pull/88), [#109](https://github.com/opensquilla/opensquilla/pull/109) |
| [@lose4578](https://github.com/lose4578) | Submitted the TUI backend/runtime foundation pull request. | [#80](https://github.com/opensquilla/opensquilla/pull/80) |
| cwan0785 (commit author name; GitHub account: [@Anonymous-4427](https://github.com/Anonymous-4427)) | Authored the TUI backend/runtime extraction commits behind the foundation pull request. | [#80](https://github.com/opensquilla/opensquilla/pull/80) |
| [@nice-code-la](https://github.com/nice-code-la) | MetaSkill orchestration, router-control replay and hold behavior, retained high-value MetaSkill routing, lifestyle MetaSkill cleanup, and live MetaSkill execution hardening. | [#82](https://github.com/opensquilla/opensquilla/pull/82), [#93](https://github.com/opensquilla/opensquilla/pull/93), [#96](https://github.com/opensquilla/opensquilla/pull/96), [#110](https://github.com/opensquilla/opensquilla/pull/110), [#114](https://github.com/opensquilla/opensquilla/pull/114) replayed through [#115](https://github.com/opensquilla/opensquilla/pull/115), [#119](https://github.com/opensquilla/opensquilla/pull/119) |
| [@openvictory](https://github.com/openvictory) | UTF-8 migration loading fix for yoyo migrations on Windows locales, plus follow-up release-gate alignment. | [#116](https://github.com/opensquilla/opensquilla/pull/116) |

## Attribution Practice

When maintainer cleanup, replay, or squash merging collapses contributor
commits, the final non-empty commit should preserve each human contributor with
`Co-authored-by:` trailers that use GitHub-associated email addresses. Preserve
pull request author attribution and commit author attribution separately when
they differ.
