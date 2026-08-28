"""
Video Merger Core Library
自动拼接多个分段短视频为完整长视频的核心功能
"""
import os
import re
import shutil
import subprocess
import tempfile
from glob import glob

# Common Windows install locations to probe when PATH inheritance fails
# (the gateway can launch this skill as a subprocess on a shell whose PATH
# does not include the user-level winget/scoop/choco bin folders).
_WINGET_FFMPEG_GLOB = (
    "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_*/"
    "ffmpeg-*-full_build/bin"
)


def _resolve_ffmpeg_binary(explicit: str, tool: str) -> str:
    """Return a usable path for ``tool`` (ffmpeg / ffprobe), falling back to
    common Windows install locations if PATH lookup fails. ``explicit`` is the
    caller-supplied path; if it already resolves on PATH or is absolute, it's
    returned unchanged."""
    found = shutil.which(explicit)
    if found:
        return found
    if os.path.isabs(explicit) and os.path.isfile(explicit):
        return explicit
    if os.name != "nt":
        return explicit  # surface the canonical "not found" error from subprocess
    suffix = ".exe" if not tool.endswith(".exe") else ""
    target = f"{tool}{suffix}"
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        for bin_dir in glob(os.path.join(local_app, _WINGET_FFMPEG_GLOB)):
            candidate = os.path.join(bin_dir, target)
            if os.path.isfile(candidate):
                return candidate
    user_profile = os.environ.get("USERPROFILE", "")
    extra = [
        os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current", "bin", target),
        rf"C:\ProgramData\chocolatey\bin\{target}",
        rf"C:\Program Files\ffmpeg\bin\{target}",
        rf"C:\ffmpeg\bin\{target}",
    ]
    for path in extra:
        if path and os.path.isfile(path):
            return path
    return explicit  # let subprocess raise the canonical "not found" error


class VideoMerger:
    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe"):
        """
        初始化视频拼接器
        :param ffmpeg_path: ffmpeg可执行文件路径
        :param ffprobe_path: ffprobe可执行文件路径
        """
        self.ffmpeg_path = _resolve_ffmpeg_binary(ffmpeg_path, "ffmpeg")
        self.ffprobe_path = _resolve_ffmpeg_binary(ffprobe_path, "ffprobe")
        self._check_dependencies()

    def _check_dependencies(self):
        """检查依赖是否安装"""
        for tool in [self.ffmpeg_path, self.ffprobe_path]:
            try:
                subprocess.run([tool, "-version"], capture_output=True, check=True)
            except Exception as e:
                raise RuntimeError(f"未找到{tool}，请先安装ffmpeg：https://ffmpeg.org/download.html") from e

    def get_sorted_videos(self, input_dir: str) -> list[str]:
        """
        按文件名数字前缀排序获取视频列表
        :param input_dir: 分镜头视频所在目录
        :return: 排序后的视频路径列表
        """
        videos = []
        for f in os.listdir(input_dir):
            if f.lower().endswith(".mp4") and re.match(r"^\d+_", f):
                videos.append(os.path.join(input_dir, f))

        if not videos:
            raise ValueError(f"目录 {input_dir} 下未找到符合命名规则（数字_开头）的MP4文件")

        # 按数字序号排序
        videos.sort(key=lambda x: int(re.match(r"^(\d+)_", os.path.basename(x)).group(1)))
        return videos

    def get_video_info(self, video_path: str) -> tuple[int, int, float]:
        """
        获取视频信息：宽度、高度、时长
        :param video_path: 视频路径
        :return: (width, height, duration)
        """
        cmd = [
            self.ffprobe_path, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        width, height, duration = result.stdout.strip().split("\n")[:3]
        return int(width), int(height), float(duration)

    def merge(self,
              input_dir: str,
              output_path: str,
              resolution: str | None = None,
              transition_duration: float = 0.5,
              fps: int = 24,
              crf: int = 22,
              preset: str = "medium") -> bool:
        """
        拼接视频
        :param input_dir: 分镜头视频所在目录
        :param output_path: 输出视频路径
        :param resolution: 自定义分辨率，如"1080x1920"，默认保持原始分辨率
        :param transition_duration: 淡入淡出转场时长（秒）
        :param fps: 输出帧率，默认24
        :param crf: 视频质量参数，0-51，越小质量越高，默认22
        :param preset: 编码速度预设，ultrafast/superfast/veryfast/faster/fast/medium/slow/slower/veryslow，默认medium
        :return: 是否成功
        """
        video_list = self.get_sorted_videos(input_dir)
        total_segments = len(video_list)
        print(f"找到 {total_segments} 个视频片段，已按序号排序完成")

        # 获取原始分辨率
        if not resolution:
            width, height, _ = self.get_video_info(video_list[0])
            resolution = f"{width}x{height}"
            print(f"使用原始分辨率：{resolution}")
        else:
            print(f"使用自定义分辨率：{resolution}")

        # 生成concat列表
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            for v in video_list:
                f.write(f"file '{os.path.abspath(v)}'\n")
            concat_file = f.name

        try:
            # 先无损拼接所有片段
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
                temp_raw = f.name

            cmd_concat = [
                self.ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                temp_raw
            ]
            print("正在拼接视频片段...")
            subprocess.run(cmd_concat, capture_output=True, check=True)

            # 获取总时长
            _, _, total_duration = self.get_video_info(temp_raw)
            print(f"总时长：{int(total_duration/60)}分{int(total_duration%60)}秒")

            # 统一参数+添加转场
            print("正在编码和添加转场效果...")
            cmd_final = [
                self.ffmpeg_path, "-y", "-i", temp_raw,
                "-vf", (f"scale={resolution},fps={fps},format=yuv420p,"
                        f"fade=t=in:st=0:d={transition_duration},"
                        f"fade=t=out:st={total_duration-transition_duration}:d={transition_duration}"),
                "-af", (f"afade=t=in:st=0:d={transition_duration},"
                        f"afade=t=out:st={total_duration-transition_duration}:d={transition_duration}"),
                "-c:v", "h264", "-crf", str(crf), "-preset", preset,
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                output_path
            ]
            subprocess.run(cmd_final, capture_output=True, check=True)

            # 验证输出文件
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                file_size = os.path.getsize(output_path) / 1024 / 1024
                print(f"[OK] 合并完成！输出文件：{output_path}")
                print(f"文件大小：{file_size:.1f}MB")
                return True
            else:
                raise RuntimeError("输出文件生成失败")

        finally:
            # 清理临时文件
            os.unlink(concat_file)
            if os.path.exists(temp_raw):
                os.unlink(temp_raw)

        return False

    def merge_chunks(self,
                     input_dir: str,
                     output_dir: str,
                     chunk_duration: int = 60,
                     resolution: str | None = None,
                     transition_duration: float = 0.5,
                     fps: int = 24,
                     crf: int = 22,
                     preset: str = "medium") -> bool:
        """
        分块拼接视频
        :param input_dir: 分镜头视频所在目录
        :param output_dir: 输出视频目录
        :param chunk_duration: 每块目标时长（秒）
        :param resolution: 自定义分辨率，如"1080x1920"，默认保持原始分辨率
        :param transition_duration: 淡入淡出转场时长（秒）
        :param fps: 输出帧率，默认24
        :param crf: 视频质量参数，0-51，越小质量越高，默认22
        :param preset: 编码速度预设，ultrafast/superfast/veryfast/faster/fast/medium/slow/slower/veryslow，默认medium
        :return: 是否成功
        """
        video_list = self.get_sorted_videos(input_dir)
        total_segments = len(video_list)
        print(f"找到 {total_segments} 个视频片段，已按序号排序完成")

        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)

        # 获取原始分辨率
        if not resolution:
            width, height, _ = self.get_video_info(video_list[0])
            resolution = f"{width}x{height}"
            print(f"使用原始分辨率：{resolution}")
        else:
            print(f"使用自定义分辨率：{resolution}")

        # 计算每个分块包含的片段
        current_chunk = []
        current_chunk_duration = 0.0
        chunk_index = 1

        for video_path in video_list:
            _, _, duration = self.get_video_info(video_path)
            if current_chunk_duration + duration > chunk_duration and current_chunk:
                # 输出当前分块
                self._merge_single_chunk(
                    current_chunk,
                    os.path.join(output_dir, f"chunk_{chunk_index:03d}.mp4"),
                    resolution,
                    transition_duration,
                    fps,
                    crf,
                    preset
                )
                chunk_index += 1
                current_chunk = []
                current_chunk_duration = 0.0
            current_chunk.append(video_path)
            current_chunk_duration += duration

        # 处理最后一个分块
        if current_chunk:
            self._merge_single_chunk(
                current_chunk,
                os.path.join(output_dir, f"chunk_{chunk_index:03d}.mp4"),
                resolution,
                transition_duration,
                fps,
                crf,
                preset
            )

        print(f"[OK] 分块合并完成！共生成 {chunk_index} 个分块，保存在：{output_dir}")
        return True

    def _merge_single_chunk(self,
                            video_list: list[str],
                            output_path: str,
                            resolution: str,
                            transition_duration: float = 0.5,
                            fps: int = 24,
                            crf: int = 22,
                            preset: str = "medium") -> bool:
        """
        合并单个分块
        """
        # 生成concat列表
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            for v in video_list:
                f.write(f"file '{os.path.abspath(v)}'\n")
            concat_file = f.name

        try:
            # 先无损拼接所有片段
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
                temp_raw = f.name

            cmd_concat = [
                self.ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", concat_file,
                "-c", "copy",
                temp_raw
            ]
            print(f"正在拼接分块 {os.path.basename(output_path)}，包含 {len(video_list)} 个片段...")
            subprocess.run(cmd_concat, capture_output=True, check=True)

            # 获取分块时长
            _, _, chunk_duration = self.get_video_info(temp_raw)

            # 统一参数+添加转场
            cmd_final = [
                self.ffmpeg_path, "-y", "-i", temp_raw,
                "-vf", (f"scale={resolution},fps={fps},format=yuv420p,"
                        f"fade=t=in:st=0:d={transition_duration},"
                        f"fade=t=out:st={chunk_duration-transition_duration}:d={transition_duration}"),
                "-af", (f"afade=t=in:st=0:d={transition_duration},"
                        f"afade=t=out:st={chunk_duration-transition_duration}:d={transition_duration}"),
                "-c:v", "h264", "-crf", str(crf), "-preset", preset,
                "-c:a", "aac", "-ar", "44100", "-ac", "2",
                output_path
            ]
            subprocess.run(cmd_final, capture_output=True, check=True)

            return True

        finally:
            # 清理临时文件
            os.unlink(concat_file)
            if os.path.exists(temp_raw):
                os.unlink(temp_raw)
