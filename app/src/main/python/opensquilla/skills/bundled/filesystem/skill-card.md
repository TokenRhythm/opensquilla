## Description: <br>
Advanced filesystem operations for listing, searching, batch processing, and directory analysis. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[gtrusler](https://clawhub.ai/user/gtrusler) <br>

### License/Terms of Use: <br>
MIT <br>


## Use Case: <br>
Developers and automation users use this skill to inspect local directories, search file names or contents, analyze storage patterns, and prepare controlled copy operations. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: The skill is designed to inspect and copy local files, so broad paths or sensitive directories can expose or move data unintentionally. <br>
Mitigation: Keep paths narrow, avoid sensitive directories unless intended, and use dry-run mode before copy operations. <br>
Risk: The security evidence notes that the scanned artifact set did not include the runnable filesystem binary. <br>
Mitigation: Review the actual CLI implementation from the resolved package or source before executing it in a trusted environment. <br>


## Reference(s): <br>
- [ClawHub skill page](https://clawhub.ai/gtrusler/clawdbot-filesystem) <br>


## Skill Output: <br>
**Output Type(s):** [text, markdown, shell commands, configuration, guidance] <br>
**Output Format:** [Markdown or plain text with inline shell commands and optional JSON output from the filesystem CLI] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Requires Node.js and local filesystem access; dry-run mode is recommended before copy operations.] <br>

## Skill Version(s): <br>
1.0.2 (source: server release metadata and package.json) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
