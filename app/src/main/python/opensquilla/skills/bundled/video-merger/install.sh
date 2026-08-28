#!/bin/bash
echo "安装 video-merger 技能依赖..."

# 检查并安装ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "正在安装ffmpeg..."
    if command -v brew &> /dev/null; then
        brew install ffmpeg
    elif command -v apt &> /dev/null; then
        sudo apt update && sudo apt install -y ffmpeg
    else
        echo "请手动安装ffmpeg：https://ffmpeg.org/download.html"
        exit 1
    fi
fi

# 检查python3
if ! command -v python3 &> /dev/null; then
    echo "错误：请先安装Python 3.8+"
    exit 1
fi

echo "✅ video-merger 技能依赖安装完成！"
