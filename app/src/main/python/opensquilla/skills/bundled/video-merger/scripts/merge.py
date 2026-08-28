#!/usr/bin/env python3
"""
Video Merger Command Line Interface
视频拼接工具命令行入口
"""
import argparse
import os
import sys

# Force UTF-8 stdio so emoji prints in the underlying VideoMerger don't
# blow up on the Windows GBK console code page when the gateway captures
# stdout via subprocess pipes. Without this, the success "✅ 合并完成"
# print at the end of merge() raises UnicodeEncodeError AFTER final.mp4
# is already on disk, the wrapper returns exit 1, and the orchestrator
# treats a successful merge as a failure.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.video_merger import VideoMerger


def main():
    parser = argparse.ArgumentParser(
        description="Multi-segment short video auto-merger tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--input", "-i", required=True,
                        help="Directory containing segmented videos (filenames must start with numeric prefix)")
    parser.add_argument("--output", "-o", required=True,
                        help="Output video file path (full mode) or directory path (chunk mode)")
    parser.add_argument("--mode", "-m", default="full",
                        choices=["full", "chunk"],
                        help="Output mode: full (single complete video) / chunk (multiple segmented videos)")
    parser.add_argument("--chunk-duration", type=int, default=60,
                        help="Target duration per chunk in seconds (only for chunk mode)")
    parser.add_argument("--resolution", "-r",
                        help="Custom output resolution, e.g. 1080x1920, defaults to original resolution")
    parser.add_argument("--transition", "-t", type=float, default=0.5,
                        help="Fade in/out transition duration in seconds")
    parser.add_argument("--fps", type=int, default=24,
                        help="Output frame rate")
    parser.add_argument("--crf", type=int, default=22,
                        help="Video quality (0-51, lower = better quality)")
    parser.add_argument("--preset", default="medium",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
                        help="Encoding speed preset (faster = larger file, slower = better compression)")
    parser.add_argument("--ffmpeg-path", default="ffmpeg",
                        help="Custom path to ffmpeg executable")
    parser.add_argument("--ffprobe-path", default="ffprobe",
                        help="Custom path to ffprobe executable")

    args = parser.parse_args()

    try:
        merger = VideoMerger(ffmpeg_path=args.ffmpeg_path, ffprobe_path=args.ffprobe_path)

        if args.mode == "full":
            success = merger.merge(
                input_dir=args.input,
                output_path=args.output,
                resolution=args.resolution,
                transition_duration=args.transition,
                fps=args.fps,
                crf=args.crf,
                preset=args.preset
            )
        else: # chunk mode
            success = merger.merge_chunks(
                input_dir=args.input,
                output_dir=args.output,
                chunk_duration=args.chunk_duration,
                resolution=args.resolution,
                transition_duration=args.transition,
                fps=args.fps,
                crf=args.crf,
                preset=args.preset
            )

        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"[ERROR] {str(e)}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
