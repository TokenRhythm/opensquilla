#!/usr/bin/env python3
# ruff: noqa
"""
Web Search Tool

Search the web using DuckDuckGo's search API. Supports web search, news,
images, and videos with various output formats.

Requirements:
    duckduckgo-search, declared by the OpenSquilla project dependencies
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Any

try:
    from duckduckgo_search import DDGS
except ImportError as e:
    print(f"Error: Missing required dependency: {e}", file=sys.stderr)
    print("Sync or reinstall the OpenSquilla project dependencies.", file=sys.stderr)
    sys.exit(1)


class WebSearch:
    """Web search using DuckDuckGo."""

    def __init__(
        self,
        region: str = "wt-wt",
        safe_search: str = "moderate",
        timeout: int = 20,
    ):
        """
        Initialize the search client.

        Args:
            region: Region code (e.g., "us-en", "uk-en", "wt-wt" for worldwide)
            safe_search: Safe search setting ("on", "moderate", "off")
            timeout: Request timeout in seconds
        """
        self.region = region
        self.safe_search = safe_search
        self.timeout = timeout

    def search_text(
        self,
        query: str,
        max_results: int = 10,
        time_range: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Perform a text/web search.

        Args:
            query: Search query
            max_results: Maximum number of results (default: 10)
            time_range: Time filter ("d" day, "w" week, "m" month, "y" year)

        Returns:
            List of search results with title, href, and body
        """
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(
                    keywords=query,
                    region=self.region,
                    safesearch=self.safe_search,
                    timelimit=time_range,
                    max_results=max_results,
                ))
            return results
        except Exception as e:
            print(f"Error performing text search: {e}", file=sys.stderr)
            return []

    def search_news(
        self,
        query: str,
        max_results: int = 10,
        time_range: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for news articles.

        Args:
            query: Search query
            max_results: Maximum number of results
            time_range: Time filter ("d" day, "w" week, "m" month)

        Returns:
            List of news results with title, url, body, date, source
        """
        try:
            with DDGS() as ddgs:
                results = list(ddgs.news(
                    keywords=query,
                    region=self.region,
                    safesearch=self.safe_search,
                    timelimit=time_range,
                    max_results=max_results,
                ))
            return results
        except Exception as e:
            print(f"Error performing news search: {e}", file=sys.stderr)
            return []

    def search_images(
        self,
        query: str,
        max_results: int = 10,
        size: Optional[str] = None,
        color: Optional[str] = None,
        type_image: Optional[str] = None,
        layout: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for images.

        Args:
            query: Search query
            max_results: Maximum number of results
            size: Image size ("Small", "Medium", "Large", "Wallpaper")
            color: Color filter ("color", "Monochrome", "Red", "Orange", "Yellow",
                   "Green", "Blue", "Purple", "Pink", "Brown", "Black", "Gray", "Teal", "White")
            type_image: Image type ("photo", "clipart", "gif", "transparent", "line")
            layout: Layout ("Square", "Tall", "Wide")

        Returns:
            List of image results with title, image URL, thumbnail, source, etc.
        """
        try:
            with DDGS() as ddgs:
                results = list(ddgs.images(
                    keywords=query,
                    region=self.region,
                    safesearch=self.safe_search,
                    size=size,
                    color=color,
                    type_image=type_image,
                    layout=layout,
                    max_results=max_results,
                ))
            return results
        except Exception as e:
            print(f"Error performing image search: {e}", file=sys.stderr)
            return []

    def search_videos(
        self,
        query: str,
        max_results: int = 10,
        duration: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for videos.

        Args:
            query: Search query
            max_results: Maximum number of results
            duration: Video duration ("short", "medium", "long")
            resolution: Video resolution ("high", "standard")

        Returns:
            List of video results with title, content, description, publisher, etc.
        """
        try:
            with DDGS() as ddgs:
                results = list(ddgs.videos(
                    keywords=query,
                    region=self.region,
                    safesearch=self.safe_search,
                    duration=duration,
                    resolution=resolution,
                    max_results=max_results,
                ))
            return results
        except Exception as e:
            print(f"Error performing video search: {e}", file=sys.stderr)
            return []


def format_text_results(results: List[Dict[str, Any]], format_type: str = "text") -> str:
    """
    Format search results for display.

    Args:
        results: List of search results
        format_type: Output format ("text", "markdown", "json")

    Returns:
        Formatted string
    """
    if not results:
        return "No results found."

    if format_type == "json":
        return json.dumps(results, indent=2, ensure_ascii=False)

    elif format_type == "markdown":
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            url = result.get('href') or result.get('url', '')
            body = result.get('body') or result.get('description', '')

            output.append(f"## {i}. {title}\n")
            output.append(f"**URL:** {url}\n")
            if body:
                output.append(f"{body}\n")
            output.append("")
        return "\n".join(output)

    else:  # text format
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            url = result.get('href') or result.get('url', '')
            body = result.get('body') or result.get('description', '')

            output.append(f"{i}. {title}")
            output.append(f"   URL: {url}")
            if body:
                # Wrap body text
                output.append(f"   {body}")
            output.append("")
        return "\n".join(output)


def format_news_results(results: List[Dict[str, Any]], format_type: str = "text") -> str:
    """Format news search results."""
    if not results:
        return "No news results found."

    if format_type == "json":
        return json.dumps(results, indent=2, ensure_ascii=False)

    elif format_type == "markdown":
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            url = result.get('url', '')
            body = result.get('body', '')
            date = result.get('date', '')
            source = result.get('source', '')

            output.append(f"## {i}. {title}\n")
            if source:
                output.append(f"**Source:** {source}")
            if date:
                output.append(f"**Date:** {date}")
            output.append(f"**URL:** {url}\n")
            if body:
                output.append(f"{body}\n")
            output.append("")
        return "\n".join(output)

    else:  # text format
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            url = result.get('url', '')
            body = result.get('body', '')
            date = result.get('date', '')
            source = result.get('source', '')

            output.append(f"{i}. {title}")
            if source and date:
                output.append(f"   {source} - {date}")
            elif source:
                output.append(f"   {source}")
            elif date:
                output.append(f"   {date}")
            output.append(f"   URL: {url}")
            if body:
                output.append(f"   {body}")
            output.append("")
        return "\n".join(output)


def format_image_results(results: List[Dict[str, Any]], format_type: str = "text") -> str:
    """Format image search results."""
    if not results:
        return "No image results found."

    if format_type == "json":
        return json.dumps(results, indent=2, ensure_ascii=False)

    elif format_type == "markdown":
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            image_url = result.get('image', '')
            thumbnail = result.get('thumbnail', '')
            source = result.get('source', '')
            width = result.get('width', '')
            height = result.get('height', '')

            output.append(f"## {i}. {title}\n")
            if width and height:
                output.append(f"**Dimensions:** {width}x{height}")
            if source:
                output.append(f"**Source:** {source}")
            output.append(f"**Image URL:** {image_url}")
            if thumbnail:
                output.append(f"**Thumbnail:** {thumbnail}")
            output.append("")
        return "\n".join(output)

    else:  # text format
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            image_url = result.get('image', '')
            source = result.get('source', '')
            width = result.get('width', '')
            height = result.get('height', '')

            output.append(f"{i}. {title}")
            if width and height:
                output.append(f"   Dimensions: {width}x{height}")
            if source:
                output.append(f"   Source: {source}")
            output.append(f"   Image URL: {image_url}")
            output.append("")
        return "\n".join(output)


def format_video_results(results: List[Dict[str, Any]], format_type: str = "text") -> str:
    """Format video search results."""
    if not results:
        return "No video results found."

    if format_type == "json":
        return json.dumps(results, indent=2, ensure_ascii=False)

    elif format_type == "markdown":
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            url = result.get('content', '')
            description = result.get('description', '')
            publisher = result.get('publisher', '')
            duration = result.get('duration', '')
            published = result.get('published', '')

            output.append(f"## {i}. {title}\n")
            if publisher:
                output.append(f"**Publisher:** {publisher}")
            if duration:
                output.append(f"**Duration:** {duration}")
            if published:
                output.append(f"**Published:** {published}")
            output.append(f"**URL:** {url}\n")
            if description:
                output.append(f"{description}\n")
            output.append("")
        return "\n".join(output)

    else:  # text format
        output = []
        for i, result in enumerate(results, 1):
            title = result.get('title', 'No title')
            url = result.get('content', '')
            description = result.get('description', '')
            publisher = result.get('publisher', '')
            duration = result.get('duration', '')

            output.append(f"{i}. {title}")
            if publisher and duration:
                output.append(f"   {publisher} - {duration}")
            elif publisher:
                output.append(f"   {publisher}")
            output.append(f"   URL: {url}")
            if description:
                output.append(f"   {description}")
            output.append("")
        return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(
        description="Search the web using DuckDuckGo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic web search
  %(prog)s "python tutorials"

  # Search with more results
  %(prog)s "machine learning" --max-results 20

  # News search
  %(prog)s "climate change" --type news --time-range w

  # Image search
  %(prog)s "sunset photos" --type images --max-results 15

  # Save results to file
  %(prog)s "artificial intelligence" --output results.txt

  # JSON output format
  %(prog)s "quantum computing" --format json --output results.json

  # Region-specific search
  %(prog)s "local news" --region us-en --type news

Time range filters (--time-range):
  d = past day
  w = past week
  m = past month
  y = past year
        """
    )

    parser.add_argument(
        'query',
        help='Search query'
    )

    # Search options
    search_group = parser.add_argument_group('search options')
    search_group.add_argument(
        '-t', '--type',
        choices=['web', 'news', 'images', 'videos'],
        default='web',
        help='Search type (default: web)'
    )
    search_group.add_argument(
        '-n', '--max-results',
        type=int,
        default=10,
        help='Maximum number of results (default: 10)'
    )
    search_group.add_argument(
        '--time-range',
        choices=['d', 'w', 'm', 'y'],
        help='Time range filter (d=day, w=week, m=month, y=year)'
    )
    search_group.add_argument(
        '-r', '--region',
        default='wt-wt',
        help='Region code (e.g., us-en, uk-en, wt-wt for worldwide, default: wt-wt)'
    )
    search_group.add_argument(
        '--safe-search',
        choices=['on', 'moderate', 'off'],
        default='moderate',
        help='Safe search setting (default: moderate)'
    )

    # Image-specific options
    image_group = parser.add_argument_group('image search options')
    image_group.add_argument(
        '--image-size',
        choices=['Small', 'Medium', 'Large', 'Wallpaper'],
        help='Image size filter'
    )
    image_group.add_argument(
        '--image-color',
        choices=['color', 'Monochrome', 'Red', 'Orange', 'Yellow', 'Green',
                 'Blue', 'Purple', 'Pink', 'Brown', 'Black', 'Gray', 'Teal', 'White'],
        help='Image color filter'
    )
    image_group.add_argument(
        '--image-type',
        choices=['photo', 'clipart', 'gif', 'transparent', 'line'],
        help='Image type filter'
    )
    image_group.add_argument(
        '--image-layout',
        choices=['Square', 'Tall', 'Wide'],
        help='Image layout filter'
    )

    # Video-specific options
    video_group = parser.add_argument_group('video search options')
    video_group.add_argument(
        '--video-duration',
        choices=['short', 'medium', 'long'],
        help='Video duration filter'
    )
    video_group.add_argument(
        '--video-resolution',
        choices=['high', 'standard'],
        help='Video resolution filter'
    )

    # Output options
    output_group = parser.add_argument_group('output options')
    output_group.add_argument(
        '-f', '--format',
        choices=['text', 'markdown', 'json'],
        default='text',
        help='Output format (default: text)'
    )
    output_group.add_argument(
        '-o', '--output',
        help='Output file path (prints to stdout if not specified)'
    )

    args = parser.parse_args()

    # Initialize search client
    searcher = WebSearch(
        region=args.region,
        safe_search=args.safe_search,
    )

    # Perform search based on type
    print(f"Searching for: {args.query}", file=sys.stderr)
    print(f"Type: {args.type}, Max results: {args.max_results}", file=sys.stderr)
    if args.time_range:
        time_labels = {'d': 'past day', 'w': 'past week', 'm': 'past month', 'y': 'past year'}
        print(f"Time range: {time_labels[args.time_range]}", file=sys.stderr)
    print("", file=sys.stderr)

    results = []
    formatter = format_text_results

    if args.type == 'web':
        results = searcher.search_text(
            query=args.query,
            max_results=args.max_results,
            time_range=args.time_range,
        )
        formatter = format_text_results

    elif args.type == 'news':
        results = searcher.search_news(
            query=args.query,
            max_results=args.max_results,
            time_range=args.time_range,
        )
        formatter = format_news_results

    elif args.type == 'images':
        results = searcher.search_images(
            query=args.query,
            max_results=args.max_results,
            size=args.image_size,
            color=args.image_color,
            type_image=args.image_type,
            layout=args.image_layout,
        )
        formatter = format_image_results

    elif args.type == 'videos':
        results = searcher.search_videos(
            query=args.query,
            max_results=args.max_results,
            duration=args.video_duration,
            resolution=args.video_resolution,
        )
        formatter = format_video_results

    # Format results
    output = formatter(results, args.format)

    # Output results
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding='utf-8')
        print(f"✓ Results saved to {args.output}", file=sys.stderr)
        print(f"  Found {len(results)} result(s)", file=sys.stderr)
    else:
        print(output)
        print(f"\nFound {len(results)} result(s)", file=sys.stderr)


if __name__ == '__main__':
    main()
