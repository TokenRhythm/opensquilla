---
name: awesome-webpage-image-download
description: "Deterministic image downloader for AwesomeWebpageMetaSkill search results. Use as skill_exec to fetch candidate image URLs into the configured local project tree without sandboxed shell curl."
description_zh: "为AwesomeWebpageMetaSkill搜索结果提供确定性的图片下载器。作为skill_exec使用，将候选图片URL抓取到配置的本地项目目录，无需沙箱内的shell curl。"
homepage: ""
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-read, filesystem-write]
    requires:
      bins: [python3]
      config:
        - awesome_webpage.output_dir
entrypoint:
  command: python {baseDir}/scripts/image_download.py
  args:
    - --output-dir
    - "{{ with.output_dir }}"
    - --local-path-prefix
    - "{{ with.local_path_prefix | default('project/assets/images') }}"
  stdin: "{{ with.payload }}"
  parse: text
  timeout: 120
---

# AwesomeWebpage Image Download

Internal deterministic downloader used by `AwesomeWebpageMetaSkill` after the
bounded web-search step has produced candidate image URLs.

It receives normalized media slots and search output on stdin, downloads direct
image URLs with Python HTTP APIs, validates the response MIME/magic bytes, saves
files by `slot_id`, and emits `IMAGE_READY:` records plus
`IMAGE_DOWNLOAD_INCOMPLETE:` when any requested image slot remains unfilled.
