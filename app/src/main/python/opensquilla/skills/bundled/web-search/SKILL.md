---
name: web-search
description: This skill should be used when users need to search the web for information, find current content, look up news articles, search for images, or find videos. It uses DuckDuckGo's search API to return results in clean, formatted output (text, markdown, or JSON). Use for research, fact-checking, finding recent information, or gathering web resources.
description_zh: "当用户需要在网上搜索信息、查找最新内容、查阅新闻文章、搜索图片或查找视频时使用。它使用DuckDuckGo的搜索API，以清晰、格式化的输出（文本、markdown或JSON）返回结果。适用于研究、事实核查、查找近期信息或收集网络资源。"
homepage: https://clawhub.ai/billyutw/web-search
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/billyutw/web-search
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: medium
    capabilities: [network-read]
entrypoint:
  command: python {baseDir}/scripts/search.py
  args:
    - "{{ with.query | default(inputs.user_message) }}"
    - --type
    - "{{ with.type | default('web') }}"
    - --max-results
    - "{{ with.max_results | default('10') }}"
    - --format
    - "{{ with.format | default('text') }}"
  parse: text
  timeout: 45
---

# Web Search

## Overview

Search the web using DuckDuckGo's API to find information across web pages, news articles, images, and videos. Returns results in multiple formats (text, markdown, JSON) with filtering options for time range, region, and safe search.

## When to Use This Skill

Use this skill when users request:
- Web searches for information or resources
- Finding current or recent information online
- Looking up news articles about specific topics
- Searching for images by description or topic
- Finding videos on specific subjects
- Research requiring current web data
- Fact-checking or verification using web sources
- Gathering URLs and resources on a topic

## Prerequisites

OpenSquilla declares the required `duckduckgo-search` Python package as a runtime dependency.
For source checkouts, run the normal project dependency sync before invoking this bundled skill.
The library provides a simple Python interface to DuckDuckGo's search API without requiring API keys or authentication.

## Core Capabilities

### 1. Basic Web Search

Search for web pages and information:

```bash
python {baseDir}/scripts/search.py "<query>"
```

**Example:**
```bash
python {baseDir}/scripts/search.py "python asyncio tutorial"
```

Returns the top 10 web results with titles, URLs, and descriptions in a clean text format.

### 2. Limiting Results

Control the number of results returned:

```bash
python {baseDir}/scripts/search.py "<query>" --max-results <N>
```

**Example:**
```bash
python {baseDir}/scripts/search.py "machine learning frameworks" --max-results 20
```

Useful for:
- Getting more comprehensive results (increase limit)
- Quick lookups with fewer results (decrease limit)
- Balancing detail vs. processing time

### 3. Time Range Filtering

Filter results by recency:

```bash
python {baseDir}/scripts/search.py "<query>" --time-range <d|w|m|y>
```

**Time range options:**
- `d` - Past day
- `w` - Past week
- `m` - Past month
- `y` - Past year

**Example:**
```bash
python {baseDir}/scripts/search.py "artificial intelligence news" --time-range w
```

Great for:
- Finding recent news or updates
- Filtering out outdated content
- Tracking recent developments

### 4. News Search

Search specifically for news articles:

```bash
python {baseDir}/scripts/search.py "<query>" --type news
```

**Example:**
```bash
python {baseDir}/scripts/search.py "climate change" --type news --time-range w --max-results 15
```

News results include:
- Article title
- Source publication
- Publication date
- URL
- Article summary/description

### 5. Image Search

Search for images:

```bash
python {baseDir}/scripts/search.py "<query>" --type images
```

**Example:**
```bash
python {baseDir}/scripts/search.py "sunset over mountains" --type images --max-results 20
```

**Image filtering options:**

Size filters:
```bash
python {baseDir}/scripts/search.py "landscape photos" --type images --image-size Large
```
Options: `Small`, `Medium`, `Large`, `Wallpaper`

Color filters:
```bash
python {baseDir}/scripts/search.py "abstract art" --type images --image-color Blue
```
Options: `color`, `Monochrome`, `Red`, `Orange`, `Yellow`, `Green`, `Blue`, `Purple`, `Pink`, `Brown`, `Black`, `Gray`, `Teal`, `White`

Type filters:
```bash
python {baseDir}/scripts/search.py "icons" --type images --image-type transparent
```
Options: `photo`, `clipart`, `gif`, `transparent`, `line`

Layout filters:
```bash
python {baseDir}/scripts/search.py "wallpapers" --type images --image-layout Wide
```
Options: `Square`, `Tall`, `Wide`

Image results include:
- Image title
- Image URL (direct link to image)
- Thumbnail URL
- Source website
- Dimensions (width x height)

### 6. Video Search

Search for videos:

```bash
python {baseDir}/scripts/search.py "<query>" --type videos
```

**Example:**
```bash
python {baseDir}/scripts/search.py "python tutorial" --type videos --max-results 15
```

**Video filtering options:**

Duration filters:
```bash
python {baseDir}/scripts/search.py "cooking recipes" --type videos --video-duration short
```
Options: `short`, `medium`, `long`

Resolution filters:
```bash
python {baseDir}/scripts/search.py "documentary" --type videos --video-resolution high
```
Options: `high`, `standard`

Video results include:
- Video title
- Publisher/channel
- Duration
- Publication date
- Video URL
- Description

### 7. Region-Specific Search

Search with region-specific results:

```bash
python {baseDir}/scripts/search.py "<query>" --region <region-code>
```

**Common region codes:**
- `us-en` - United States (English)
- `uk-en` - United Kingdom (English)
- `ca-en` - Canada (English)
- `au-en` - Australia (English)
- `de-de` - Germany (German)
- `fr-fr` - France (French)
- `wt-wt` - Worldwide (default)

**Example:**
```bash
python {baseDir}/scripts/search.py "local news" --region us-en --type news
```

### 8. Safe Search Control

Control safe search filtering:

```bash
python {baseDir}/scripts/search.py "<query>" --safe-search <on|moderate|off>
```

**Options:**
- `on` - Strict filtering
- `moderate` - Balanced filtering (default)
- `off` - No filtering

**Example:**
```bash
python {baseDir}/scripts/search.py "medical information" --safe-search on
```

### 9. Output Formats

Choose how results are formatted:

**Text format (default):**
```bash
python {baseDir}/scripts/search.py "quantum computing"
```

Clean, readable plain text with numbered results.

**Markdown format:**
```bash
python {baseDir}/scripts/search.py "quantum computing" --format markdown
```

Formatted markdown with headers, bold text, and links.

**JSON format:**
```bash
python {baseDir}/scripts/search.py "quantum computing" --format json
```

Structured JSON data for programmatic processing.

### 10. Saving Results to File

Save search results to a file:

```bash
python {baseDir}/scripts/search.py "<query>" --output <file-path>
```

**Example:**
```bash
python {baseDir}/scripts/search.py "artificial intelligence" --output ai_results.txt
python {baseDir}/scripts/search.py "AI news" --type news --format markdown --output ai_news.md
python {baseDir}/scripts/search.py "AI research" --format json --output ai_data.json
```

The file format is determined by the `--format` flag, not the file extension.

## Output Format Examples

### Text Format
```
1. Page Title Here
   URL: https://example.com/page
   Brief description of the page content...

2. Another Result
   URL: https://example.com/another
   Another description...
```

### Markdown Format
```markdown
## 1. Page Title Here

**URL:** https://example.com/page

Brief description of the page content...

## 2. Another Result

**URL:** https://example.com/another

Another description...
```

### JSON Format
```json
[
  {
    "title": "Page Title Here",
    "href": "https://example.com/page",
    "body": "Brief description of the page content..."
  },
  {
    "title": "Another Result",
    "href": "https://example.com/another",
    "body": "Another description..."
  }
]
```

## Common Usage Patterns

### Research on a Topic

Gather comprehensive information about a subject:

```bash
# Get overview from web
python {baseDir}/scripts/search.py "machine learning basics" --max-results 15 --output ml_web.txt

# Get recent news
python {baseDir}/scripts/search.py "machine learning" --type news --time-range m --output ml_news.txt

# Find tutorial videos
python {baseDir}/scripts/search.py "machine learning tutorial" --type videos --max-results 10 --output ml_videos.txt
```

### Current Events Monitoring

Track news on specific topics:

```bash
python {baseDir}/scripts/search.py "climate summit" --type news --time-range d --format markdown --output daily_climate_news.md
```

### Finding Visual Resources

Search for images with specific criteria:

```bash
python {baseDir}/scripts/search.py "data visualization examples" --type images --image-type photo --image-size Large --max-results 25 --output viz_images.txt
```

### Fact-Checking

Verify information with recent sources:

```bash
python {baseDir}/scripts/search.py "specific claim to verify" --time-range w --max-results 20
```

### Academic Research

Find resources on scholarly topics:

```bash
python {baseDir}/scripts/search.py "quantum entanglement research" --time-range y --max-results 30 --output quantum_research.txt
```

### Market Research

Gather information about products or companies:

```bash
python {baseDir}/scripts/search.py "electric vehicle market 2025" --max-results 20 --format markdown --output ev_market.md
python {baseDir}/scripts/search.py "EV news" --type news --time-range m --output ev_news.txt
```

## Implementation Approach

When users request web searches:

1. **Identify search intent**:
   - What type of content (web, news, images, videos)?
   - How recent should results be?
   - How many results are needed?
   - Any filtering requirements?

2. **Configure search parameters**:
   - Choose appropriate search type (`--type`)
   - Set time range if currency matters (`--time-range`)
   - Adjust result count (`--max-results`)
   - Apply filters (image size, video duration, etc.)

3. **Select output format**:
   - Text for quick reading
   - Markdown for documentation
   - JSON for further processing

4. **Execute search**:
   - Run the search command
   - Save to file if results need to be preserved
   - Print to stdout for immediate review

5. **Process results**:
   - Read saved files if needed
   - Extract URLs or specific information
   - Combine results from multiple searches

## Quick Reference

**Command structure:**
```bash
python {baseDir}/scripts/search.py "<query>" [options]
```

**Essential options:**
- `-t, --type` - Search type (web, news, images, videos)
- `-n, --max-results` - Maximum results (default: 10)
- `--time-range` - Time filter (d, w, m, y)
- `-r, --region` - Region code (e.g., us-en, uk-en)
- `--safe-search` - Safe search level (on, moderate, off)
- `-f, --format` - Output format (text, markdown, json)
- `-o, --output` - Save to file

**Image-specific options:**
- `--image-size` - Size filter (Small, Medium, Large, Wallpaper)
- `--image-color` - Color filter
- `--image-type` - Type filter (photo, clipart, gif, transparent, line)
- `--image-layout` - Layout filter (Square, Tall, Wide)

**Video-specific options:**
- `--video-duration` - Duration filter (short, medium, long)
- `--video-resolution` - Resolution filter (high, standard)

**Get full help:**
```bash
python {baseDir}/scripts/search.py --help
```

## Best Practices

1. **Be specific** - Use clear, specific search queries for better results
2. **Use time filters** - Apply `--time-range` for current information
3. **Adjust result count** - Start with 10-20 results, increase if needed
4. **Save important searches** - Use `--output` to preserve results
5. **Choose appropriate type** - Use news search for current events, web for general info
6. **Use JSON for automation** - JSON format is easiest to parse programmatically
7. **Respect usage** - Don't hammer the API with rapid repeated searches

## Troubleshooting

**Common issues:**

- **"Missing required dependency"**: Sync or reinstall the OpenSquilla project dependencies
- **No results found**: Try broader search terms or remove time filters
- **Timeout errors**: The search service may be temporarily unavailable; retry after a moment
- **Rate limiting**: Space out searches if making many requests
- **Unexpected results**: DuckDuckGo's results may differ from Google; try refining the query

**Limitations:**

- Results quality depends on DuckDuckGo's index and algorithms
- No advanced search operators (unlike Google's site:, filetype:, etc.)
- Image and video searches may have fewer results than web search
- No control over result ranking or relevance scoring
- Some specialized searches may work better on dedicated search engines

## Advanced Use Cases

### Combining Multiple Searches

Gather comprehensive information by combining search types:

```bash
# Web overview
python {baseDir}/scripts/search.py "topic" --max-results 15 --output topic_web.txt

# Recent news
python {baseDir}/scripts/search.py "topic" --type news --time-range w --output topic_news.txt

# Images
python {baseDir}/scripts/search.py "topic" --type images --max-results 20 --output topic_images.txt
```

### Programmatic Processing

Use JSON output for automated processing:

```bash
python {baseDir}/scripts/search.py "research topic" --format json --output results.json
# Then process with another script
python analyze_results.py results.json
```

### Building a Knowledge Base

Create searchable documentation from web results:

```bash
# Search multiple related topics
python {baseDir}/scripts/search.py "topic1" --format markdown --output kb/topic1.md
python {baseDir}/scripts/search.py "topic2" --format markdown --output kb/topic2.md
python {baseDir}/scripts/search.py "topic3" --format markdown --output kb/topic3.md
```

## Resources

### scripts/search.py

The main search tool implementing DuckDuckGo search functionality. Key features:

- **Multiple search types** - Web, news, images, and videos
- **Flexible filtering** - Time range, region, safe search, and type-specific filters
- **Multiple output formats** - Text, Markdown, and JSON
- **File output** - Save results for later processing
- **Clean formatting** - Human-readable output with all essential information
- **Error handling** - Graceful handling of network errors and empty results

The script can be executed directly and includes comprehensive command-line help via `--help`.
