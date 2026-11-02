# Dependency security refresh: 2026-09-17

Baseline: `7ac8b378d48f022839f9bd06db99bb3d1f023e0e`. All 101 original alert ranges are excluded by the updated lockfiles. The full dependency audit additionally identified and repaired AJV, Electron, Click, and lxml-html-clean findings.

This table records version-range verification; runtime reachability is not assumed. GitHub alert closure is checked after merge. Complete lock audits cover Python optional/platform variants and npm development dependencies.

WeasyPrint alert #40 has no patched-version metadata in GitHub, but upstream v69.0 explicitly fixes CVE-2026-49452; this change selects v70.0. Reference: https://github.com/Kozea/WeasyPrint/releases/tag/v69.0

| Alert | Advisory | Package | Dependency manifest | Severity | Before | After | Range check |
|---|---|---|---|---|---|---|---|
| #1 | GHSA-pp6c-gr5w-3c5g | python-multipart | `uv.lock` | high | 0.0.26 | 0.0.31 | outside affected range |
| #2 | GHSA-qccp-gfcp-xxvc | urllib3 | `uv.lock` | high | 2.6.3 | 2.7.0 | outside affected range |
| #3 | GHSA-mf9v-mfxr-j63j | urllib3 | `uv.lock` | high | 2.6.3 | 2.7.0 | outside affected range |
| #4 | GHSA-65pc-fj4g-8rjx | idna | `uv.lock` | medium | 3.11 | 3.15 | outside affected range |
| #5 | GHSA-jg22-mg44-37j8 | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #6 | GHSA-hg6j-4rv6-33pg | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #7 | GHSA-86qp-5c8j-p5mr | starlette | `uv.lock` | medium | 1.0.0 | 1.3.1 | outside affected range |
| #8 | GHSA-cj93-chg6-vgv8 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #9 | GHSA-248m-82v9-q6g6 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #15 | GHSA-m6qw-4cw2-hm4m | aiohttp | `uv.lock` | low | 3.13.5 | 3.14.3 | outside affected range |
| #16 | GHSA-2fqr-mr3j-6wp8 | aiohttp | `uv.lock` | low | 3.13.5 | 3.14.3 | outside affected range |
| #17 | GHSA-hpj7-wq8m-9hgp | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #18 | GHSA-63hw-fmq6-xxg2 | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #19 | GHSA-g3cq-j2xw-wf74 | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #20 | GHSA-4fvr-rgm6-gqmc | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #21 | GHSA-9x8q-7h8h-wcw9 | aiohttp | `uv.lock` | low | 3.13.5 | 3.14.3 | outside affected range |
| #22 | GHSA-4m7w-qmgq-4wj5 | aiohttp | `uv.lock` | low | 3.13.5 | 3.14.3 | outside affected range |
| #23 | GHSA-xcgm-r5h9-7989 | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #24 | GHSA-537c-gmf6-5ccf | cryptography | `uv.lock` | high | 46.0.7 | 50.0.0 | outside affected range |
| #25 | GHSA-x746-7m8f-x49c | starlette | `uv.lock` | medium | 1.0.0 | 1.3.1 | outside affected range |
| #26 | GHSA-wqp7-x3pw-xc5r | starlette | `uv.lock` | high | 1.0.0 | 1.3.1 | outside affected range |
| #27 | GHSA-vffw-93wf-4j4q | python-multipart | `uv.lock` | low | 0.0.26 | 0.0.31 | outside affected range |
| #28 | GHSA-6jv3-5f52-599m | python-multipart | `uv.lock` | low | 0.0.26 | 0.0.31 | outside affected range |
| #29 | GHSA-v9pg-7xvm-68hf | python-multipart | `uv.lock` | low | 0.0.26 | 0.0.31 | outside affected range |
| #30 | GHSA-5rvq-cxj2-64vf | python-multipart | `uv.lock` | high | 0.0.26 | 0.0.31 | outside affected range |
| #31 | GHSA-jp82-jpqv-5vv3 | Starlette | `uv.lock` | low | 1.0.0 | 1.3.1 | outside affected range |
| #32 | GHSA-82w8-qh3p-5jfq | starlette | `uv.lock` | high | 1.0.0 | 1.3.1 | outside affected range |
| #33 | GHSA-wjqc-6w8f-h24c | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #34 | GHSA-5hgr-hg42-57jg | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #35 | GHSA-j543-4vmf-qm7v | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #36 | GHSA-52x6-gq3r-vpf4 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #37 | GHSA-m2v9-299j-rv96 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #38 | GHSA-jm82-fx9c-mx94 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #39 | GHSA-4xgf-cpjx-pc3j | pydantic-settings | `uv.lock` | medium | 2.13.1 | 2.14.2 | outside affected range |
| #40 | GHSA-jhhc-3hcp-qhm5 | weasyprint | `uv.lock` | medium | 68.1 | 70.0 | outside affected range |
| #41 | GHSA-2wc2-fm75-p42x | soupsieve | `uv.lock` | high | 2.8.3 | 2.8.4 | outside affected range |
| #42 | GHSA-836r-79rf-4m37 | soupsieve | `uv.lock` | high | 2.8.3 | 2.8.4 | outside affected range |
| #43 | GHSA-g9xf-7f8q-9mcj | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #44 | GHSA-hvrp-rf83-w775 | mcp | `uv.lock` | high | 1.27.0 | 1.28.1 | outside affected range |
| #45 | GHSA-jpw9-pfvf-9f58 | mcp | `uv.lock` | high | 1.27.0 | 1.28.1 | outside affected range |
| #46 | GHSA-vj7q-gjh5-988w | mcp | `uv.lock` | high | 1.27.0 | 1.28.1 | outside affected range |
| #47 | GHSA-pg7v-jwj7-p798 | pillow | `uv.lock` | medium | 12.2.0 | 12.3.0 | outside affected range |
| #57 | GHSA-62p4-gmf7-7g93 | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #58 | GHSA-8v84-f9pq-wr9x | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #59 | GHSA-5x94-69rx-g8h2 | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #60 | GHSA-45hq-cxwh-f6vc | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #61 | GHSA-phj9-mv4w-65pm | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #62 | GHSA-4x4j-2g7c-83w6 | Pillow | `uv.lock` | medium | 12.2.0 | 12.3.0 | outside affected range |
| #63 | GHSA-xj96-63gp-2gmr | Pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #64 | GHSA-fj7v-r99m-22gq | Pillow | `uv.lock` | medium | 12.2.0 | 12.3.0 | outside affected range |
| #65 | GHSA-6r8x-57c9-28j4 | Pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #66 | GHSA-jjj6-mw9f-p565 | Pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #67 | GHSA-vjc4-5qp5-m44j | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #68 | GHSA-9hw9-ch79-4vh6 | pillow | `uv.lock` | high | 12.2.0 | 12.3.0 | outside affected range |
| #74 | GHSA-5qjq-93h5-hrgp | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #75 | GHSA-55h5-xmcq-c37v | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #76 | GHSA-5xf7-4p34-54qr | pypdf | `uv.lock` | high | 6.10.2 | 6.16.1 | outside affected range |
| #77 | GHSA-g867-7843-wf8q | pypdf | `uv.lock` | high | 6.10.2 | 6.16.1 | outside affected range |
| #94 | GHSA-8xcm-r25x-g524 | undici | `desktop/electron/package-lock.json` | medium | 6.28.0, 7.28.0 | 6.28.1, 7.29.1 | outside affected range |
| #95 | GHSA-4cwx-7wf7-3272 | undici | `desktop/electron/package-lock.json` | high | 6.28.0, 7.28.0 | 6.28.1, 7.29.1 | outside affected range |
| #96 | GHSA-jr45-8vmc-qm54 | undici | `desktop/electron/package-lock.json` | medium | 6.28.0, 7.28.0 | 6.28.1, 7.29.1 | outside affected range |
| #97 | GHSA-v3r7-h72x-cjcm | undici | `desktop/electron/package-lock.json` | medium | 6.28.0, 7.28.0 | 6.28.1, 7.29.1 | outside affected range |
| #98 | GHSA-m8rv-5g2x-5cg5 | undici | `desktop/electron/package-lock.json` | medium | 6.28.0, 7.28.0 | 6.28.1, 7.29.1 | outside affected range |
| #103 | GHSA-mq44-7p77-q5h7 | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #104 | GHSA-mfx4-hv73-q22v | aiohttp | `uv.lock` | medium | 3.13.5 | 3.14.3 | outside affected range |
| #105 | GHSA-cq5v-8q36-5273 | aiohttp | `uv.lock` | high | 3.13.5 | 3.14.3 | outside affected range |
| #106 | GHSA-g6cj-pr64-35w5 | cryptography | `uv.lock` | high | 46.0.7 | 50.0.0 | outside affected range |
| #107 | GHSA-7p8r-x3mc-p8w7 | fast-uri | `desktop/electron/package-lock.json` | high | 3.1.4 | 3.1.8 | outside affected range |
| #111 | GHSA-5p4m-2wfm-xmqj | js-yaml | `desktop/electron/package-lock.json` | high | 4.3.0 | 4.3.2 | outside affected range |
| #112 | GHSA-6hr6-w5qg-qmwg | h2 | `uv.lock` | medium | 4.3.0 | 4.4.1 | outside affected range |
| #113 | GHSA-55q2-fjhq-7xh7 | dompurify | `opensquilla-webui/package-lock.json` | medium | 3.4.12 | 3.4.13 | outside affected range |
| #121 | GHSA-fwg2-594c-jp42 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #122 | GHSA-fp3f-mc75-235c | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #123 | GHSA-2v37-7h3g-55p8 | nanoid | `opensquilla-webui/package-lock.json` | high | 3.3.16 | 3.3.19 | outside affected range |
| #124 | GHSA-3496-9g83-7v6x | sqlparse | `uv.lock` | medium | 0.5.5 | 0.6.0 | outside affected range |
| #125 | GHSA-f2ff-p2ww-7p4p | sqlparse | `uv.lock` | high | 0.5.5 | 0.6.0 | outside affected range |
| #126 | GHSA-pwgv-4x5q-6m9f | sqlparse | `uv.lock` | high | 0.5.5 | 0.6.0 | outside affected range |
| #127 | GHSA-prg7-hcfm-mfcr | sqlparse | `uv.lock` | high | 0.5.5 | 0.6.0 | outside affected range |
| #131 | GHSA-jp53-mhqp-8xcg | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #132 | GHSA-763m-79hh-57f2 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #133 | GHSA-23w6-3w8w-8484 | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #134 | GHSA-cfqr-cjx5-5jcm | sqlparse | `uv.lock` | medium | 0.5.5 | 0.6.0 | outside affected range |
| #135 | GHSA-fc8x-2rww-xw9m | pypdf | `uv.lock` | medium | 6.10.2 | 6.16.1 | outside affected range |
| #136 | GHSA-6gmq-8vp8-gcm6 | @xmldom/xmldom | `desktop/electron/package-lock.json` | medium | 0.8.13 | 0.8.15 | outside affected range |
| #137 | GHSA-jqff-g426-hqxp | fast-uri | `desktop/electron/package-lock.json` | high | 3.1.4 | 3.1.8 | outside affected range |
| #138 | GHSA-fph4-wmhf-6fwf | fast-uri | `desktop/electron/package-lock.json` | high | 3.1.4 | 3.1.8 | outside affected range |
| #139 | GHSA-f65p-4m7j-42xc | fast-uri | `desktop/electron/package-lock.json` | high | 3.1.4 | 3.1.8 | outside affected range |
| #140 | GHSA-5jgf-p345-68v8 | fast-uri | `desktop/electron/package-lock.json` | high | 3.1.4 | 3.1.8 | outside affected range |
| #141 | GHSA-m2h6-j472-rp4c | cryptography | `uv.lock` | medium | 46.0.7 | 50.0.0 | outside affected range |
| #142 | GHSA-jwv3-5hgf-82ww | cryptography | `uv.lock` | high | 46.0.7 | 50.0.0 | outside affected range |
| #143 | GHSA-4w3w-2rp5-g8jm | @xmldom/xmldom | `desktop/electron/package-lock.json` | high | 0.8.13 | 0.8.15 | outside affected range |
| #144 | GHSA-w2rr-34g9-rvrj | @xmldom/xmldom | `desktop/electron/package-lock.json` | high | 0.8.13 | 0.8.15 | outside affected range |
| #145 | GHSA-93r5-fhx6-vmg9 | @xmldom/xmldom | `desktop/electron/package-lock.json` | high | 0.8.13 | 0.8.15 | outside affected range |
| #148 | GHSA-8344-3jmq-59r6 | @xmldom/xmldom | `desktop/electron/package-lock.json` | high | 0.8.13 | 0.8.15 | outside affected range |
| #149 | GHSA-6h8r-xr42-gp59 | @xmldom/xmldom | `desktop/electron/package-lock.json` | medium | 0.8.13 | 0.8.15 | outside affected range |
| #150 | GHSA-27p8-2357-5qqv | @xmldom/xmldom | `desktop/electron/package-lock.json` | high | 0.8.13 | 0.8.15 | outside affected range |
| #151 | GHSA-c7q8-3ch8-vqpv | @xmldom/xmldom | `desktop/electron/package-lock.json` | high | 0.8.13 | 0.8.15 | outside affected range |
| #152 | GHSA-2883-xcg3-v3hh | js-yaml | `desktop/electron/package-lock.json` | high | 4.3.0 | 4.3.2 | outside affected range |
| #153 | GHSA-82fw-gwwq-j7x9 | @vitest/mocker | `opensquilla-webui/package-lock.json` | medium | 4.1.9 | 4.1.11 | outside affected range |
| #154 | GHSA-82fw-gwwq-j7x9 | vitest | `opensquilla-webui/package-lock.json` | medium | 4.1.9 | 4.1.11 | outside affected range |
| #155 | GHSA-jf6q-chmf-3h3v | weasyprint | `uv.lock` | medium | 68.1 | 70.0 | outside affected range |
