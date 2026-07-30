---
name: filesystem
description: Advanced filesystem operations - listing, searching, batch processing, and directory analysis for Clawdbot
description_zh: "高级文件系统操作——为Clawdbot提供列举、搜索、批量处理和目录分析。"
homepage: https://clawhub.ai/gtrusler/clawdbot-filesystem
provenance:
  origin: clawhub-mit
  license: MIT
  upstream_url: https://clawhub.ai/gtrusler/clawdbot-filesystem
  maintained_by: OpenSquilla
metadata:
  clawdbot:
    emoji: "📁"
    requires:
      bins: [node]
  opensquilla:
    risk: medium
    capabilities: [filesystem-read, filesystem-write, command-exec]
    requires:
      bins: []
      env: []
---

# 📁 Filesystem Management

Advanced filesystem operations for AI agents. Comprehensive file and directory operations with intelligent filtering, searching, and batch processing capabilities.

## OpenSquilla Compatibility Contract

When invoked from OpenSquilla, use the host-provided filesystem tools in the
current workspace. Do not install `clawdbot-filesystem`, do not call ClawHub,
and do not assume a standalone `filesystem` binary exists.

For `AwesomeWebpageMetaSkill` local packaging:

- Use only the `config.awesome_webpage.output_dir` supplied by the caller.
- Create or verify exactly:
  - `project/index.html`
  - `project/style.css`
  - `project/script.js`
  - `project/assets/images`
  - `project/assets/audio`
  - `project/assets/video`
- Keep all paths inside the current OpenSquilla workspace or the supplied
  output directory.
- If generation did not produce files, return `PACKAGING_BLOCKED` with the
  missing paths; do not try to discover unrelated config files.

## Features

### 📋 **Smart File Listing**
- **Advanced Filtering** - Filter by file types, patterns, size, and date
- **Recursive Traversal** - Deep directory scanning with depth control
- **Rich Formatting** - Table, tree, and JSON output formats
- **Sort Options** - By name, size, date, or type

### 🔍 **Powerful Search**
- **Pattern Matching** - Glob patterns and regex support
- **Content Search** - Full-text search within files
- **Multi-criteria** - Combine filename and content searches
- **Context Display** - Show matching lines with context

### 🔄 **Batch Operations**
- **Safe Copying** - Pattern-based file copying with validation
- **Dry Run Mode** - Preview operations before execution
- **Progress Tracking** - Real-time operation progress
- **Error Handling** - Graceful failure recovery

### 🌳 **Directory Analysis**
- **Tree Visualization** - ASCII tree structure display
- **Statistics** - File counts, size distribution, type analysis
- **Space Analysis** - Identify large files and directories
- **Performance Metrics** - Operation timing and optimization

## Quick Start

```bash
# List files with filtering
filesystem list --path ./src --recursive --filter "*.js"

# Search for content
filesystem search --pattern "TODO" --path ./src --content

# Batch copy with safety
filesystem copy --pattern "*.log" --to ./backup/ --dry-run

# Show directory tree
filesystem tree --path ./ --depth 3

# Analyze directory structure
filesystem analyze --path ./logs --stats
```

## Command Reference

### `filesystem list`
Advanced file and directory listing with filtering options.

**Options:**
- `--path, -p <dir>` - Target directory (default: current)
- `--recursive, -r` - Include subdirectories
- `--filter, -f <pattern>` - Filter files by pattern
- `--details, -d` - Show detailed information
- `--sort, -s <field>` - Sort by name|size|date
- `--format <type>` - Output format: table|json|list

### `filesystem search`
Search files by name patterns or content.

**Options:**
- `--pattern <pattern>` - Search pattern (glob or regex)
- `--path, -p <dir>` - Search directory
- `--content, -c` - Search file contents
- `--context <lines>` - Show context lines
- `--include <pattern>` - Include file patterns
- `--exclude <pattern>` - Exclude file patterns

### `filesystem copy`
Batch copy files with pattern matching and safety checks.

**Options:**
- `--pattern <glob>` - Source file pattern
- `--to <dir>` - Destination directory
- `--dry-run` - Preview without executing
- `--overwrite` - Allow file overwrites
- `--preserve` - Preserve timestamps and permissions

### `filesystem tree`
Display directory structure as a tree.

**Options:**
- `--path, -p <dir>` - Root directory
- `--depth, -d <num>` - Maximum depth
- `--dirs-only` - Show directories only
- `--size` - Include file sizes
- `--no-color` - Disable colored output

### `filesystem analyze`
Analyze directory structure and generate statistics.

**Options:**
- `--path, -p <dir>` - Target directory
- `--stats` - Show detailed statistics
- `--types` - Analyze file types
- `--sizes` - Show size distribution
- `--largest <num>` - Show N largest files

## Installation

```bash
# Clone or install the skill
cd ~/.clawdbot/skills
git clone <filesystem-skill-repo>

# Or install via ClawdHub
clawdhub install filesystem

# Make executable
chmod +x filesystem/filesystem
```

## Configuration

Customize behavior via `config.json`:

```json
{
  "defaultPath": "./",
  "maxDepth": 10,
  "defaultFilters": ["*"],
  "excludePatterns": ["node_modules", ".git", ".DS_Store"],
  "outputFormat": "table",
  "dateFormat": "YYYY-MM-DD HH:mm:ss",
  "sizeFormat": "human",
  "colorOutput": true
}
```

## Examples

### Development Workflow
```bash
# Find all JavaScript files in src
filesystem list --path ./src --recursive --filter "*.js" --details

# Search for TODO comments
filesystem search --pattern "TODO|FIXME" --path ./src --content --context 2

# Copy all logs to backup
filesystem copy --pattern "*.log" --to ./backup/logs/ --preserve

# Analyze project structure
filesystem tree --path ./ --depth 2 --size
```

### System Administration
```bash
# Find large files
filesystem analyze --path /var/log --sizes --largest 10

# List recent files
filesystem list --path /tmp --sort date --details

# Clean old temp files
filesystem list --path /tmp --filter "*.tmp" --older-than 7d
```

## Safety Features

- **Path Validation** - Prevents directory traversal attacks
- **Permission Checks** - Verifies read/write access before operations
- **Dry Run Mode** - Preview destructive operations
- **Backup Prompts** - Suggests backups before overwrites
- **Error Recovery** - Graceful handling of permission errors

## Integration

Works seamlessly with other Clawdbot tools:
- **Security Skill** - Validates all filesystem operations
- **Git Operations** - Respects .gitignore patterns
- **Backup Tools** - Integrates with backup workflows
- **Log Analysis** - Perfect for log file management

## Updates & Community

**Stay informed about the latest Clawdbot skills and filesystem tools:**

- 🐦 **Follow [@LexpertAI](https://x.com/LexpertAI)** on X for skill updates and releases
- 🛠️ **New filesystem features** and enhancements
- 📋 **Best practices** for file management automation
- 💡 **Tips and tricks** for productivity workflows

Get early access to new skills and improvements by following @LexpertAI for:
- **Skill announcements** and new releases
- **Performance optimizations** and feature updates
- **Integration examples** and workflow automation
- **Community discussions** on productivity tools

## License

MIT License - Free for personal and commercial use.

---

**Remember**: Great filesystem management starts with the right tools. This skill provides comprehensive operations while maintaining safety and performance.
