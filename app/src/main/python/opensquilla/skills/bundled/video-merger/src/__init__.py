"""
Video Merger Library
~~~~~~~~~~~~~~~~~~~~
Multi-segment short video auto-merger tool.

Basic usage:
    >>> from video_merger import VideoMerger
    >>> merger = VideoMerger()
    >>> merger.merge(input_dir="./segments", output_path="./full.mp4")
"""

from .video_merger import VideoMerger

__version__ = "1.0.0"
__author__ = "machunlin"
__license__ = "MIT"
