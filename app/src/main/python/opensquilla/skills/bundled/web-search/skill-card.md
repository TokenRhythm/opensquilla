## Description: <br>
This skill should be used when users need to search the web for information, find current content, look up news articles, search for images, or find videos. It uses DuckDuckGo's search API to return results in clean, formatted output (text, markdown, or JSON). Use for research, fact-checking, finding recent information, or gathering web resources. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[billyutw](https://clawhub.ai/user/billyutw) <br>

### License/Terms of Use: <br>


## Use Case: <br>
Developers, researchers, and agents use this skill to search current web, news, image, and video results with optional filters for recency, region, safe search, and result type. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: Search terms are sent to DuckDuckGo through the installed dependency. <br>
Mitigation: Avoid searching for secrets, credentials, confidential business data, or highly sensitive personal information. <br>
Risk: Using --output can overwrite files at the selected output path. <br>
Mitigation: Choose intentional, non-sensitive output paths and review paths before running file-saving searches. <br>
Risk: Web, news, image, and video result quality depends on DuckDuckGo availability, ranking, and index coverage. <br>
Mitigation: Review returned sources before relying on results, especially for time-sensitive or high-impact decisions. <br>


## Reference(s): <br>
- [ClawHub skill page](https://clawhub.ai/billyutw/web-search) <br>
- [Publisher profile](https://clawhub.ai/user/billyutw) <br>


## Skill Output: <br>
**Output Type(s):** [text, markdown, JSON, files] <br>
**Output Format:** [Plain text, Markdown, or JSON search results; optional saved output files.] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Results include titles, URLs, summaries, and type-specific fields such as sources, dates, thumbnails, dimensions, publishers, or durations when available.] <br>

## Skill Version(s): <br>
1.0.0 (source: server release metadata) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
