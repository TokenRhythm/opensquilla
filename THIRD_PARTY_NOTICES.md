# Third Party Notices

This file records third-party attribution for assets bundled with OpenSquilla.
It covers:

- The bundled skill descriptors under `src/opensquilla/skills/bundled/`, which
  include OpenClaw-derived MIT descriptors and OpenSquilla-original descriptors.
- The bundled pptx skill references the python-pptx and PptxGenJS libraries;
  OpenSquilla does not vendor those libraries, but the skill instructs the
  agent runtime to invoke them and is documented here for transparency.
- The local SquillaRouter V4 Phase 3 model bundle under
  `src/opensquilla/squilla_router/models/v4.2_phase3_inference/`.
- Selected Python runtime dependencies distributed with the Gateway.
- Web UI runtime dependencies, bundled fonts, and generated Control UI assets.
- The Web UI "Arctic" theme color palette, adapted from the Nord palette under
  the MIT license; see the dedicated section below.
- The built-in tokenjuice tool-result projection backend and bundled
  reduction rules under `src/opensquilla/plugins/tokenjuice/`.
- Optional managed toolchains downloaded after explicit user confirmation;
  these binaries are not embedded in the OpenSquilla wheel or source tree.
- The cron prompt-injection scanner was reviewed against Hermes Agent
  reference material; the MIT notice is reproduced below for conservative
  attribution.

## OpenAI-compatible empty tool-call tails

The handling of absent, null, and empty tool-call deltas in compatible terminal
frames follows the no-op semantics reviewed in
[Pi's OpenAI Completions adapter](https://github.com/earendil-works/pi/blob/4c8eb393c73220c742e75745df210335aedc020e/packages/ai/src/api/openai-completions.ts).
This behavior is adapted using OpenSquilla's existing tool-payload validator;
no upstream source or fixtures are bundled, and OpenSquilla retains its stricter
post-terminal state checks. Pi is distributed under the MIT license:

```text
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Inline Visualize frame runtime

The Web UI bundles `generative-frame` 0.0.5 and its `safe-content-frame` 0.0.34
dependency from [assistant-ui](https://github.com/assistant-ui/assistant-ui),
maintained by AgentbaseAI Inc. Both packages are MIT licensed. Their identical
license text, reproduced from the installed packages' LICENSE files, follows.
OpenSquilla owns the Vue host, restricted bridge, Artifact integration and
Visualize host contract. The skill's generated `references/guidance-*.txt` files
derive from the MIT-licensed `generative-frame` 0.0.5 model guidance and preserve
its copyright notice. `upstream-guidance.json` records the exact source and
compiled-input hashes, generation configuration, and local adaptations; the
full MIT license is distributed beside the skill as `LICENSE.generative-frame.txt`.
The OpenSquilla host contract and three optional examples are original content.
They do not incorporate Codex skill text or claim to reproduce Codex internals.

```text
MIT License

Copyright (c) 2026 AgentbaseAI Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Interactive terminal

The Web UI bundles [`@xterm/xterm`](https://github.com/xtermjs/xterm.js) 6.0.0
and [`@xterm/addon-fit`](https://github.com/xtermjs/xterm.js/tree/master/addons/addon-fit)
0.11.0 for the Workbench terminal. Both packages are MIT licensed; their
upstream notices are reproduced below.

### @xterm/xterm 6.0.0

```text
Copyright (c) 2017-2019, The xterm.js authors (https://github.com/xtermjs/xterm.js)
Copyright (c) 2014-2016, SourceLair Private Company (https://www.sourcelair.com)
Copyright (c) 2012-2013, Christopher Jeffrey (https://github.com/chjj/)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### @xterm/addon-fit 0.11.0

```text
Copyright (c) 2019, The xterm.js authors (https://github.com/xtermjs/xterm.js)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

## Markdown diagrams and rich rendering

The Web UI dynamically bundles [Mermaid](https://github.com/mermaid-js/mermaid)
11.17.2 (npm package `mermaid`). Diagram interaction and sequence-diagram
compatibility helpers were adapted from [browsa](https://github.com/xiaohuzai/browsa),
including its preview height estimates and semicolon retry behavior. OpenSquilla owns the Vue
lifecycle, resource policy, and shared rendering integration. Both upstreams
are MIT licensed; their notices are retained here.

```text
The MIT License (MIT)

Mermaid: Copyright (c) 2014 - 2022 Knut Sveidqvist
browsa: Copyright (c) 2026 browsa contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Mermaid source HTML parsing

The Web UI bundles [`parse5`](https://github.com/inikulin/parse5) 8.0.1 to inspect
Mermaid source as a plain HTML syntax tree without constructing browser DOM.
Its [entities](https://github.com/fb55/entities) dependency is locked to 8.1.0.
The following notices reproduce the license files from those npm packages.

### parse5 8.0.1 (MIT)

```text
Copyright (c) 2013-2019 Ivan Nikulin (ifaaan@gmail.com, https://github.com/inikulin)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

### entities 8.1.0 (BSD-2-Clause)

```text
Copyright (c) Felix Böhm
All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

THIS IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS,
EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Python runtime dependencies

SQLAlchemy defines the Gateway's persisted model types, and websockets supports
channel and Gateway connections. The following notices reproduce the license
files shipped in the exact upstream wheels selected by `uv.lock`.

### SQLAlchemy 2.0.53

Source: [SQLAlchemy 2.0.53 on PyPI](https://pypi.org/project/SQLAlchemy/2.0.53/).
The wheel includes `sqlalchemy-2.0.53.dist-info/licenses/LICENSE` and `AUTHORS`;
the license refers to the authors list reproduced below.

```text
Copyright 2005-2026 SQLAlchemy authors and contributors <see AUTHORS file>.

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

```text
SQLAlchemy was created by Michael Bayer.

Major contributing authors include:

- Mike Bayer
- Jason Kirtland
- Michael Trier
- Diana Clarke
- Gaetan de Menten
- Lele Gaifax
- Jonathan Ellis
- Gord Thompson
- Federico Caselli
- Philip Jenvey
- Rick Morrison
- Chris Withers
- Ants Aasma
- Sheila Allen
- Paul Johnston
- Tony Locke
- Hajime Nakagami
- Vraj Mohan
- Robert Leftwich
- Taavi Burns
- Jonathan Vanasco
- Jeff Widman
- Scott Dugas
- Dobes Vandermeer
- Ville Skytta
- Rodrigo Menezes
```

### websockets 17.1

Source: [websockets 17.1 on PyPI](https://pypi.org/project/websockets/17.1/).
The following BSD-3-Clause text comes from the wheel's
`websockets-17.1.dist-info/licenses/LICENSE`.

```text
Copyright (c) Aymeric Augustin and contributors

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of the copyright holder nor the names of its contributors
      may be used to endorse or promote products derived from this software
      without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Web UI dependencies and bundled fonts

### Office preview renderer and offline Simplified Chinese fallback fonts

The Web UI lazily loads `@silurus/ooxml` **0.88.0**, an MIT-licensed read-only
DOCX/XLSX/PPTX renderer, copyright (c) 2026 Yuki Yokotani. Its precompiled
WebAssembly parsers and JavaScript assets are distributed with the generated
Control UI. The complete upstream MIT license and `THIRD_PARTY_NOTICES.md`
(including Rust/WASM dependencies, Unicode data, and optional MathJax/STIX
attribution) are preserved in
`opensquilla-webui/public-assets/assets/office-preview-licenses/` and copied
unchanged into release Web UI assets. No separate Silurus MCP binary is bundled.

Chinese fallback faces are the unmodified **Noto Sans SC** and **Noto Serif SC**
region subsets, Regular and Bold, from `notofonts/noto-cjk` commit
`f8d157532fbfaeda587e826d4cd5b21a49186f7c`. Each is distributed under the SIL Open
Font License 1.1; the original family-specific license/copyright notices are
preserved alongside the Silurus notices. Fonts are prepared at build time with
immutable source URLs and pinned hashes from
`opensquilla-webui/scripts/office-preview-fonts.json`, then emitted as local
assets. The browser does not download these fonts from Google Fonts, and the
fonts are not installed into the host operating system.

The generated assets retain the names and licenses of their unmodified font
sources. These fallback faces do not claim metric identity with Microsoft
fonts. Changes to the Silurus version, optional runtime assets, or font sources
must update these notices and the build-time asset inventory together.

### Viz.js, Graphviz, and Expat diagram renderer

The Web UI renders DOT diagrams in a dedicated Worker using the unmodified
`@viz-js/viz` **3.31.0** npm package. Its JavaScript wrapper is MIT licensed,
copyright (c) Michael Daines. The package also contains **Graphviz 16.1.0**
(EPL-2.0) and **Expat 2.8.5** (MIT) in WebAssembly object-code form. These
components are emitted into generated Web UI assets; no native Graphviz
executable is bundled by this integration.

Upstream package: <https://github.com/mdaines/viz-js>. The npm release's
`gitHead` is `c8ce050d28eb0dc1ed1629ea32ab7f3dc117db0f`; its
[MIT license](https://github.com/mdaines/viz-js/blob/c8ce050d28eb0dc1ed1629ea32ab7f3dc117db0f/LICENSE)
and [backend build recipe](https://github.com/mdaines/viz-js/blob/c8ce050d28eb0dc1ed1629ea32ab7f3dc117db0f/packages/viz/backend/Dockerfile)
are available at that revision. The installed package's `lib/metadata.js` and
`lib/provenance.json` record the Graphviz version and the exact source archives
used by the upstream build:

| Component | Source archive | SHA-256 | License |
|---|---|---|---|
| Graphviz 16.1.0 | [graphviz-16.1.0.tar.gz](https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/16.1.0/graphviz-16.1.0.tar.gz) | `beea483ab130f456c1c3905f4f2e40778a9c493c3d73ae8012367d552d71ca84` | [EPL-2.0](https://gitlab.com/graphviz/graphviz/-/blob/16.1.0/COPYING) |
| Expat 2.8.5 | [expat-2.8.5.tar.gz](https://github.com/libexpat/libexpat/releases/download/R_2_8_5/expat-2.8.5.tar.gz) | `920dde485e15eda0cce8d2310b41d492c534e5e3d89ad407a0b4176dd2ff88fe` | [MIT](https://github.com/libexpat/libexpat/blob/R_2_8_5/COPYING) |

Graphviz source code is available under the Eclipse Public License 2.0 from
the source archive above and the [16.1.0 source tree](https://gitlab.com/graphviz/graphviz/-/tree/16.1.0).
OpenSquilla does not modify the upstream Viz.js, Graphviz, or Expat sources.
Graphviz's file-specific copyright notices are retained in that source tree;
its [GVC entry point](https://gitlab.com/graphviz/graphviz/-/blob/16.1.0/lib/gvc/gvc.c)
carries the following notice, and its [AUTHORS file](https://gitlab.com/graphviz/graphviz/-/blob/16.1.0/AUTHORS)
identifies the upstream contributors:

```text
Copyright (c) 2011 AT&T Intellectual Property
All rights reserved. This program and the accompanying materials
are made available under the terms of the Eclipse Public License v2.0
which accompanies this distribution, and is available at
https://www.eclipse.org/org/documents/epl-2.0/EPL-2.0.html

Contributors: Details at https://graphviz.org
```

The complete upstream license texts follow. Keep these notices and source
references with any distribution of the generated diagram-rendering assets.

#### Viz.js MIT license

```text
MIT License

Copyright (c) Michael Daines

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

#### Graphviz Eclipse Public License 2.0

```text
Eclipse Public License - v 2.0

    THE ACCOMPANYING PROGRAM IS PROVIDED UNDER THE TERMS OF THIS ECLIPSE
    PUBLIC LICENSE ("AGREEMENT"). ANY USE, REPRODUCTION OR DISTRIBUTION
    OF THE PROGRAM CONSTITUTES RECIPIENT'S ACCEPTANCE OF THIS AGREEMENT.

1. DEFINITIONS

"Contribution" means:

  a) in the case of the initial Contributor, the initial content
     Distributed under this Agreement, and

  b) in the case of each subsequent Contributor:
     i) changes to the Program, and
     ii) additions to the Program;
  where such changes and/or additions to the Program originate from
  and are Distributed by that particular Contributor. A Contribution
  "originates" from a Contributor if it was added to the Program by
  such Contributor itself or anyone acting on such Contributor's behalf.
  Contributions do not include changes or additions to the Program that
  are not Modified Works.

"Contributor" means any person or entity that Distributes the Program.

"Licensed Patents" mean patent claims licensable by a Contributor which
are necessarily infringed by the use or sale of its Contribution alone
or when combined with the Program.

"Program" means the Contributions Distributed in accordance with this
Agreement.

"Recipient" means anyone who receives the Program under this Agreement
or any Secondary License (as applicable), including Contributors.

"Derivative Works" shall mean any work, whether in Source Code or other
form, that is based on (or derived from) the Program and for which the
editorial revisions, annotations, elaborations, or other modifications
represent, as a whole, an original work of authorship.

"Modified Works" shall mean any work in Source Code or other form that
results from an addition to, deletion from, or modification of the
contents of the Program, including, for purposes of clarity any new file
in Source Code form that contains any contents of the Program. Modified
Works shall not include works that contain only declarations,
interfaces, types, classes, structures, or files of the Program solely
in each case in order to link to, bind by name, or subclass the Program
or Modified Works thereof.

"Distribute" means the acts of a) distributing or b) making available
in any manner that enables the transfer of a copy.

"Source Code" means the form of a Program preferred for making
modifications, including but not limited to software source code,
documentation source, and configuration files.

"Secondary License" means either the GNU General Public License,
Version 2.0, or any later versions of that license, including any
exceptions or additional permissions as identified by the initial
Contributor.

2. GRANT OF RIGHTS

  a) Subject to the terms of this Agreement, each Contributor hereby
  grants Recipient a non-exclusive, worldwide, royalty-free copyright
  license to reproduce, prepare Derivative Works of, publicly display,
  publicly perform, Distribute and sublicense the Contribution of such
  Contributor, if any, and such Derivative Works.

  b) Subject to the terms of this Agreement, each Contributor hereby
  grants Recipient a non-exclusive, worldwide, royalty-free patent
  license under Licensed Patents to make, use, sell, offer to sell,
  import and otherwise transfer the Contribution of such Contributor,
  if any, in Source Code or other form. This patent license shall
  apply to the combination of the Contribution and the Program if, at
  the time the Contribution is added by the Contributor, such addition
  of the Contribution causes such combination to be covered by the
  Licensed Patents. The patent license shall not apply to any other
  combinations which include the Contribution. No hardware per se is
  licensed hereunder.

  c) Recipient understands that although each Contributor grants the
  licenses to its Contributions set forth herein, no assurances are
  provided by any Contributor that the Program does not infringe the
  patent or other intellectual property rights of any other entity.
  Each Contributor disclaims any liability to Recipient for claims
  brought by any other entity based on infringement of intellectual
  property rights or otherwise. As a condition to exercising the
  rights and licenses granted hereunder, each Recipient hereby
  assumes sole responsibility to secure any other intellectual
  property rights needed, if any. For example, if a third party
  patent license is required to allow Recipient to Distribute the
  Program, it is Recipient's responsibility to acquire that license
  before distributing the Program.

  d) Each Contributor represents that to its knowledge it has
  sufficient copyright rights in its Contribution, if any, to grant
  the copyright license set forth in this Agreement.

  e) Notwithstanding the terms of any Secondary License, no
  Contributor makes additional grants to any Recipient (other than
  those set forth in this Agreement) as a result of such Recipient's
  receipt of the Program under the terms of a Secondary License
  (if permitted under the terms of Section 3).

3. REQUIREMENTS

3.1 If a Contributor Distributes the Program in any form, then:

  a) the Program must also be made available as Source Code, in
  accordance with section 3.2, and the Contributor must accompany
  the Program with a statement that the Source Code for the Program
  is available under this Agreement, and informs Recipients how to
  obtain it in a reasonable manner on or through a medium customarily
  used for software exchange; and

  b) the Contributor may Distribute the Program under a license
  different than this Agreement, provided that such license:
     i) effectively disclaims on behalf of all other Contributors all
     warranties and conditions, express and implied, including
     warranties or conditions of title and non-infringement, and
     implied warranties or conditions of merchantability and fitness
     for a particular purpose;

     ii) effectively excludes on behalf of all other Contributors all
     liability for damages, including direct, indirect, special,
     incidental and consequential damages, such as lost profits;

     iii) does not attempt to limit or alter the recipients' rights
     in the Source Code under section 3.2; and

     iv) requires any subsequent distribution of the Program by any
     party to be under a license that satisfies the requirements
     of this section 3.

3.2 When the Program is Distributed as Source Code:

  a) it must be made available under this Agreement, or if the
  Program (i) is combined with other material in a separate file or
  files made available under a Secondary License, and (ii) the initial
  Contributor attached to the Source Code the notice described in
  Exhibit A of this Agreement, then the Program may be made available
  under the terms of such Secondary Licenses, and

  b) a copy of this Agreement must be included with each copy of
  the Program.

3.3 Contributors may not remove or alter any copyright, patent,
trademark, attribution notices, disclaimers of warranty, or limitations
of liability ("notices") contained within the Program from any copy of
the Program which they Distribute, provided that Contributors may add
their own appropriate notices.

4. COMMERCIAL DISTRIBUTION

Commercial distributors of software may accept certain responsibilities
with respect to end users, business partners and the like. While this
license is intended to facilitate the commercial use of the Program,
the Contributor who includes the Program in a commercial product
offering should do so in a manner which does not create potential
liability for other Contributors. Therefore, if a Contributor includes
the Program in a commercial product offering, such Contributor
("Commercial Contributor") hereby agrees to defend and indemnify every
other Contributor ("Indemnified Contributor") against any losses,
damages and costs (collectively "Losses") arising from claims, lawsuits
and other legal actions brought by a third party against the Indemnified
Contributor to the extent caused by the acts or omissions of such
Commercial Contributor in connection with its distribution of the Program
in a commercial product offering. The obligations in this section do not
apply to any claims or Losses relating to any actual or alleged
intellectual property infringement. In order to qualify, an Indemnified
Contributor must: a) promptly notify the Commercial Contributor in
writing of such claim, and b) allow the Commercial Contributor to control,
and cooperate with the Commercial Contributor in, the defense and any
related settlement negotiations. The Indemnified Contributor may
participate in any such claim at its own expense.

For example, a Contributor might include the Program in a commercial
product offering, Product X. That Contributor is then a Commercial
Contributor. If that Commercial Contributor then makes performance
claims, or offers warranties related to Product X, those performance
claims and warranties are such Commercial Contributor's responsibility
alone. Under this section, the Commercial Contributor would have to
defend claims against the other Contributors related to those performance
claims and warranties, and if a court requires any other Contributor to
pay any damages as a result, the Commercial Contributor must pay
those damages.

5. NO WARRANTY

EXCEPT AS EXPRESSLY SET FORTH IN THIS AGREEMENT, AND TO THE EXTENT
PERMITTED BY APPLICABLE LAW, THE PROGRAM IS PROVIDED ON AN "AS IS"
BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, EITHER EXPRESS OR
IMPLIED INCLUDING, WITHOUT LIMITATION, ANY WARRANTIES OR CONDITIONS OF
TITLE, NON-INFRINGEMENT, MERCHANTABILITY OR FITNESS FOR A PARTICULAR
PURPOSE. Each Recipient is solely responsible for determining the
appropriateness of using and distributing the Program and assumes all
risks associated with its exercise of rights under this Agreement,
including but not limited to the risks and costs of program errors,
compliance with applicable laws, damage to or loss of data, programs
or equipment, and unavailability or interruption of operations.

6. DISCLAIMER OF LIABILITY

EXCEPT AS EXPRESSLY SET FORTH IN THIS AGREEMENT, AND TO THE EXTENT
PERMITTED BY APPLICABLE LAW, NEITHER RECIPIENT NOR ANY CONTRIBUTORS
SHALL HAVE ANY LIABILITY FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING WITHOUT LIMITATION LOST
PROFITS), HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OR DISTRIBUTION OF THE PROGRAM OR THE
EXERCISE OF ANY RIGHTS GRANTED HEREUNDER, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGES.

7. GENERAL

If any provision of this Agreement is invalid or unenforceable under
applicable law, it shall not affect the validity or enforceability of
the remainder of the terms of this Agreement, and without further
action by the parties hereto, such provision shall be reformed to the
minimum extent necessary to make such provision valid and enforceable.

If Recipient institutes patent litigation against any entity
(including a cross-claim or counterclaim in a lawsuit) alleging that the
Program itself (excluding combinations of the Program with other software
or hardware) infringes such Recipient's patent(s), then such Recipient's
rights granted under Section 2(b) shall terminate as of the date such
litigation is filed.

All Recipient's rights under this Agreement shall terminate if it
fails to comply with any of the material terms or conditions of this
Agreement and does not cure such failure in a reasonable period of
time after becoming aware of such noncompliance. If all Recipient's
rights under this Agreement terminate, Recipient agrees to cease use
and distribution of the Program as soon as reasonably practicable.
However, Recipient's obligations under this Agreement and any licenses
granted by Recipient relating to the Program shall continue and survive.

Everyone is permitted to copy and distribute copies of this Agreement,
but in order to avoid inconsistency the Agreement is copyrighted and
may only be modified in the following manner. The Agreement Steward
reserves the right to publish new versions (including revisions) of
this Agreement from time to time. No one other than the Agreement
Steward has the right to modify this Agreement. The Eclipse Foundation
is the initial Agreement Steward. The Eclipse Foundation may assign the
responsibility to serve as the Agreement Steward to a suitable separate
entity. Each new version of the Agreement will be given a distinguishing
version number. The Program (including Contributions) may always be
Distributed subject to the version of the Agreement under which it was
received. In addition, after a new version of the Agreement is published,
Contributor may elect to Distribute the Program (including its
Contributions) under the new version.

Except as expressly stated in Sections 2(a) and 2(b) above, Recipient
receives no rights or licenses to the intellectual property of any
Contributor under this Agreement, whether expressly, by implication,
estoppel or otherwise. All rights in the Program not expressly granted
under this Agreement are reserved. Nothing in this Agreement is intended
to be enforceable by any entity that is not a Contributor or Recipient.
No third-party beneficiary rights are created under this Agreement.

Exhibit A - Form of Secondary Licenses Notice

"This Source Code may also be made available under the following
Secondary Licenses when the conditions for such availability set forth
in the Eclipse Public License, v. 2.0 are satisfied: {name license(s),
version(s), and exceptions or additional permissions here}."

  Simply including a copy of this Agreement, including this Exhibit A
  is not sufficient to license the Source Code under Secondary Licenses.

  If it is not possible or desirable to put the notice in a particular
  file, then You may include the notice in a location (such as a LICENSE
  file in a relevant directory) where a recipient would be likely to
  look for such a notice.

  You may add additional accurate notices of copyright ownership.
```

#### Expat MIT license

```text
Copyright (c) 1998-2000 Thai Open Source Software Center Ltd and Clark Cooper
Copyright (c) 2001-2026 Expat maintainers

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### Other Web UI dependencies

OpenSquilla builds the Vue Control UI from npm dependencies and locally bundled
fonts. Release artifacts contain the generated browser assets from the
source-owned `opensquilla-webui/dist/` tree (staged into
`src/opensquilla/gateway/static/dist/` for Python packages); the dependency sources and exact
resolved versions are recorded by `opensquilla-webui/package.json` and
`opensquilla-webui/package-lock.json`.

| Component | Distributed files | License and attribution |
|---|---|---|
| Vue.js (`vue`, `@vue/reactivity`, `@vue/runtime-core`, `@vue/runtime-dom`, `@vue/shared`) | Vue runtime in generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2018-present, Yuxi (Evan) You. |
| Pinia (`pinia`) | State-management runtime in generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2019-present Eduardo San Martin Morote. |
| TanStack Virtual (`@tanstack/vue-virtual`, `@tanstack/virtual-core`; [TanStack/virtual](https://github.com/TanStack/virtual)) | List virtualization and measurement runtime in generated Web UI JavaScript | MIT. Copyright (c) 2021-present Tanner Linsley. |
| Vue Devtools API (`@vue/devtools-api`; [vuejs/devtools](https://github.com/vuejs/devtools)) | Explicit Pinia devtools peer; integration code where retained in generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2023 webfansplz. |
| Vue Router (`vue-router`) | Client-side routing runtime in generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2019-present Eduardo San Martin Morote. |
| Vue I18n (`vue-i18n`, `@intlify/core-base`, `@intlify/message-compiler`, `@intlify/shared`) | Localization runtime in generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2020 kazuya kawaguchi. |
| noble-hashes (`@noble/hashes`; [paulmillr/noble-hashes](https://github.com/paulmillr/noble-hashes)) | SHA-256 fallback for Web UI delivery identity generation when the browser does not expose SubtleCrypto; generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2022 Paul Miller (paulmillr.com). |
| html-to-image (`html-to-image`) | Image-export runtime in generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2017-2025 W.Y. |
| KaTeX (`katex`) | npm dependency used by `opensquilla-webui/src/composables/chat/useChatTextRendering.ts`; generated JavaScript, CSS, and `KaTeX_*` fonts under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2013-2020 Khan Academy and other contributors. |
| marked (`marked`) | npm dependency used by `opensquilla-webui/src/composables/chat/useChatTextRendering.ts`; generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MIT. Copyright (c) 2018+, MarkedJS and Copyright (c) 2011-2018, Christopher Jeffrey. The bundled Markdown-derived portion carries the John Gruber notice reproduced below. |
| DOMPurify (`dompurify`) | npm dependency used by `opensquilla-webui/src/composables/chat/useChatTextRendering.ts`; generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | MPL-2.0 OR Apache-2.0. OpenSquilla distributes this component under the Apache-2.0 option. Copyright belongs to Cure53 and other contributors. |
| highlight.js (`highlight.js`) | npm dependency used by `opensquilla-webui/src/composables/chat/useChatTextRendering.ts`; generated JavaScript under `src/opensquilla/gateway/static/dist/assets/` | BSD-3-Clause. Copyright (c) 2006, Ivan Sagalaev. |
| PDF.js (`pdfjs-dist` 5.4.296; [mozilla/pdf.js](https://github.com/mozilla/pdf.js)) | PDF reader runtime, worker, CMaps, standard fonts and WebAssembly resources used by `opensquilla-webui/src/workbench/pdfPreview.ts`; generated assets under `src/opensquilla/gateway/static/dist/assets/` | Core: Apache-2.0, Copyright 2024 Mozilla Foundation. Bundled resource licenses and copyright notices are reproduced below. |
| Papa Parse (`papaparse` 5.5.3; [mholt/PapaParse](https://github.com/mholt/PapaParse)) | CSV/TSV parsing runtime used by `opensquilla-webui/src/workbench/csvPreview.ts`; generated Web UI JavaScript | MIT. Copyright (c) 2015 Matthew Holt. |
| IBM Plex Sans and IBM Plex Mono | `opensquilla-webui/src/assets/fonts/ibm-plex-*.woff2` and generated Web UI font assets | SIL Open Font License 1.1. Copyright 2017 IBM Corp. with Reserved Font Name "Plex". |
| Space Grotesk | `opensquilla-webui/src/assets/fonts/space-grotesk-*.woff2` and generated Web UI font assets | SIL Open Font License 1.1. Copyright 2020 The Space Grotesk Project Authors. |

The Web UI lockfile is the version authority for these dependencies. The build
pipeline regenerates the browser bundle from that lockfile; no separate
hand-maintained copies of these libraries are shipped by the gateway.

### PDF.js bundled resources and Papa Parse notices

The pinned `pdfjs-dist` package includes resources emitted by the PDF preview
build in addition to its Apache-2.0 runtime and worker. The package's core
license is Apache License 2.0 (reproduced in the repository `LICENSE`); the
resource-specific notices below are copied from that same 5.4.296 package.
Papa Parse's MIT notice is copied from its pinned 5.5.3 package.

#### PDF.js Adobe CMaps

Source: `pdfjs-dist/cmaps/LICENSE` in the pinned npm package.

```text
%%Copyright: -----------------------------------------------------------
%%Copyright: Copyright 1990-2009 Adobe Systems Incorporated.
%%Copyright: All rights reserved.
%%Copyright:
%%Copyright: Redistribution and use in source and binary forms, with or
%%Copyright: without modification, are permitted provided that the
%%Copyright: following conditions are met:
%%Copyright:
%%Copyright: Redistributions of source code must retain the above
%%Copyright: copyright notice, this list of conditions and the following
%%Copyright: disclaimer.
%%Copyright:
%%Copyright: Redistributions in binary form must reproduce the above
%%Copyright: copyright notice, this list of conditions and the following
%%Copyright: disclaimer in the documentation and/or other materials
%%Copyright: provided with the distribution.
%%Copyright:
%%Copyright: Neither the name of Adobe Systems Incorporated nor the names
%%Copyright: of its contributors may be used to endorse or promote
%%Copyright: products derived from this software without specific prior
%%Copyright: written permission.
%%Copyright:
%%Copyright: THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
%%Copyright: CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
%%Copyright: INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
%%Copyright: MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
%%Copyright: DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
%%Copyright: CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
%%Copyright: SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
%%Copyright: NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
%%Copyright: LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
%%Copyright: HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
%%Copyright: CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
%%Copyright: OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
%%Copyright: SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
%%Copyright: -----------------------------------------------------------
```

#### PDF.js Foxit/PDFium standard fonts

Source: `pdfjs-dist/standard_fonts/LICENSE_FOXIT` in the pinned npm package.

```text
// Copyright 2014 PDFium Authors. All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions are
// met:
//
//    * Redistributions of source code must retain the above copyright
// notice, this list of conditions and the following disclaimer.
//    * Redistributions in binary form must reproduce the above
// copyright notice, this list of conditions and the following disclaimer
// in the documentation and/or other materials provided with the
// distribution.
//    * Neither the name of Google Inc. nor the names of its
// contributors may be used to endorse or promote products derived from
// this software without specific prior written permission.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
// "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
// LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
// A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
// OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
// SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
// LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
// DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
// THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
// OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

#### PDF.js Liberation standard fonts

Source: `pdfjs-dist/standard_fonts/LICENSE_LIBERATION` in the pinned npm package.

```text
Digitized data copyright (c) 2010 Google Corporation
	with Reserved Font Arimo, Tinos and Cousine.
Copyright (c) 2012 Red Hat, Inc.
	with Reserved Font Name Liberation.

This Font Software is licensed under the SIL Open Font License,
Version 1.1.

This license is copied below, and is also available with a FAQ at:
http://scripts.sil.org/OFL

SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007

PREAMBLE The goals of the Open Font License (OFL) are to stimulate
worldwide development of collaborative font projects, to support the font
creation efforts of academic and linguistic communities, and to provide
a free and open framework in which fonts may be shared and improved in
partnership with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves.
The fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works.  The fonts and derivatives,
however, cannot be released under any other type of license.  The
requirement for fonts to remain under this license does not apply to
any document created using the fonts or their derivatives.



DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such.
This may include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components
as distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting ? in part or in whole ?
any of the components of the Original Version, by changing formats or
by porting the Font Software to a new environment.

"Author" refers to any designer, engineer, programmer, technical writer
or other person who contributed to the Font Software.


PERMISSION & CONDITIONS

Permission is hereby granted, free of charge, to any person obtaining a
copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,in
   Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
   redistributed and/or sold with any software, provided that each copy
   contains the above copyright notice and this license. These can be
   included either as stand-alone text files, human-readable headers or
   in the appropriate machine-readable metadata fields within text or
   binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
   Name(s) unless explicit written permission is granted by the
   corresponding Copyright Holder. This restriction only applies to the
   primary font name as presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
   Software shall not be used to promote, endorse or advertise any
   Modified Version, except to acknowledge the contribution(s) of the
   Copyright Holder(s) and the Author(s) or with their explicit written
   permission.

5) The Font Software, modified or unmodified, in part or in whole, must
   be distributed entirely under this license, and must not be distributed
   under any other license. The requirement for fonts to remain under
   this license does not apply to any document created using the Font
   Software.



TERMINATION
This license becomes null and void if any of the above conditions are not met.



DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT.  IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER
DEALINGS IN THE FONT SOFTWARE.
```

#### PDF.js OpenJPEG WebAssembly

Source: `pdfjs-dist/wasm/LICENSE_OPENJPEG` in the pinned npm package.

```text
/*
 * The copyright in this software is being made available under the 2-clauses
 * BSD License, included below. This software may be subject to other third
 * party and contributor rights, including patent rights, and no such rights
 * are granted under this license.
 *
 * Copyright (c) 2002-2014, Universite catholique de Louvain (UCL), Belgium
 * Copyright (c) 2002-2014, Professor Benoit Macq
 * Copyright (c) 2003-2014, Antonin Descampe
 * Copyright (c) 2003-2009, Francois-Olivier Devaux
 * Copyright (c) 2005, Herve Drolon, FreeImage Team
 * Copyright (c) 2002-2003, Yannick Verschueren
 * Copyright (c) 2001-2003, David Janssens
 * Copyright (c) 2011-2012, Centre National d'Etudes Spatiales (CNES), France
 * Copyright (c) 2012, CS Systemes d'Information, France
 *
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS `AS IS'
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 */
```

#### PDF.js OpenJPEG wrapper

Source: `pdfjs-dist/wasm/LICENSE_PDFJS_OPENJPEG` in the pinned npm package.

```text
Copyright (c) 2024, Mozilla Foundation

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

#### PDF.js qcms WebAssembly

Source: `pdfjs-dist/wasm/LICENSE_QCMS` in the pinned npm package.

```text
qcms
Copyright (C) 2009-2024 Mozilla Corporation
Copyright (C) 1998-2007 Marti Maria

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the Software
is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

#### PDF.js qcms wrapper

Source: `pdfjs-dist/wasm/LICENSE_PDFJS_QCMS` in the pinned npm package.

```text
Copyright (c) 2025, Mozilla Foundation

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

#### Papa Parse

Source: `papaparse/LICENSE` in the pinned npm package.

```text
The MIT License (MIT)

Copyright (c) 2015 Matthew Holt

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### TanStack Virtual MIT license

```text
MIT License

Copyright (c) 2021-present Tanner Linsley

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### marked Markdown notice

```text
Copyright (c) 2004, John Gruber
http://daringfireball.net/
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice,
  this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.
* Neither the name "Markdown" nor the names of its contributors may be used to
  endorse or promote products derived from this software without specific
  prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

### highlight.js BSD-3-Clause license

```text
BSD 3-Clause License

Copyright (c) 2006, Ivan Sagalaev.
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

## Lucide Icons

- Component: `sparkles`, `settings-2`, `clock-3`, `chart-no-axes-column`, and
  `folder-outline` (upstream `folder`) SVG path data in
  `opensquilla-webui/src/utils/icons.ts` and the generated Web UI assets.
- Upstream project: https://github.com/lucide-icons/lucide
- Version: 1.17.0
- License: ISC

```
ISC License

Copyright (c) 2026 Lucide Icons and Contributors

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## GitHub Octicons

- Component: `gitBranch` (upstream `git-branch-16`) icon path data in
  `opensquilla-webui/src/utils/icons.ts` and the generated Web UI assets.
- Upstream project: https://github.com/primer/octicons
- License: MIT
- Copyright notice: Copyright (c) 2026 GitHub Inc.

```
MIT License

Copyright (c) 2026 GitHub Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Feather Icons

- Component: `music`, `pause`, and `volume` icon path data in
  `opensquilla-webui/src/utils/icons.ts` and the generated Web UI assets.
- Upstream project: https://github.com/feathericons/feather
- License: MIT
- Copyright notice: Copyright (c) 2013-2017 Cole Bemis

```
MIT License

Copyright (c) 2013-2017 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The MIT license text is reproduced in this file for MIT-licensed bundled
components. The Apache License 2.0 text is reproduced in the repository
`LICENSE` file. The SIL Open Font License 1.1 text for the bundled fonts is
reproduced below.

```
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any Modified
Version, except to acknowledge the contribution(s) of the Copyright
Holder(s) and the Author(s) or with their explicit written permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to remain
under this license does not apply to any document created using the Font
Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```

## Arctic theme color palette (Nord)

- Component: Web UI "Arctic" value theme color palette in
  `opensquilla-webui/src/themes/arctic/tokens.css` (and its generated Web UI CSS
  assets).
- Upstream project: https://www.nordtheme.com
- License: MIT
- Copyright notice: Copyright (c) Sven Greb and the Nord contributors

The "Arctic" theme's color values are adapted from the Nord palette. OpenSquilla
is not affiliated with, sponsored by, or endorsed by the Nord project; the
palette is reused under the MIT license solely for its color values, with a
matching attribution header reproduced at the top of the theme's `tokens.css`.
Some values are lightened from the upstream palette so the theme passes
OpenSquilla's WCAG contrast guards. The MIT license text is reproduced below.

```
MIT License

Copyright (c) Sven Greb and the Nord contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Optional managed toolchains downloaded at runtime

OpenSquilla does not embed the following toolchains in its source distribution
or wheel. The Web setup flow presents their source, version, license, and known
download size, asks for explicit consent, and then stores verified artifacts in
the user's OpenSquilla state directory. Each downloaded component remains under
its upstream terms.

| Runtime component | Catalog source | Catalog license / provenance note |
|---|---|---|
| TinyTeX 2026.05 / TeX Live packages | https://github.com/rstudio/tinytex-releases | TinyTeX release repository: GPL-2.0. The self-contained verified archive is not updated from the floating TeX Live network. Its included packages remain governed by their own TeX Live catalog metadata, and upstream license files remain in the downloaded tree. |
| FFmpeg / FFprobe macOS 8.1.2 ZIPs | https://ffmpeg.martin-riedl.de/ | GPL-3.0-or-later builds selected separately for Apple Silicon and Intel, with the build source pinned to commit [`bb1d6db29cee948f9685bcd69e6caf17d960662b`](https://git.martin-riedl.de/ffmpeg/build-script/commit/bb1d6db29cee948f9685bcd69e6caf17d960662b). Each original ZIP is verified by fixed byte size and SHA-256 before extraction. Because the embedded binary signatures are invalid, OpenSquilla then removes them, applies a local ad-hoc signature, and requires strict `codesign` verification before activation. This local signature is not a Developer ID signature or Apple notarization. The matching pinned GPL license assets are installed beside the binaries. These builds require macOS 12 or later. |
| FFmpeg Linux static GPL builds | https://github.com/BtbN/FFmpeg-Builds | GPL-3.0-or-later catalog build. |
| FFmpeg Windows essentials build | https://github.com/GyanD/codexffmpeg | GPL build; upstream license files remain in the downloaded archive. |
| Noto Sans CJK 2.004 regular font | https://github.com/notofonts/noto-cjk/tree/Sans2.004 | SIL Open Font License 1.1. The pinned license file is downloaded and checksum-verified beside the font; the OFL text is also reproduced earlier in this notice. |

The built-in catalog records fixed URLs, byte sizes, and SHA-256 values for
every executable archive, license file, and font download. OpenSquilla neither
republishes these toolchains as part of its releases nor claims ownership of
them.

## npm and Python dependency packaging strategy

OpenSquilla uses npm lockfiles for the Web UI and Electron shell and `uv.lock`
for Python release environments. Lockfiles pin dependency resolution for
reproducible builds, but lockfiles are not a replacement for third-party license
notices when source, minified JavaScript, fonts, model files, or adapted code
are copied into OpenSquilla release artifacts.

Build-time npm dependencies are installed from the package registry during CI.
The Electron and gateway release artifacts distribute the compiled Control UI,
selected static assets, updater metadata, and packaged runtime outputs rather
than the full npm source tree. Any static npm-derived file copied into
`src/opensquilla/gateway/static/` must be listed in this notices file or in a
component-local notice file.

Python dependencies are resolved from `pyproject.toml` and `uv.lock` during
wheel, portable, and packaged-gateway builds. Python packages remain governed by
their upstream package metadata and licenses. If OpenSquilla vendors or adapts
Python package code, model files, rule files, binary assets, or generated
runtime artifacts into the repository, the component must be recorded here or
in a package-local provenance file.

## OpenClaw-derived bundled skill descriptors

- Component: SKILL.md frontmatter and instruction text for these bundled skills:
  - `sub-agent`
  - `github`
  - `skill-creator`
- Upstream project: https://github.com/openclaw/openclaw
- License: MIT
- Copyright notice: Copyright (c) 2025 Peter Steinberger

Note: `sub-agent` was renamed from `coding-agent` on 2026-05-23; the
descriptor retains the same OpenClaw upstream lineage and MIT attribution.

The descriptor text instructs the agent runtime how to use built-in skill
surfaces and external tools; OpenSquilla does not redistribute third-party CLIs
through these descriptors. Per the MIT license, the upstream copyright and
permission notice are reproduced below in their entirety and apply to the
OpenClaw-derived bundled descriptor files.

```
MIT License

Copyright (c) 2025 Peter Steinberger

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## OpenSquilla-original bundled skills

These bundled skill descriptors are authored and maintained by OpenSquilla and
are released under OpenSquilla's repository license (Apache-2.0; see `LICENSE`):

- `browser-use`
- `deep-research`
- `docx`
- `github`
- `pdf-toolkit`
- `pptx`
- `skill-creator`
- `sub-agent`
- `xlsx`

## OpenSquilla-adapted bundled skills

These bundled skill descriptors combine OpenSquilla-authored host guidance with
adapted upstream material. The upstream license and source attribution are
documented in the Inline Visualize frame runtime section above and shipped with
the skill as `LICENSE.generative-frame.txt`:

- `visualize`

## tokenjuice adapted reduction rules

- Component: built-in tokenjuice tool-result projection backend and bundled
  reduction rules under `src/opensquilla/plugins/tokenjuice/`.
- Upstream project: https://github.com/vincentkoc/tokenjuice
- License: MIT
- Copyright notice: Copyright (c) 2026 Vincent Koc

OpenSquilla includes a Python adaptation of tokenjuice's rule-driven reducer
and bundles reduction rules derived from the upstream project. OpenSquilla does
not depend on the upstream tokenjuice npm package at runtime. Additional
provenance is recorded in
`src/opensquilla/plugins/tokenjuice/PROVENANCE.md`; the MIT license text is
also shipped with that package as `LICENSE.tokenjuice`.

```
MIT License

Copyright (c) 2026 Vincent Koc

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Hermes Agent reference material

- Component: cron prompt-injection scanner reference material.
- Upstream project: https://github.com/NousResearch/hermes-agent
- License: MIT
- Copyright notice: Copyright (c) 2025 Nous Research

OpenSquilla does not redistribute Hermes Agent. This notice records conservative
attribution for reference material reviewed while hardening OpenSquilla's cron
prompt scanner.

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## ClawHub-derived bundled skill descriptors

- Component: SKILL.md frontmatter and instruction text for these bundled skills:
  - `deep-research`
  - `docx`
  - `html-coder`
  - `pdf-toolkit`
  - `pptx`
  - `xlsx`
- Upstream registry: https://clawhub.ai
- License: MIT-0 (Public-domain-equivalent; no attribution required, but
  each skill records its specific upstream slug in its own
  `THIRD_PARTY_NOTICES.md` for transparency)

These bundled skills record their ClawHub source slug in SKILL.md frontmatter
and, when present, the skill-local `THIRD_PARTY_NOTICES.md`. ClawHub's MIT-0
default license permits unlimited use, modification, and redistribution without
attribution.

```
MIT No Attribution

Copyright

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## BAAI bge-small-zh-v1.5 / FlagEmbedding

- Component: BAAI/bge-small-zh-v1.5 embedding model and tokenizer assets.
- Upstream model: https://huggingface.co/BAAI/bge-small-zh-v1.5
- Upstream project: https://github.com/FlagOpen/FlagEmbedding
- License: MIT
- Copyright notice: Copyright (c) 2022 staoxiao

The bundled router contains an ONNX export and tokenizer files derived from
the BAAI bge-small-zh-v1.5 model. The upstream Hugging Face model card marks
the model as MIT licensed and states that the released models can be used for
commercial purposes free of charge.

MIT License

Copyright (c) 2022 staoxiao

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Router Artifact Safety Note

The SquillaRouter bundle contains `.pkl` and `.joblib` artifacts used by the
current V4 Phase 3 runtime. Treat these artifacts as executable-code-equivalent
inputs: load only assets shipped with a trusted OpenSquilla release or assets
whose checksums match `artifact_manifest.json`.

## Workspace application artwork

The file opener bundles the following unmodified SVG artwork in
`opensquilla-webui/src/assets/workspace-apps/`. Application names and marks remain
the property of their respective owners.

- `vscode.svg`, `cursor.svg`, `sublime.svg`: Material Icon Theme, revision
  `cb1dfb6d9cb73b15681a93939983d75dbba7bf5b`, imported through ZCode's attributed
  material-icons assets. Source: https://github.com/material-extensions/vscode-material-icon-theme/tree/cb1dfb6d9cb73b15681a93939983d75dbba7bf5b/icons
- `windsurf.svg`, `zed.svg`: CodexBar, revision
  `215efbd39ad58acb4b0cd637c407d63e6183bc8d`, imported through OpenClaw's attributed
  provider-icons assets. Source: https://github.com/steipete/CodexBar/tree/215efbd39ad58acb4b0cd637c407d63e6183bc8d/Sources/CodexBar/Resources

Both artwork collections use the MIT License:

Copyright (c) 2025 Material Extensions

Copyright (c) 2026 Peter Steinberger

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
