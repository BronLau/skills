#!/usr/bin/env python3
"""Shared local-media validation and compression guidance for Qwen requests."""

from __future__ import annotations

import json
import math
import mimetypes
import shlex
import subprocess
from pathlib import Path
from typing import Any


API_MAX_DATA_URL_BYTES = 10 * 1024 * 1024
DEFAULT_INLINE_LIMIT_MB = 9.5
TARGET_COMPRESSED_BYTES = int(6.5 * 1024 * 1024)
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/gif",
}
SAMPLE_FRACTIONS = (0.1, 0.3, 0.5, 0.7, 0.9)
SAMPLE_TIME_OFFSETS = (-0.08, 0.0, 0.08)


class MediaPreflightError(RuntimeError):
    pass


def estimated_data_url_size(path: Path, mime_type: str | None = None) -> int:
    prefix_size = len(f"data:{mime_type};base64,") if mime_type else 128
    size = path.stat().st_size
    return prefix_size + 4 * ((size + 2) // 3)


def probe_video_signature(path: Path) -> dict[str, object]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,start_time:stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        metadata = json.loads(completed.stdout)
        duration = float(metadata["format"]["duration"])
        start_time = float(metadata["format"].get("start_time") or 0.0)
        streams = metadata.get("streams", [])
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        if video_stream is None:
            raise MediaPreflightError(f"文件中没有视频流：{path}")
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
        rotation = int((video_stream.get("tags") or {}).get("rotate") or 0)
        for side_data in video_stream.get("side_data_list") or []:
            if side_data.get("rotation") is not None:
                rotation = int(side_data["rotation"])
                break
        if abs(rotation) % 180 == 90:
            width, height = height, width
    except MediaPreflightError:
        raise
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise MediaPreflightError(f"ffprobe 无法读取视频元数据：{path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise MediaPreflightError(f"视频时长无效：{path}")
    if not math.isfinite(start_time):
        raise MediaPreflightError(f"视频起始时间无效：{path}")
    if width <= 0 or height <= 0:
        raise MediaPreflightError(f"视频宽高无效：{path}")
    return {
        "duration": duration,
        "start_time": start_time,
        "width": width,
        "height": height,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def build_compression_command(
    source_video: Path,
    signature: dict[str, object],
) -> str:
    duration = float(signature["duration"])
    total_kbps = int(TARGET_COMPRESSED_BYTES * 8 * 0.94 / duration / 1000)
    has_audio = bool(signature["has_audio"])
    audio_kbps = min(64, max(16, int(total_kbps * 0.2))) if has_audio else 0
    video_kbps = total_kbps - audio_kbps
    if video_kbps < 32:
        raise MediaPreflightError(
            "视频过长，无法在 10 MiB Base64 限制内同时保留可用画面和音频。"
            "请缩短参考视频后再运行。"
        )
    output_height = 480 if video_kbps >= 350 else 360 if video_kbps >= 120 else 240
    compressed = source_video.with_name(f"{source_video.stem}_compressed.mp4")
    target_duration = min(duration, math.ceil(duration) - 0.001)
    audio_options = (
        f"-map 0:a:0 -c:a aac -b:a {audio_kbps}k -ac 1" if has_audio else "-an"
    )
    return (
        f"ffmpeg -i {shlex.quote(str(source_video))} "
        f"-map 0:v:0 {audio_options} -vf scale=-2:{output_height} "
        f"-c:v libx264 -b:v {video_kbps}k -maxrate {video_kbps}k "
        f"-bufsize {video_kbps * 2}k -t {target_duration:.3f} -shortest "
        f"{shlex.quote(str(compressed))}"
    )


def validate_image_input(path: Path, label: str, inline_limit_mb: float) -> None:
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise MediaPreflightError(
            f"{label}格式不支持：{path.name}，识别到 {mime_type or '未知'}"
        )
    encoded_size = estimated_data_url_size(path, mime_type)
    limit = min(int(inline_limit_mb * 1024 * 1024), API_MAX_DATA_URL_BYTES)
    if encoded_size >= limit:
        raise MediaPreflightError(
            f"{label} Base64 Data URL 超过 9.5 MiB 安全阈值"
            "（接口要求严格小于 10 MiB）："
            f"{encoded_size / 1024**2:.2f} MiB。请先压缩到约 7 MiB 以下。"
        )
    try:
        from PIL import Image
    except ImportError as exc:
        raise MediaPreflightError(
            "图片预检需要 Pillow：python3 -m pip install Pillow"
        ) from exc
    try:
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
    except Exception as exc:
        raise MediaPreflightError(f"{label}无法读取：{path}：{exc}") from exc
    if width <= 10 or height <= 10:
        raise MediaPreflightError(f"{label}宽高必须都大于 10 像素：{width}x{height}")
    if max(width / height, height / width) > 200:
        raise MediaPreflightError(f"{label}宽高比不能超过 200:1：{width}x{height}")


def _normalized_gray(frame: Any) -> Any:
    import cv2
    import numpy as np

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    array = gray.astype(np.float32)
    std = float(array.std())
    return (array - float(array.mean())) / max(std, 1.0)


def _read_frame(cap: Any, seconds: float) -> Any:
    import cv2

    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, seconds) * 1000.0)
    ok, frame = cap.read()
    if not ok:
        raise MediaPreflightError(f"无法读取视频 {seconds:.3f} 秒处的校验帧。")
    return _normalized_gray(frame)


def validate_video_content_similarity(
    source_video: Path,
    analysis_video: Path,
    duration: float,
) -> None:
    if source_video == analysis_video:
        return
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise MediaPreflightError("视频内容校验需要 OpenCV 与 NumPy。") from exc

    source_cap = cv2.VideoCapture(str(source_video))
    analysis_cap = cv2.VideoCapture(str(analysis_video))
    if not source_cap.isOpened() or not analysis_cap.isOpened():
        source_cap.release()
        analysis_cap.release()
        raise MediaPreflightError("无法打开原片或压缩分析视频进行内容校验。")
    scores: list[float] = []
    try:
        for fraction in SAMPLE_FRACTIONS:
            timestamp = duration * fraction
            source_frame = _read_frame(source_cap, timestamp)
            candidates = []
            for offset in SAMPLE_TIME_OFFSETS:
                candidate_time = timestamp + offset
                if not 0 <= candidate_time < duration:
                    continue
                try:
                    candidates.append(_read_frame(analysis_cap, candidate_time))
                except MediaPreflightError:
                    continue
            if not candidates:
                raise MediaPreflightError(
                    f"无法读取压缩分析视频 {timestamp:.3f} 秒附近的校验帧。"
                )
            score = max(
                float(np.mean(source_frame * candidate)) for candidate in candidates
            )
            scores.append(score)
    finally:
        source_cap.release()
        analysis_cap.release()

    median = float(np.median(scores))
    matching = sum(score >= 0.60 for score in scores)
    if median < 0.72 or matching < 4:
        raise MediaPreflightError(
            "压缩分析视频与原片画面内容不一致："
            f"median_similarity={median:.3f}, matching_samples={matching}/5"
        )


def validate_analysis_video(
    source_video: Path,
    analysis_video: Path,
    inline_limit_mb: float,
) -> None:
    source_signature = probe_video_signature(source_video)
    encoded_size = estimated_data_url_size(analysis_video)
    limit = min(int(inline_limit_mb * 1024 * 1024), API_MAX_DATA_URL_BYTES)
    if encoded_size >= limit:
        command = build_compression_command(source_video, source_signature)
        compressed = source_video.with_name(f"{source_video.stem}_compressed.mp4")
        raise MediaPreflightError(
            "分析视频 Base64 Data URL 超过 9.5 MiB 安全阈值"
            "（接口要求严格小于 10 MiB）："
            f"{encoded_size / 1024**2:.2f} MiB。请先压缩：\n{command}\n"
            "压缩完成后保持 --video 指向原片，并增加 "
            f"--analysis-video {shlex.quote(str(compressed))}。"
        )
    analysis_signature = (
        source_signature
        if analysis_video == source_video
        else probe_video_signature(analysis_video)
    )
    source_duration = float(source_signature["duration"])
    analysis_duration = float(analysis_signature["duration"])
    if abs(source_duration - analysis_duration) > 0.1:
        raise MediaPreflightError(
            "压缩分析视频与原片时长不一致："
            f"{analysis_duration:.3f} != {source_duration:.3f} 秒"
        )
    if math.ceil(source_duration) != math.ceil(analysis_duration):
        raise MediaPreflightError(
            "压缩分析视频与原片向上取整后的提示词时长不一致："
            f"{math.ceil(analysis_duration)} != {math.ceil(source_duration)} 秒"
        )
    if (
        abs(
            float(source_signature["start_time"])
            - float(analysis_signature["start_time"])
        )
        > 0.1
    ):
        raise MediaPreflightError("压缩分析视频与原片的时间轴起点不一致。")
    if bool(source_signature["has_audio"]) != bool(analysis_signature["has_audio"]):
        raise MediaPreflightError("压缩分析视频与原片的音轨存在性不一致。")
    source_ratio = float(source_signature["width"]) / float(source_signature["height"])
    analysis_ratio = float(analysis_signature["width"]) / float(
        analysis_signature["height"]
    )
    if abs(source_ratio - analysis_ratio) > max(0.002, source_ratio * 0.005):
        raise MediaPreflightError(
            "压缩分析视频与原片显示画幅不一致："
            f"{source_signature['width']}x{source_signature['height']} != "
            f"{analysis_signature['width']}x{analysis_signature['height']}"
        )
    validate_video_content_similarity(source_video, analysis_video, source_duration)
