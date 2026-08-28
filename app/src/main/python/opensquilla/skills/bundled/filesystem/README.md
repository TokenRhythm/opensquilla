# 📁 Filesystem Management

Advanced filesystem operations for AI agents. Comprehensive file and directory operations with intelligent filtering, searching, and batch processing capabilities.

[![Version](https://img.shields.io/badge/version-1.0.0-blue)](https://clawdhub.com/unknown/clawdbot-filesystem)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Node.js](https://img.shields.io/badge/node-%3E%3D14.0.0-brightgreen)](https://nodejs.org/)

## 🚀 Features

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

## 📦 Installation

### Via ClawdHub (Recommended)

```bash
clawdhub install filesystem
```

### Manual Installation

```bash
# Clone the skill
git clone https://github.com/gtrusler/clawdbot-filesystem.git
cd clawdbot-filesystem

# Make executable
chmod +x filesystem

# Optional: Install globally
npm install -g .
```

## 🛠️ Usage

### Basic Commands

```bash
# List files in current directory
filesystem list

# List with details and filtering
filesystem list --path ./src --recursive --filter "*.js" --details

# Search for content
filesystem search --pattern "TODO" --path ./src --content

# Copy files safely
filesystem copy --pattern "*.log" --to ./backup/ --dry-run

# Show directory tree
filesystem tree --path ./ --depth 3

# Analyze directory
filesystem analyze --path ./logs --stats --largest 10
```

### Advanced Examples

#### Development Workflow
```bash
# Find all JavaScript files in project
filesystem list --path ./src --recursive --filter "*.js" --sort size

# Search for TODO comments with context
filesystem search --pattern "TODO|FIXME|HACK" --content --context 3

# Copy all configuration files
filesystem copy --pattern "*.config.*" --to ./backup/configs/ --preserve

# Analyze project structure
filesystem tree --depth 2 --size
```

#### System Administration
```bash
# Find large log files
filesystem analyze --path /var/log --sizes --largest 15

# Search for error patterns in logs
filesystem search --pattern "ERROR|FATAL" --path /var/log --content --include "*.log"

# List recent files
filesystem list --path /tmp --sort date --details

# Clean analysis before deletion
filesystem list --path /tmp --filter "*.tmp" --details
```

## ⚙️ Configuration

The skill uses a `config.json` file for default settings:

```json
{
  "defaultPath": "./",
  "maxDepth": 10,
  "excludePatterns": ["node_modules", ".git", ".DS_Store"],
  "outputFormat": "table",
  "colorOutput": true,
  "performance": {
    "maxFileSize": 52428800,
    "maxFiles": 10000
  },
  "safety": {
    "requireConfirmation": true,
    "preventSystemPaths": true
  }
}
```

## 📖 Command Reference

### `filesystem list`
List files and directories with advanced filtering.

| Option | Description | Default |
|--------|-------------|---------|
| `--path, -p` | Target directory | Current directory |
| `--recursive, -r` | Include subdirectories | false |
| `--filter, -f` | Filter by pattern | `*` |
| `--details, -d` | Show file details | false |
| `--sort, -s` | Sort by field | name |
| `--format` | Output format | table |

### `filesystem search`
Search files by name patterns or content.

| Option | Description | Default |
|--------|-------------|---------|
| `--pattern` | Search pattern | Required |
| `--path, -p` | Search directory | Current directory |
| `--content, -c` | Search file contents | false |
| `--context` | Context lines | 2 |
| `--include` | Include patterns | All files |
| `--exclude` | Exclude patterns | None |

### `filesystem copy`
Batch copy files with pattern matching.

| Option | Description | Default |
|--------|-------------|---------|
| `--pattern` | Source pattern | `*` |
| `--to` | Destination directory | Required |
| `--dry-run` | Preview only | false |
| `--overwrite` | Allow overwrites | false |
| `--preserve` | Preserve timestamps | false |

### `filesystem tree`
Display directory structure as a tree.

| Option | Description | Default |
|--------|-------------|---------|
| `--path, -p` | Root directory | Current directory |
| `--depth, -d` | Maximum depth | 3 |
| `--dirs-only` | Show directories only | false |
| `--size` | Show file sizes | false |
| `--no-color` | Disable colors | false |

### `filesystem analyze`
Analyze directory structure and statistics.

| Option | Description | Default |
|--------|-------------|---------|
| `--path, -p` | Target directory | Current directory |
| `--stats` | Show statistics | true |
| `--types` | Analyze file types | false |
| `--sizes` | Size distribution | false |
| `--largest` | Show N largest files | 10 |

## 🛡️ Safety Features

- **Path Validation** - Prevents directory traversal attacks
- **Permission Checks** - Verifies access before operations
- **Dry Run Mode** - Preview destructive operations
- **Protected Paths** - Blocks system directory access
- **Size Limits** - Prevents processing huge files
- **Timeout Protection** - Prevents infinite operations

## 🔧 Integration

### With Other Clawdbot Skills

```bash
# Use with security skill
security validate-command "filesystem list --path /etc"

# Pipe to analysis tools
filesystem list --format json | jq '.[] | select(.size > 1000000)'

# Integration with Git workflows
filesystem list --filter "*.js" --format json | git-analyze-changes
```

### Automation Examples

```bash
# Daily log analysis
filesystem analyze --path /var/log --stats --largest 5

# Code quality checks
filesystem search --pattern "TODO|FIXME" --content --path ./src

# Backup preparation
filesystem copy --pattern "*.config*" --to ./backup/$(date +%Y%m%d)/
```

## 🧪 Testing

Test the installation:

```bash
# Basic functionality
filesystem help
filesystem list --path . --details

# Search capabilities
echo "TODO: Test this function" > test.txt
filesystem search --pattern "TODO" --content

# Tree visualization
filesystem tree --depth 2 --size

# Analysis features
filesystem analyze --stats --types
```

## 🐛 Troubleshooting

### Common Issues

**Permission Denied**
```bash
# Check file permissions
ls -la filesystem
chmod +x filesystem
```

**Large Directory Performance**
```bash
# Use filtering to narrow scope
filesystem list --filter "*.log" --exclude "node_modules/*"

# Limit depth for tree operations
filesystem tree --depth 2
```

**Memory Issues with Large Files**
```bash
# Files larger than 50MB are skipped by default
# Check current limits
node -e "console.log(require('./config.json').performance)"
```

## 📈 Performance Tips

- Use `--filter` to narrow file scope
- Set appropriate `--depth` limits for tree operations
- Enable exclusion patterns for common build directories
- Use `--dry-run` first for batch operations
- Monitor output with `--stats` for large directories

## 🤝 Contributing

1. **Report Issues** - Submit bugs and feature requests
2. **Add Patterns** - Contribute common file patterns
3. **Performance** - Submit optimization improvements
4. **Documentation** - Help improve examples and guides

## 📄 License

MIT License - Free for personal and commercial use.

See [LICENSE](LICENSE) file for details.

## 🔗 Links

- **ClawdHub** - [clawdhub.com/unknown/clawdbot-filesystem](https://clawdhub.com/unknown/clawdbot-filesystem)
- **Issues** - [GitHub Issues](https://github.com/gtrusler/clawdbot-filesystem/issues)
- **Documentation** - [Clawdbot Docs](https://docs.clawd.bot)

## 📢 Updates & Community

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

---

**Built with ❤️ for the Clawdbot community**