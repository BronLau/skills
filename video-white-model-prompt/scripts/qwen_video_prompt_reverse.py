#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用阿里云百炼 Qwen3.5-Omni-Plus 与 Qwen3.8-Max 两阶段反推视频生成提示词。

参考视频必须通过 --video 显式传入，原视频始终只读。
系统提示词从独立文本文件读取，并作为 system message 原样提交。

环境变量：
  DASHSCOPE_API_KEY   可选，阿里云百炼 API Key；也可传 --api-key-file
  DASHSCOPE_BASE_URL  可选，OpenAI 兼容接口根地址
  QWEN_MODEL          可选，默认 qwen3.8-max
  QWEN_OMNI_MODEL     可选，默认 qwen3.5-omni-plus

示例：
  export DASHSCOPE_API_KEY="sk-..."
  python3 qwen_video_prompt_reverse.py --video /path/to/reference.mp4

使用 Workspace 专属地址：
  export DASHSCOPE_BASE_URL="https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
  python3 qwen_video_prompt_reverse.py --video /path/to/reference.mp4

参考视频含口播时，建议额外传入转写文本：
  python3 qwen_video_prompt_reverse.py --video /path/to/reference.mp4 --transcript-file transcript.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from math import gcd
from pathlib import Path
from typing import Any

from media_preflight import (
    API_MAX_DATA_URL_BYTES,
    CompressionSignature,
    DEFAULT_INLINE_LIMIT_MB,
    MediaPreflightError,
    build_compression_command as build_compression_command_shared,
    estimated_data_url_size,
    validate_image_input as validate_image_input_shared,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SYSTEM_PROMPT = (
    SCRIPT_DIR.parent / "prompts" / "video_reverse_system_prompt.txt"
)
DEFAULT_OMNI_DRAFT_ADDENDUM = (
    SCRIPT_DIR.parent / "prompts" / "video_reverse_omni_draft_addendum.txt"
)
DEFAULT_MAX_REFINE_ADDENDUM = (
    SCRIPT_DIR.parent / "prompts" / "video_reverse_max_refine_addendum.txt"
)
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-max"
DEFAULT_OMNI_MODEL = "qwen3.5-omni-plus"
RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
DEFAULT_MAX_INLINE_REQUEST_MB = DEFAULT_INLINE_LIMIT_MB
DEFAULT_MAX_TOKENS = 32768
MIN_SEGMENT_SECONDS = 4
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/gif",
}
SUPPORTED_VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-m4v",
    "video/x-msvideo",
    "video/x-matroska",
    "video/webm",
    "video/x-flv",
    "video/x-ms-wmv",
    "video/mpeg",
}


class ScriptError(RuntimeError):
    """可直接展示给使用者的脚本错误。"""


class AudioFactError(ScriptError):
    """Omni 没有给出可锁定的音频事实声明。"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="由 Omni 生成视听初稿、Max 视觉精修，反推视频生成提示词。"
    )
    parser.add_argument(
        "--video",
        type=Path,
        required=True,
        help="本地参考视频路径，必须显式传入。",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        default=DEFAULT_SYSTEM_PROMPT,
        help=f"系统提示词文本路径，默认：{DEFAULT_SYSTEM_PROMPT}",
    )
    parser.add_argument(
        "--omni-draft-addendum",
        type=Path,
        default=DEFAULT_OMNI_DRAFT_ADDENDUM,
        help="Qwen3.5-Omni-Plus 初稿阶段附加提示词。",
    )
    parser.add_argument(
        "--max-refine-addendum",
        type=Path,
        default=DEFAULT_MAX_REFINE_ADDENDUM,
        help="Qwen3.8-Max 精修阶段附加提示词。",
    )
    parser.add_argument(
        "--api-key-file",
        type=Path,
        help="DASHSCOPE_API_KEY 未设置时读取的 Key 文件路径。",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI 兼容接口根地址；也可设置 DASHSCOPE_BASE_URL。",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("QWEN_MODEL", DEFAULT_MODEL),
        help=f"模型 ID，默认：{DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--omni-model",
        default=os.environ.get("QWEN_OMNI_MODEL", DEFAULT_OMNI_MODEL),
        help=f"音轨存在时用于视听初稿的模型 ID，默认：{DEFAULT_OMNI_MODEL}",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=4.0,
        help="视频采样帧率，范围 0.1-10，默认 4。",
    )
    parser.add_argument(
        "--aspect-ratio",
        help="覆盖自动探测的画幅比例，例如 9:16。",
    )
    parser.add_argument(
        "--duration-seconds",
        type=int,
        help="覆盖自动探测并取整后的目标时长。",
    )
    parser.add_argument(
        "--segment-max-seconds",
        type=int,
        choices=(15, 30),
        required=True,
        help="用户选择的单段提示词最大时长，只能为 15 或 30。",
    )
    parser.add_argument("--character-image", type=Path, help="可选人物形象图。")
    parser.add_argument(
        "--product-image",
        type=Path,
        action="append",
        default=[],
        help="可选产品图，可重复传入，最多 9 张。",
    )
    parser.add_argument("--product-name", default="", help="可选产品名称。")
    parser.add_argument("--selling-points", default="", help="可选产品卖点。")
    parser.add_argument("--user-idea", default="", help="可选创意想法，最多 200 字。")
    parser.add_argument(
        "--allow-audio-rewrite",
        action="store_true",
        help="仅在用户明确要求改写口播时启用。",
    )
    parser.add_argument(
        "--spoken-replacement",
        action="append",
        default=[],
        metavar="旧词=新词",
        help="用户明确指定的口播词语替换，可重复传入。",
    )
    parser.add_argument(
        "--transcript-file",
        type=Path,
        help="可选的补充音轨转写文本；未提供时由模型直接分析视频原始音轨。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="结果文件路径；默认写入脚本目录 outputs/ 下的时间戳文件。",
    )
    parser.add_argument(
        "--draft-output",
        type=Path,
        help="可选：保存 Qwen3.5-Omni-Plus 视听提示词初稿。",
    )
    parser.add_argument(
        "--draft-file",
        type=Path,
        help="可选：复用已验证的视听提示词初稿，跳过 Omni 调用。",
    )
    parser.add_argument(
        "--draft-metadata-output",
        type=Path,
        help="可选：保存初稿与本次输入的绑定元数据。",
    )
    parser.add_argument(
        "--draft-metadata-file",
        type=Path,
        help="复用初稿时的绑定元数据；默认读取初稿同目录的 prompt_draft_meta.json。",
    )
    parser.add_argument(
        "--candidate-output",
        type=Path,
        help="可选：Max 未通过校验时保留的候选稿路径。",
    )
    parser.add_argument(
        "--draft-candidate-output",
        type=Path,
        help="可选：Omni 未完整结束时保留的初稿候选路径。",
    )
    parser.add_argument(
        "--request-body-output",
        type=Path,
        help="可选：保存 Max 阶段的完整请求体 JSON，不包含请求头和 API Key。",
    )
    parser.add_argument(
        "--response-body-output",
        type=Path,
        help="可选：保存 Max 阶段返回的完整响应体 JSON。",
    )
    parser.add_argument(
        "--omni-request-body-output",
        type=Path,
        help="可选：保存 Omni 阶段的完整请求体 JSON，不包含 API Key。",
    )
    parser.add_argument(
        "--omni-response-body-output",
        type=Path,
        help="可选：保存 Omni 阶段的流式响应块 JSON。",
    )
    parser.add_argument(
        "--segment-plan-output",
        type=Path,
        help="可选：保存从提示词解析出的分段时长与累计切分时间点 JSON。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖本次显式指定的输出文件。",
    )
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=int, default=300, help="单次请求超时秒数。")
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="每个模型阶段的最大请求次数，默认 2。",
    )
    parser.add_argument(
        "--max-inline-request-mb",
        type=float,
        default=DEFAULT_MAX_INLINE_REQUEST_MB,
        help="单个 Data URL 的内联上限 MiB，最大且默认 9.5；超限停止并提示压缩。",
    )
    return parser.parse_args()


def require_readable_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ScriptError(f"{label}不存在：{resolved}")
    if not os.access(resolved, os.R_OK):
        raise ScriptError(f"{label}不可读：{resolved}")
    return resolved


def load_api_key(path: Path) -> str:
    resolved = require_readable_file(path, "DashScope API Key 文件")
    content = resolved.read_text(encoding="utf-8")
    match = re.search(r"sk-[A-Za-z0-9_-]{10,}", content)
    if not match:
        raise ScriptError(f"API Key 文件中未找到有效的 sk- token：{resolved}")
    return match.group(0)


def parse_spoken_replacements(values: list[str]) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    seen_sources: set[str] = set()
    for value in values:
        if "=" not in value:
            raise ScriptError(
                "--spoken-replacement 必须使用 旧词=新词 格式。"
            )
        source, target = (part.strip() for part in value.split("=", 1))
        if not source or not target:
            raise ScriptError("--spoken-replacement 的旧词和新词都不能为空。")
        if any(character in source + target for character in "{}\r\n"):
            raise ScriptError("--spoken-replacement 不能包含大括号或换行。")
        if source in seen_sources:
            raise ScriptError(f"--spoken-replacement 重复指定旧词：{source}")
        seen_sources.add(source)
        replacements.append((source, target))
    return replacements


def validate_args(args: argparse.Namespace) -> None:
    if not args.model.strip() or not args.omni_model.strip():
        raise ScriptError("--model 和 --omni-model 不能为空。")
    if not 0.1 <= args.fps <= 10:
        raise ScriptError("--fps 必须在 0.1 到 10 之间。")
    if args.duration_seconds is not None and args.duration_seconds <= 0:
        raise ScriptError("--duration-seconds 必须是正整数。")
    if len(args.product_image) > 9:
        raise ScriptError("--product-image 最多传入 9 次。")
    if len(args.user_idea) > 200:
        raise ScriptError("--user-idea 不能超过 200 字。")
    if args.max_tokens <= 0 or args.timeout <= 0 or args.retries <= 0:
        raise ScriptError("--max-tokens、--timeout 和 --retries 必须为正整数。")
    if args.max_tokens > 65536:
        raise ScriptError("--max-tokens 不能超过模型的 65536 输出上限。")
    if not 0 < args.temperature <= 2:
        raise ScriptError("--temperature 必须在 0 到 2 之间。")
    if args.max_inline_request_mb <= 0:
        raise ScriptError("--max-inline-request-mb 必须为正数。")
    if args.max_inline_request_mb > DEFAULT_MAX_INLINE_REQUEST_MB:
        raise ScriptError("--max-inline-request-mb 不能超过 9.5 MiB。")
    if args.draft_file and args.draft_output:
        raise ScriptError("--draft-file 与 --draft-output 不能同时使用。")
    if args.draft_file and (args.draft_metadata_output or args.draft_candidate_output):
        raise ScriptError(
            "复用 --draft-file 时不能设置 --draft-metadata-output 或 "
            "--draft-candidate-output。"
        )
    if not args.draft_file and args.draft_metadata_file:
        raise ScriptError("--draft-metadata-file 必须与 --draft-file 一起使用。")
    replacements = parse_spoken_replacements(args.spoken_replacement)
    if args.allow_audio_rewrite and replacements:
        raise ScriptError(
            "--allow-audio-rewrite 与 --spoken-replacement 不能同时使用。"
        )


def probe_video(video_path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ScriptError("未找到 ffprobe，无法自动探测画幅和时长。")

    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(video_path),
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
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ScriptError(f"ffprobe 无法读取参考视频：{detail.strip()}") from exc

    streams = metadata.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    if not video_stream:
        raise ScriptError("参考文件中没有视频流。")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ScriptError("无法从参考视频读取有效宽高。")

    rotation = int((video_stream.get("tags") or {}).get("rotate") or 0)
    for side_data in video_stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            rotation = int(side_data["rotation"])
            break
    if abs(rotation) % 180 == 90:
        width, height = height, width

    try:
        source_duration = float(metadata["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScriptError("无法从参考视频读取有效时长。") from exc
    if not math.isfinite(source_duration) or source_duration <= 0:
        raise ScriptError("参考视频时长无效。")

    divisor = gcd(width, height)
    return {
        "aspect_ratio": f"{width // divisor}:{height // divisor}",
        "source_duration": source_duration,
        "duration_seconds": max(1, int(math.ceil(source_duration))),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def validate_video_api_limits(path: Path, metadata: dict[str, Any]) -> None:
    duration = float(metadata["source_duration"])
    if duration < MIN_SEGMENT_SECONDS:
        raise ScriptError(
            f"Seedance 成片要求参考视频至少 {MIN_SEGMENT_SECONDS} 秒。"
        )
    if metadata["has_audio"] and duration > 3600:
        raise ScriptError("含音轨视频超过 Qwen3.5-Omni-Plus 的 1 小时时长上限。")
    if not metadata["has_audio"] and duration > 7200:
        raise ScriptError("视频超过 Qwen3.8-Max 的 2 小时时长上限。")


def validate_image_api_limits(path: Path, label: str, inline_limit_mb: float) -> None:
    try:
        validate_image_input_shared(path, label, inline_limit_mb)
    except MediaPreflightError as exc:
        raise ScriptError(str(exc)) from exc


def media_mime_type(path: Path, media_prefix: str) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    supported = (
        SUPPORTED_VIDEO_MIME_TYPES
        if media_prefix == "video"
        else SUPPORTED_IMAGE_MIME_TYPES
    )
    if mime_type not in supported:
        raise ScriptError(
            f"不支持的{media_prefix}文件格式：{path.name}，识别到 {mime_type or '未知'}"
        )
    return mime_type


def encoded_data_url_size(path: Path, mime_type: str) -> int:
    return estimated_data_url_size(path, mime_type)


def file_to_data_url(path: Path, media_prefix: str) -> str:
    mime_type = media_mime_type(path, media_prefix)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_compression_command(path: Path) -> str:
    metadata = probe_video(path)
    signature: CompressionSignature = {
        "duration": metadata["source_duration"],
        "has_audio": metadata["has_audio"],
    }
    try:
        return build_compression_command_shared(path, signature)
    except MediaPreflightError as exc:
        raise ScriptError(str(exc)) from exc


class MediaResolver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.inline_limit = int(args.max_inline_request_mb * 1024 * 1024)
        self.cache: dict[tuple[Path, str], str] = {}

    def resolve(self, path: Path, media_prefix: str) -> str:
        resolved = require_readable_file(path, media_prefix)
        cache_key = (resolved, media_prefix)
        if cache_key in self.cache:
            return self.cache[cache_key]
        mime_type = media_mime_type(resolved, media_prefix)
        encoded_size = encoded_data_url_size(resolved, mime_type)
        effective_limit = min(self.inline_limit, API_MAX_DATA_URL_BYTES)
        if encoded_size >= effective_limit:
            encoded_mb = encoded_size / 1024**2
            if media_prefix == "video":
                command = build_compression_command(resolved)
                raise ScriptError(
                    "视频 Base64 Data URL 超过 9.5 MiB 安全阈值"
                    "（接口要求严格小于 10 MiB）："
                    f"{encoded_mb:.2f} MiB。请先压缩后重新运行：\n{command}"
                )
            raise ScriptError(
                "图片 Base64 Data URL 超过 9.5 MiB 安全阈值"
                "（接口要求严格小于 10 MiB）："
                f"{encoded_mb:.2f} MiB。请先压缩图片到约 7 MiB 以下。"
            )
        reference = file_to_data_url(resolved, media_prefix)
        print(
            f"INLINE_MEDIA file={resolved.name} data_url={encoded_size / 1024**2:.2f}MiB",
            file=sys.stderr,
        )
        self.cache[cache_key] = reference
        return reference


def optional_text_file(path: Path | None, label: str) -> str:
    if path is None:
        return ""
    resolved = require_readable_file(path, label)
    content = resolved.read_text(encoding="utf-8").strip()
    if not content:
        raise ScriptError(f"{label}内容为空：{resolved}")
    return content


def add_image_content(
    content: list[dict[str, Any]], reference: str, number: int, meaning: str
) -> None:
    content.append({"type": "text", "text": f"@图片{number}：{meaning}"})
    content.append(
        {
            "type": "image_url",
            "image_url": {"url": reference},
        }
    )


def context_lines(
    args: argparse.Namespace,
    aspect_ratio: str,
    duration_seconds: int,
) -> list[str]:
    image_references: list[str] = []
    image_number = 1
    if args.character_image:
        image_references.append(f"@图片{image_number}=人物形象图")
        image_number += 1
    for product_index, _ in enumerate(args.product_image, start=1):
        image_references.append(
            f"@图片{image_number}=第{product_index}张产品参考图"
        )
        image_number += 1

    segment_count = math.ceil(duration_seconds / args.segment_max_seconds)
    if segment_count == 1:
        segment_contract = (
            "本次输出1段完整正文，镜头时间轴从00:00连续覆盖到"
            f"{duration_seconds}秒。"
        )
    else:
        segment_contract = (
            f"本次必须恰好输出{segment_count}段，每段为4到"
            f"{args.segment_max_seconds}秒的整数秒，合计{duration_seconds}秒；"
            "先按完整台词、连续声效和音乐节点选择安全切点，再依照"
            "【第N段提示词（D秒，对齐参考视频S-E秒）】格式填写实际整数，"
            "各段内部镜头均从镜头1[00:00-...]重新编号计时。"
        )

    target_generator = (
        "Doubao Seedance 2.0"
        if args.segment_max_seconds == 15
        else "Doubao Seedance 2.5"
    )
    spoken_replacements = parse_spoken_replacements(args.spoken_replacement)
    if args.allow_audio_rewrite:
        audio_rewrite_permission = "用户已明确允许改写口播。"
    elif spoken_replacements:
        mappings = "；".join(
            f"{source}→{target}" for source, target in spoken_replacements
        )
        audio_rewrite_permission = f"仅允许以下指定词替换：{mappings}。"
    else:
        audio_rewrite_permission = "未授权改写；逐段保持 Omni 台词原文。"

    return [
        "reference_video：本消息中的视频，仅用于提示词推理。",
        f"character_image：{'已按图片编号提供' if args.character_image else '未提供'}。",
        f"product_images：{'已按图片编号提供' if args.product_image else '未提供'}。",
        "available_image_references："
        + (
            "；".join(image_references)
            if image_references
            else "空；所有主体使用纯文本定义。"
        ),
        f"product_name：{args.product_name.strip() or '未提供'}。",
        f"selling_points：{args.selling_points.strip() or '未提供'}。",
        f"user_idea：{args.user_idea.strip() or '未提供'}。",
        f"composition_aspect_context：源画面比例为{aspect_ratio}，仅用于构图推理；"
        "提示词正文不复述该比例。",
        f"duration_seconds：{duration_seconds}",
        f"segment_min_seconds：{MIN_SEGMENT_SECONDS}",
        f"segment_max_seconds：{args.segment_max_seconds}",
        f"required_segment_count：{segment_count}",
        f"segment_output_contract：{segment_contract}",
        f"target_generator：{target_generator}",
        f"audio_rewrite_permission：{audio_rewrite_permission}",
    ]


def build_direct_user_text(
    args: argparse.Namespace,
    aspect_ratio: str,
    duration_seconds: int,
    has_audio: bool,
    transcript: str,
) -> str:
    if transcript:
        audio_context = (
            "以下为用户提供的音轨转写，请按时间和画面匹配使用；"
            f"未写出的 BGM、环境声或音效不得自行补造：\n{transcript}"
        )
    elif has_audio:
        raise ScriptError("含音轨视频必须先完成 Omni 视听初稿")
    else:
        audio_context = "参考视频没有音轨，不要编造任何人声、BGM、环境声或音效。"

    return "\n".join(
        [
            "请严格依据系统提示词分析本消息中的参考视频，只输出最终视频生成提示词正文。",
            *context_lines(args, aspect_ratio, duration_seconds),
            f"音频补充信息：{audio_context}",
        ]
    )


def build_draft_user_text(
    args: argparse.Namespace,
    aspect_ratio: str,
    duration_seconds: int,
    transcript: str,
) -> str:
    transcript_context = (
        "未提供额外转写。请以参考视频原始音轨为准。"
        if not transcript
        else (
            "以下用户转写仅用于辅助核对台词文字；声音时间、说话人、语气、"
            f"BGM、环境声和音效仍以原始音轨为准：\n{transcript}"
        )
    )
    return "\n".join(
        [
            "请分析本消息中的参考视频画面与原始音轨，输出完整的视听提示词初稿正文。",
            *context_lines(args, aspect_ratio, duration_seconds),
            f"音频补充信息：{transcript_context}",
        ]
    )


def build_refine_user_text(
    args: argparse.Namespace,
    aspect_ratio: str,
    duration_seconds: int,
    draft: str,
    draft_plan: dict[str, Any] | None = None,
    draft_structure_error: str = "",
) -> str:
    lines = [
        "请结合本消息中的参考视频画面、图片参考和下方视听初稿，"
        "输出完整的最终视频生成提示词正文。",
        *context_lines(args, aspect_ratio, duration_seconds),
        "audio_visual_draft：以下初稿由可理解音频的全模态模型生成；"
        "其中的音频内容按系统提示词中的事实权限处理。",
    ]
    if draft_structure_error:
        if draft_plan is None:
            raise ScriptError("内部错误：结构修复交接缺少已锁定的分段计划。")
        lines.extend(
            [
                "draft_structure_repair：Omni 已确定合法分段计划和音频事实，"
                "但初稿内部结构未通过机器校验。请在输出最终稿时完成结构修复，"
                "不得移动下方锁定分段点。",
                f"draft_structure_error：{draft_structure_error}",
                locked_segment_plan_contract(draft_plan),
            ]
        )
    elif draft_plan is not None:
        lines.append(locked_shot_timeline_contract(draft))
    spoken_replacements = parse_spoken_replacements(args.spoken_replacement)
    if not args.allow_audio_rewrite:
        validate_spoken_replacement_sources(draft, spoken_replacements)
        spoken_rule = (
            "最终稿只执行上述指定词替换；除此之外，所有 {} 内文本按出现顺序"
            "拼接后必须与初稿逐字一致。"
            if spoken_replacements
            else "最终稿可以在镜头间重新拆分或合并这些台词，但所有 {} 内文本"
            "按出现顺序拼接后必须与上述清单逐字一致。"
        )
        lines.extend(
            [
                spoken_content_contract(draft, spoken_replacements),
                spoken_rule,
            ]
        )
    lines.extend(
        [
            "===== 视听提示词初稿 开始 =====",
            draft,
            "===== 视听提示词初稿 结束 =====",
        ]
    )
    return "\n".join(lines)


def build_messages(
    args: argparse.Namespace,
    system_prompt: str,
    video_reference: str,
    user_text: str,
    character_reference: str | None,
    product_references: list[str],
    include_fps: bool = True,
) -> list[dict[str, Any]]:
    video_content: dict[str, Any] = {
        "type": "video_url",
        "video_url": {"url": video_reference},
    }
    if include_fps:
        video_content["fps"] = args.fps
    user_content: list[dict[str, Any]] = [video_content]

    image_number = 1
    if character_reference:
        add_image_content(user_content, character_reference, image_number, "人物形象图")
        image_number += 1
    for index, product_reference in enumerate(product_references, start=1):
        add_image_content(
            user_content,
            product_reference,
            image_number,
            f"第{index}张产品参考图",
        )
        image_number += 1

    user_content.append(
        {
            "type": "text",
            "text": user_text,
        }
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def response_text(body: dict[str, Any]) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ScriptError(
            "API 返回结构中没有模型正文：" + json.dumps(body, ensure_ascii=False)[:1000]
        ) from exc

    if isinstance(content, str):
        result = content.strip()
    elif isinstance(content, list):
        result = "\n".join(
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("text")
        ).strip()
    else:
        result = ""
    if not result:
        raise ScriptError("API 返回的模型正文为空。")
    return result


def completion_finish_reason(body: dict[str, Any]) -> str:
    try:
        return str(body["choices"][0].get("finish_reason") or "")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


def require_complete_finish(reason: str, label: str) -> None:
    if reason not in {"", "stop"}:
        raise ScriptError(f"{label}未正常完成，finish_reason={reason}")


def file_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def draft_input_metadata(
    args: argparse.Namespace,
    video_path: Path,
    character_image: Path | None,
    product_images: list[Path],
    transcript_file: Path | None,
    aspect_ratio: str,
    duration_seconds: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "source_video": file_identity(video_path),
        "character_image": file_identity(character_image),
        "product_images": [file_identity(path) for path in product_images],
        "transcript_file": file_identity(transcript_file),
        "product_name": args.product_name.strip(),
        "selling_points": args.selling_points.strip(),
        "user_idea": args.user_idea.strip(),
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds,
        "segment_max_seconds": args.segment_max_seconds,
        "omni_model": args.omni_model,
    }
    if args.allow_audio_rewrite:
        body["allow_audio_rewrite"] = True
    if args.spoken_replacement:
        body["spoken_replacements"] = list(args.spoken_replacement)
    return body


def load_and_validate_draft_metadata(
    path: Path,
    expected: dict[str, Any],
) -> None:
    resolved = require_readable_file(path, "视听初稿元数据")
    try:
        actual = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptError(f"视听初稿元数据不是有效 JSON：{resolved}") from exc
    if actual != expected:
        raise ScriptError("视听初稿元数据与当前视频、产品或参数不一致，拒绝复用。")


TIME_TOLERANCE = 0.02
SEGMENT_HEADER_PATTERN = re.compile(
    r"【(?P<title>[^】]*?段提示词)[（(]\s*"
    r"(?P<duration>\d+(?:\.\d+)?)\s*秒\s*[,，]\s*"
    r"对齐参考视频\s*(?P<source_start>\d+(?:\.\d+)?)\s*"
    r"[-–—至]\s*(?P<source_end>\d+(?:\.\d+)?)\s*秒?\s*[）)]】"
)
SHOT_PATTERN = re.compile(
    r"镜头(?P<number>\d+)\["
    r"(?P<start>\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*[-–—]\s*"
    r"(?P<end>\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\]"
)


def parse_timecode(value: str) -> float:
    parts = value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ScriptError(f"无效镜头时间码：{value}") from exc
    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0.0
    elif len(numbers) == 3:
        hours, minutes, seconds = numbers
    else:
        raise ScriptError(f"无效镜头时间码：{value}")
    if hours < 0 or minutes < 0 or seconds < 0 or minutes >= 60 or seconds >= 60:
        raise ScriptError(f"无效镜头时间码：{value}")
    return hours * 3600 + minutes * 60 + seconds


def validate_shot_timeline(section: str, duration: float, label: str) -> None:
    matches = list(SHOT_PATTERN.finditer(section))
    if not matches:
        raise ScriptError(f"{label}没有可解析的镜头时间轴。")
    expected_start = 0.0
    for index, match in enumerate(matches, start=1):
        number = int(match.group("number"))
        start = parse_timecode(match.group("start"))
        end = parse_timecode(match.group("end"))
        if number != index:
            raise ScriptError(
                f"{label}镜头编号不连续：期望镜头{index}，实际镜头{number}"
            )
        if abs(start - expected_start) > TIME_TOLERANCE:
            raise ScriptError(
                f"{label}镜头时间轴存在缺口或重叠：{start:g} != {expected_start:g}"
            )
        if end <= start:
            raise ScriptError(f"{label}镜头{number}结束时间必须晚于开始时间。")
        if end > duration + TIME_TOLERANCE:
            raise ScriptError(f"{label}镜头{number}超出本段时长 {duration:g} 秒。")
        expected_start = end
    if abs(expected_start - duration) > TIME_TOLERANCE:
        raise ScriptError(
            f"{label}最后镜头未覆盖到段尾：{expected_start:g} != {duration:g}"
        )


def build_segment_header_plan(
    result: str,
    prompt_duration_seconds: int,
    source_duration_seconds: float,
    segment_max_seconds: int,
) -> dict[str, Any]:
    if prompt_duration_seconds < MIN_SEGMENT_SECONDS:
        raise ScriptError(
            f"Seedance 目标时长不能少于 {MIN_SEGMENT_SECONDS} 秒："
            f"{prompt_duration_seconds}"
        )
    expected_segment_count = math.ceil(prompt_duration_seconds / segment_max_seconds)
    header_matches = list(SEGMENT_HEADER_PATTERN.finditer(result))
    durations: list[float] = []
    if not header_matches:
        if expected_segment_count > 1:
            raise ScriptError("Qwen 返回内容未包含可解析的分段标题，无法驱动白模切分。")
        durations = [float(prompt_duration_seconds)]
    else:
        if len(header_matches) != expected_segment_count:
            raise ScriptError(
                "Qwen 返回的分段数量不是 Seedance 约束下的最少段数："
                f"{len(header_matches)} != {expected_segment_count}"
            )
        if result[: header_matches[0].start()].strip():
            raise ScriptError("第一段标题之前存在额外内容。")
        source_cursor = 0.0
        for index, match in enumerate(header_matches, start=1):
            duration = float(match.group("duration"))
            source_start = float(match.group("source_start"))
            source_end = float(match.group("source_end"))
            expected_end = source_cursor + duration
            if abs(source_start - source_cursor) > TIME_TOLERANCE:
                raise ScriptError(
                    f"第{index}段参考视频起点错误：{source_start:g} != {source_cursor:g}"
                )
            if abs(source_end - expected_end) > TIME_TOLERANCE:
                raise ScriptError(
                    f"第{index}段参考视频终点错误：{source_end:g} != {expected_end:g}"
                )
            durations.append(duration)
            source_cursor = expected_end

    if any(
        value < MIN_SEGMENT_SECONDS or value > segment_max_seconds
        for value in durations
    ):
        raise ScriptError(
            "Qwen 返回的分段时长必须在 "
            f"{MIN_SEGMENT_SECONDS} 到 {segment_max_seconds} 秒之间：{durations}"
        )
    if any(abs(value - round(value)) > TIME_TOLERANCE for value in durations):
        raise ScriptError(f"Seedance 分段时长必须为整数秒：{durations}")
    if abs(sum(durations) - prompt_duration_seconds) > 0.01:
        raise ScriptError(
            "Qwen 返回的分段时长总和与目标时长不一致："
            f"{sum(durations):g} != {prompt_duration_seconds}"
        )

    segments: list[dict[str, Any]] = []
    cursor = 0.0
    for index, duration in enumerate(durations, start=1):
        start = cursor
        cursor += duration
        segments.append(
            {
                "index": index,
                "duration_seconds": duration,
                "prompt_start_seconds": start,
                "prompt_end_seconds": cursor,
            }
        )

    split_times = []
    cursor = 0.0
    for duration in durations[:-1]:
        cursor += duration
        if cursor >= source_duration_seconds:
            raise ScriptError(
                "Qwen 返回的切分时间点超出参考视频实际时长："
                f"{cursor:g} >= {source_duration_seconds:.6f}"
            )
        split_times.append(cursor)

    return {
        "schema_version": 2,
        "segment_max_seconds": segment_max_seconds,
        "segment_min_seconds": MIN_SEGMENT_SECONDS,
        "expected_segment_count": expected_segment_count,
        "prompt_duration_seconds": prompt_duration_seconds,
        "source_duration_seconds": source_duration_seconds,
        "segments": segments,
        "split_times_seconds": split_times,
    }


def validate_segment_shot_timelines(
    result: str,
    plan: dict[str, Any],
) -> None:
    header_matches = list(SEGMENT_HEADER_PATTERN.finditer(result))
    segments = list(plan["segments"])
    if not header_matches:
        validate_shot_timeline(
            result,
            float(segments[0]["duration_seconds"]),
            "单段提示词",
        )
        return
    for index, match in enumerate(header_matches, start=1):
        section_end = (
            header_matches[index].start()
            if index < len(header_matches)
            else len(result)
        )
        validate_shot_timeline(
            result[match.end() : section_end],
            float(segments[index - 1]["duration_seconds"]),
            f"第{index}段提示词",
        )


def build_segment_plan(
    result: str,
    prompt_duration_seconds: int,
    source_duration_seconds: float,
    segment_max_seconds: int,
) -> dict[str, Any]:
    plan = build_segment_header_plan(
        result,
        prompt_duration_seconds,
        source_duration_seconds,
        segment_max_seconds,
    )
    validate_segment_shot_timelines(result, plan)
    return plan


def require_same_split_times(
    draft_plan: dict[str, Any], final_plan: dict[str, Any]
) -> None:
    draft_times = [float(value) for value in draft_plan["split_times_seconds"]]
    final_times = [float(value) for value in final_plan["split_times_seconds"]]
    if len(draft_times) != len(final_times) or any(
        abs(draft - final) > TIME_TOLERANCE
        for draft, final in zip(draft_times, final_times)
    ):
        raise ScriptError(
            "Max 最终分段点与 Omni 音频安全分段点不一致："
            f"draft={draft_times}, final={final_times}"
        )


IMAGE_REFERENCE_PATTERN = re.compile(r"@图片(?P<number>\d+)")
BARE_IMAGE_REFERENCE_PATTERN = re.compile(r"(?<!@)图片(?P<number>\d+)")
SPOKEN_CONTENT_PATTERN = re.compile(r"\{(?P<content>[^{}]*)\}")
NO_SPEECH_MARKER = "[[NO_SPEECH_CONFIRMED]]"
API_CONTROL_LITERAL_PATTERN = re.compile(
    r"(?<!\d)(?:21:9|16:9|9:16|4:3|3:4|1:1)(?!\d)"
    r"|(?<![A-Za-z0-9])(?:480p|720p|1080p|2k|4k|8k)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
DEFINITION_LINE_PATTERN = re.compile(
    r"^(?:参考@图片\d+中的.+，将其定义为<[^<>]+>|将.+定义为<[^<>]+>)[。.]?$"
)


def prompt_sections(result: str) -> list[str]:
    header_matches = list(SEGMENT_HEADER_PATTERN.finditer(result))
    if not header_matches:
        return [result]
    return [
        result[
            match.end() : (
                header_matches[index].start()
                if index < len(header_matches)
                else len(result)
            )
        ]
        for index, match in enumerate(header_matches, start=1)
    ]


def validate_image_reference_contract(
    result: str,
    expected_image_count: int,
    label: str,
) -> None:
    expected = set(range(1, expected_image_count + 1))
    for index, section in enumerate(prompt_sections(result), start=1):
        bare = sorted(
            {
                int(match.group("number"))
                for match in BARE_IMAGE_REFERENCE_PATTERN.finditer(section)
            }
        )
        if bare:
            raise ScriptError(f"{label}第{index}段图片引用缺少 @ 前缀：{bare}")
        actual = {
            int(match.group("number"))
            for match in IMAGE_REFERENCE_PATTERN.finditer(section)
        }
        if actual != expected:
            raise ScriptError(
                f"{label}第{index}段图片引用与实际输入不一致："
                f"expected={sorted(expected)}, actual={sorted(actual)}"
            )


def validate_no_api_control_literals(result: str, label: str) -> None:
    matches = sorted({match.group(0) for match in API_CONTROL_LITERAL_PATTERN.finditer(result)})
    if matches:
        raise ScriptError(
            f"{label}包含应由 Seedance API 控制的画幅或分辨率字面量：{matches}"
        )


def validate_no_segment_overview(result: str, label: str) -> None:
    for index, section in enumerate(prompt_sections(result), start=1):
        first_shot = SHOT_PATTERN.search(section)
        if first_shot is None:
            continue
        preamble_lines = [
            line.strip()
            for line in section[: first_shot.start()].splitlines()
            if line.strip()
        ]
        invalid_lines = [
            line
            for line in preamble_lines
            if DEFINITION_LINE_PATTERN.fullmatch(line) is None
        ]
        if invalid_lines:
            raise ScriptError(
                f"{label}第{index}段在镜头1前包含非主体定义内容；"
                "主体定义后必须直接进入镜头1。"
            )


def validate_omni_audio_fact_coverage(result: str) -> str:
    has_spoken_content = SPOKEN_CONTENT_PATTERN.search(result) is not None
    marker_count = result.count(NO_SPEECH_MARKER)
    if has_spoken_content and marker_count:
        raise AudioFactError("Omni 初稿同时包含台词和无人声确认标记。")
    if not has_spoken_content and marker_count != 1:
        raise AudioFactError(
            "Omni 初稿没有可锁定台词，也没有唯一的无人声确认标记。"
        )
    return result.replace(NO_SPEECH_MARKER, "").strip()


def shot_timeline_signature(result: str) -> list[list[tuple[float, float]]]:
    return [
        [
            (
                parse_timecode(match.group("start")),
                parse_timecode(match.group("end")),
            )
            for match in SHOT_PATTERN.finditer(section)
        ]
        for section in prompt_sections(result)
    ]


def locked_shot_timeline_contract(result: str) -> str:
    clean_result = result.replace(NO_SPEECH_MARKER, "").strip()
    lines = [
        "locked_shot_timeline：以下镜头数量、顺序和时间区间已经锁定；"
        "只精修各镜头内部内容："
    ]
    for section_index, section in enumerate(
        shot_timeline_signature(clean_result), start=1
    ):
        intervals = "、".join(
            f"{start:g}-{end:g}秒" for start, end in section
        )
        lines.append(f"第{section_index}段：{intervals}")
    return "\n".join(lines)


def require_same_shot_timeline(draft: str, final: str) -> None:
    clean_draft = draft.replace(NO_SPEECH_MARKER, "").strip()
    draft_signature = shot_timeline_signature(clean_draft)
    final_signature = shot_timeline_signature(final)
    if len(draft_signature) != len(final_signature):
        raise ScriptError("Max 最终稿改变了 Omni 初稿的镜头分段数量。")
    for section_index, (draft_section, final_section) in enumerate(
        zip(draft_signature, final_signature), start=1
    ):
        if len(draft_section) != len(final_section) or any(
            abs(draft_start - final_start) > TIME_TOLERANCE
            or abs(draft_end - final_end) > TIME_TOLERANCE
            for (draft_start, draft_end), (final_start, final_end) in zip(
                draft_section, final_section
            )
        ):
            raise ScriptError(
                f"Max 最终稿改变了 Omni 初稿第{section_index}段的镜头顺序或时间区间。"
            )


def apply_spoken_replacements(
    content: str,
    replacements: list[tuple[str, str]],
) -> str:
    for source, target in replacements:
        content = content.replace(source, target)
    return content


def validate_spoken_replacement_sources(
    result: str,
    replacements: list[tuple[str, str]],
) -> None:
    spoken_items = [
        match.group("content") for match in SPOKEN_CONTENT_PATTERN.finditer(result)
    ]
    for source, _ in replacements:
        if not any(source in item for item in spoken_items):
            raise ScriptError(
                f"指定口播替换的旧词未出现在 Omni 台词中：{source}"
            )


def normalized_spoken_content(
    result: str,
    replacements: list[tuple[str, str]] | None = None,
) -> str:
    spoken = "".join(
        match.group("content") for match in SPOKEN_CONTENT_PATTERN.finditer(result)
    )
    spoken = apply_spoken_replacements(spoken, replacements or [])
    return re.sub(r"[\W_]+", "", spoken, flags=re.UNICODE).casefold()


def spoken_content_contract(
    result: str,
    replacements: list[tuple[str, str]] | None = None,
) -> str:
    active_replacements = replacements or []
    sections = prompt_sections(result)
    section_items = [
        [
            apply_spoken_replacements(
                match.group("content").strip(),
                active_replacements,
            )
            for match in SPOKEN_CONTENT_PATTERN.finditer(section)
        ]
        for section in sections
    ]
    if not any(section_items):
        return "locked_spoken_content：空；最终稿不写人物台词。"
    lines = ["locked_spoken_content：以下台词按段锁定，文字与顺序不可改写："]
    for section_index, items in enumerate(section_items, start=1):
        lines.append(f"第{section_index}段：")
        lines.extend(
            f"{item_index}. {{{content}}}"
            for item_index, content in enumerate(items, start=1)
        )
    return "\n".join(lines)


def locked_segment_plan_contract(plan: dict[str, Any]) -> str:
    lines = [
        "locked_segment_plan：以下段长、累计切点和参考视频范围已经锁定；"
        "每段内部镜头必须从 00:00 连续覆盖到该段段尾："
    ]
    for segment in plan["segments"]:
        index = int(segment["index"])
        duration = float(segment["duration_seconds"])
        source_start = float(segment["prompt_start_seconds"])
        source_end = float(segment["prompt_end_seconds"])
        lines.append(
            f"第{index}段：{duration:g}秒，对齐参考视频"
            f"{source_start:g}-{source_end:g}秒。"
        )
    return "\n".join(lines)


def require_same_spoken_content(
    draft: str,
    final: str,
    replacements: list[tuple[str, str]] | None = None,
) -> None:
    active_replacements = replacements or []
    draft_sections = [
        normalized_spoken_content(section, active_replacements)
        for section in prompt_sections(draft)
    ]
    final_sections = [
        normalized_spoken_content(section) for section in prompt_sections(final)
    ]
    if len(draft_sections) != len(final_sections):
        raise ScriptError(
            "Max 最终稿与 Omni 初稿的台词分段数量不一致："
            f"{len(final_sections)} != {len(draft_sections)}"
        )
    for section_index, (draft_spoken, final_spoken) in enumerate(
        zip(draft_sections, final_sections),
        start=1,
    ):
        if draft_spoken == final_spoken:
            continue
        first_difference = next(
            (
                index
                for index, (draft_char, final_char) in enumerate(
                    zip(draft_spoken, final_spoken)
                )
                if draft_char != final_char
            ),
            min(len(draft_spoken), len(final_spoken)),
        )
        excerpt_start = max(0, first_difference - 12)
        excerpt_end = first_difference + 20
        raise ScriptError(
            f"Max 最终稿改变了 Omni 初稿第{section_index}段的台词原文："
            f"first_diff={first_difference}, "
            f"draft={draft_spoken[excerpt_start:excerpt_end]!r}, "
            f"final={final_spoken[excerpt_start:excerpt_end]!r}"
        )


def validate_prompt_contract(
    result: str,
    prompt_duration_seconds: int,
    source_duration_seconds: float,
    segment_max_seconds: int,
    expected_image_count: int,
    label: str,
    locked_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = build_segment_plan(
        result,
        prompt_duration_seconds,
        source_duration_seconds,
        segment_max_seconds,
    )
    validate_image_reference_contract(result, expected_image_count, label)
    validate_no_api_control_literals(result, label)
    validate_no_segment_overview(result, label)
    if locked_plan is not None:
        require_same_split_times(locked_plan, plan)
    return plan


def validate_omni_draft_for_max(
    result: str,
    prompt_duration_seconds: int,
    source_duration_seconds: float,
    segment_max_seconds: int,
    expected_image_count: int,
    label: str,
) -> tuple[dict[str, Any], str]:
    contract_result = validate_omni_audio_fact_coverage(result)
    try:
        plan = validate_prompt_contract(
            contract_result,
            prompt_duration_seconds,
            source_duration_seconds,
            segment_max_seconds,
            expected_image_count,
            label,
        )
        return plan, ""
    except ScriptError as structure_error:
        plan = build_segment_header_plan(
            contract_result,
            prompt_duration_seconds,
            source_duration_seconds,
            segment_max_seconds,
        )
        return plan, str(structure_error)


def build_contract_repair_messages(
    messages: list[dict[str, Any]],
    candidate: str,
    validation_error: str,
    stage_label: str,
    locked_spoken_contract: str = "",
) -> list[dict[str, Any]]:
    return [
        *messages,
        {"role": "assistant", "content": candidate},
        {
            "role": "user",
            "content": (
                f"上一版{stage_label}未通过机器校验：{validation_error}\n"
                "请保留可确认的视听事实、主体定义和既定分段意图，完整重写正文，"
                "只修正校验错误及其连带的编号、时间区间或图片引用。"
                + (f"\n{locked_spoken_contract}" if locked_spoken_contract else "")
                + "\n"
                "输出必须是完整可提交提示词，不要输出解释、修改清单或局部补丁。"
            ),
        },
    ]


def sdk_json(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    return {"value": str(value)}


def call_omni(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
    retries: int,
    capture_chunks: bool,
) -> tuple[str, dict[str, Any]]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ScriptError(
            "含音轨视频需要 openai Python SDK 以调用 Qwen3.5-Omni-Plus"
        ) from exc

    client = OpenAI(
        api_key=api_key,
        base_url=base_url.rstrip("/") + "/",
        timeout=float(timeout),
        max_retries=0,
    )
    last_error = ""
    for attempt in range(1, retries + 1):
        received_content = False
        result_parts: list[str] = []
        raw_chunks: list[dict[str, Any]] | None = [] if capture_chunks else None
        usage: dict[str, Any] | None = None
        finish_reason = ""
        reported_chars = 0
        total_chars = 0
        print(
            f"OMNI_DRAFT request_start attempt={attempt}/{retries}",
            file=sys.stderr,
            flush=True,
        )
        try:
            stream = client.chat.completions.create(**payload)
            for chunk in stream:
                if raw_chunks is not None:
                    raw_chunks.append(sdk_json(chunk))
                if getattr(chunk, "usage", None) is not None:
                    usage = sdk_json(chunk.usage)
                if not getattr(chunk, "choices", None):
                    continue
                chunk_finish = getattr(chunk.choices[0], "finish_reason", None)
                if chunk_finish:
                    finish_reason = str(chunk_finish)
                content = getattr(chunk.choices[0].delta, "content", None)
                if not content:
                    continue
                text_part = content if isinstance(content, str) else str(content)
                result_parts.append(text_part)
                received_content = True
                total_chars += len(text_part)
                if total_chars - reported_chars >= 2000:
                    reported_chars = total_chars
                    print(
                        f"OMNI_DRAFT received_chars={total_chars}",
                        file=sys.stderr,
                        flush=True,
                    )
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            last_error = str(exc)
            can_retry = (
                not received_content
                and status_code in RETRYABLE_HTTP_STATUS
                and attempt < retries
            )
            if not can_retry:
                break
            delay = min(2 ** (attempt - 1), 8)
            print(
                f"Omni 请求失败，{delay} 秒后重试：{last_error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
            continue

        result = "".join(result_parts).strip()
        if not result:
            last_error = "Omni 返回的初稿正文为空"
            break
        response_record: dict[str, Any] = {
            "model": payload["model"],
            "usage": usage,
            "finish_reason": finish_reason,
        }
        if raw_chunks is not None:
            response_record["chunks"] = raw_chunks
        print(
            f"OMNI_DRAFT complete chars={len(result)}",
            file=sys.stderr,
            flush=True,
        )
        return result, response_record

    raise ScriptError(f"Qwen3.5-Omni-Plus 调用失败：{last_error}")


def call_qwen(
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
    retries: int,
) -> tuple[str, dict[str, Any]]:
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error = ""
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            endpoint,
            data=request_body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            return response_text(body), body
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt == retries:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            break

        delay = min(2 ** (attempt - 1), 8)
        print(
            f"请求失败，第 {attempt}/{retries} 次，{delay} 秒后重试：{last_error}",
            file=sys.stderr,
        )
        time.sleep(delay)

    raise ScriptError(f"Qwen API 调用失败：{last_error}")


def write_json_output(
    path: Path, body: dict[str, Any], overwrite: bool, label: str
) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists() and not overwrite:
        raise ScriptError(f"{label}已存在；如需覆盖请增加 --overwrite：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if not overwrite:
        flags |= os.O_EXCL
    serialized = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise ScriptError(f"{label}已存在：{destination}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(serialized)
    return destination


def write_text_output(path: Path, content: str, overwrite: bool, label: str) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists() and not overwrite:
        raise ScriptError(f"{label}已存在；如需覆盖请增加 --overwrite：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if not overwrite:
        flags |= os.O_EXCL
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError as exc:
        raise ScriptError(f"{label}已存在：{destination}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content.rstrip() + "\n")
    return destination


def check_output_target(path: Path, overwrite: bool, label: str) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists() and not overwrite:
        raise ScriptError(f"{label}已存在；如需覆盖请增加 --overwrite：{destination}")
    return destination


def output_path(args: argparse.Namespace, video_path: Path) -> Path:
    if args.output:
        return check_output_target(args.output, args.overwrite, "最终提示词文件")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(__file__).resolve().parent / "outputs"
    return check_output_target(
        output_dir / f"{video_path.stem}_qwen3.8_prompt_{timestamp}.txt",
        args.overwrite,
        "最终提示词文件",
    )


def candidate_output_path(args: argparse.Namespace, final_path: Path) -> Path:
    path = args.candidate_output or final_path.with_name(
        f"{final_path.stem}_candidate{final_path.suffix}"
    )
    return check_output_target(path, args.overwrite, "Max 候选稿")


def draft_output_path(args: argparse.Namespace, final_path: Path) -> Path:
    if args.draft_output:
        return check_output_target(args.draft_output, args.overwrite, "视听提示词初稿")
    return check_output_target(
        final_path.with_name(f"{final_path.stem}_draft{final_path.suffix}"),
        args.overwrite,
        "视听提示词初稿",
    )


def draft_candidate_output_path(args: argparse.Namespace, draft_path: Path) -> Path:
    path = args.draft_candidate_output or draft_path.with_name(
        f"{draft_path.stem}_candidate{draft_path.suffix}"
    )
    return check_output_target(path, args.overwrite, "Omni 初稿候选稿")


def draft_metadata_output_path(args: argparse.Namespace, draft_path: Path) -> Path:
    path = args.draft_metadata_output or draft_path.with_name("prompt_draft_meta.json")
    return check_output_target(path, args.overwrite, "视听初稿元数据")


def draft_metadata_input_path(args: argparse.Namespace, draft_path: Path) -> Path:
    return (
        args.draft_metadata_file.expanduser().resolve()
        if args.draft_metadata_file
        else draft_path.with_name("prompt_draft_meta.json")
    )


def promote_candidate(
    candidate: Path,
    destination: Path,
    overwrite: bool,
    label: str,
) -> Path:
    content = require_readable_file(candidate, f"{label}候选稿").read_text(
        encoding="utf-8"
    )
    promoted = write_text_output(destination, content, overwrite, label)
    candidate.unlink()
    return promoted


def check_distinct_targets(targets: list[tuple[str, Path]]) -> None:
    seen: dict[Path, str] = {}
    for label, path in targets:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            raise ScriptError(f"{label}与{seen[resolved]}不能使用同一路径：{resolved}")
        seen[resolved] = label


def check_outputs_do_not_overlap_inputs(
    targets: list[tuple[str, Path]], inputs: list[Path | None]
) -> None:
    input_paths = {path.expanduser().resolve() for path in inputs if path is not None}
    for label, path in targets:
        resolved = path.expanduser().resolve()
        if resolved in input_paths:
            raise ScriptError(f"{label}不能覆盖任何输入文件：{resolved}")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            if not args.api_key_file:
                raise ScriptError(
                    "未设置 DASHSCOPE_API_KEY；请通过 --api-key-file 提供 Key 文件路径。"
                )
            api_key = load_api_key(args.api_key_file)

        system_prompt = optional_text_file(args.system_prompt, "系统提示词文件")

        transcript_path = (
            require_readable_file(args.transcript_file, "音轨转写文件")
            if args.transcript_file
            else None
        )
        transcript = optional_text_file(transcript_path, "音轨转写文件")
        local_video = require_readable_file(args.video, "参考视频")
        character_image = (
            require_readable_file(args.character_image, "人物形象图")
            if args.character_image
            else None
        )
        product_images = [
            require_readable_file(path, f"第{index}张产品参考图")
            for index, path in enumerate(args.product_image, start=1)
        ]
        metadata = probe_video(local_video)
        validate_video_api_limits(local_video, metadata)
        if character_image:
            validate_image_api_limits(
                character_image,
                "人物形象图",
                args.max_inline_request_mb,
            )
        for index, product_image in enumerate(product_images, start=1):
            validate_image_api_limits(
                product_image,
                f"第{index}张产品参考图",
                args.max_inline_request_mb,
            )
        aspect_ratio = args.aspect_ratio or metadata["aspect_ratio"]
        duration_seconds = args.duration_seconds or metadata["duration_seconds"]
        source_duration_seconds = float(metadata["source_duration"])
        has_audio = metadata["has_audio"]
        expected_image_count = int(character_image is not None) + len(product_images)
        audio_rewrite_allowed = bool(args.allow_audio_rewrite)
        spoken_replacements = parse_spoken_replacements(args.spoken_replacement)
        print(
            "已读取视频元数据："
            f"画幅 {aspect_ratio}，源时长 {source_duration_seconds:.3f} 秒，"
            f"提示词目标时长 {duration_seconds} 秒，"
            f"音轨{'存在' if has_audio else '不存在'}。",
            file=sys.stderr,
        )

        expected_draft_metadata = draft_input_metadata(
            args,
            local_video,
            character_image,
            product_images,
            transcript_path,
            aspect_ratio,
            duration_seconds,
        )

        destination = output_path(args, local_video)
        candidate_destination = candidate_output_path(args, destination)
        reuse_draft_path = (
            require_readable_file(args.draft_file, "视听提示词初稿")
            if args.draft_file
            else None
        )
        reuse_draft_metadata_path = (
            draft_metadata_input_path(args, reuse_draft_path)
            if reuse_draft_path
            else None
        )
        should_generate_draft = has_audio and reuse_draft_path is None
        draft_destination = (
            draft_output_path(args, destination) if should_generate_draft else None
        )
        draft_candidate_destination = (
            draft_candidate_output_path(args, draft_destination)
            if draft_destination
            else None
        )
        draft_meta_destination = (
            draft_metadata_output_path(args, draft_destination)
            if draft_destination
            else None
        )
        output_targets: list[tuple[str, Path]] = [
            ("最终提示词文件", destination),
            ("Max 候选稿", candidate_destination),
        ]
        optional_targets = [
            ("Max 请求体文件", args.request_body_output),
            ("Max 响应体文件", args.response_body_output),
            (
                "Omni 请求体文件",
                args.omni_request_body_output if should_generate_draft else None,
            ),
            (
                "Omni 响应体文件",
                args.omni_response_body_output if should_generate_draft else None,
            ),
            ("分段计划文件", args.segment_plan_output),
        ]
        if draft_destination:
            output_targets.append(("视听提示词初稿", draft_destination))
        if draft_candidate_destination:
            output_targets.append(("Omni 初稿候选稿", draft_candidate_destination))
        if draft_meta_destination:
            output_targets.append(("视听初稿元数据", draft_meta_destination))
        for label, path in optional_targets:
            if path is None:
                continue
            output_targets.append(
                (label, check_output_target(path, args.overwrite, label))
            )
        check_distinct_targets(output_targets)
        check_outputs_do_not_overlap_inputs(
            output_targets,
            [
                local_video,
                character_image,
                *product_images,
                transcript_path,
                reuse_draft_path,
                reuse_draft_metadata_path,
                args.system_prompt,
                args.omni_draft_addendum,
                args.max_refine_addendum,
                args.api_key_file,
            ],
        )

        draft_plan: dict[str, Any] | None = None
        draft_structure_error = ""
        draft = ""
        if reuse_draft_path:
            if reuse_draft_metadata_path is None:
                raise ScriptError("内部错误：复用初稿缺少元数据路径。")
            load_and_validate_draft_metadata(
                reuse_draft_metadata_path,
                expected_draft_metadata,
            )
            draft = reuse_draft_path.read_text(encoding="utf-8").strip()
            if not draft:
                raise ScriptError(f"视听提示词初稿内容为空：{reuse_draft_path}")
            draft_plan, draft_structure_error = validate_omni_draft_for_max(
                draft,
                duration_seconds,
                source_duration_seconds,
                args.segment_max_seconds,
                expected_image_count,
                "复用的 Omni 初稿",
            )
            if draft_structure_error:
                print(
                    "REUSE_DRAFT structure_delegated_to_max "
                    f"error={draft_structure_error}",
                    file=sys.stderr,
                    flush=True,
                )

        media_resolver = MediaResolver(args)
        video_reference = media_resolver.resolve(local_video, "video")
        character_reference = (
            media_resolver.resolve(character_image, "image")
            if character_image
            else None
        )
        product_references = [
            media_resolver.resolve(path, "image") for path in product_images
        ]

        if reuse_draft_path:
            print(f"REUSE_DRAFT file={reuse_draft_path}", file=sys.stderr, flush=True)
            refine_addendum = optional_text_file(
                args.max_refine_addendum,
                "Max 精修阶段提示词",
            )
            max_system_prompt = f"{system_prompt}\n\n---\n\n{refine_addendum}"
            max_user_text = build_refine_user_text(
                args,
                aspect_ratio,
                duration_seconds,
                draft,
                draft_plan,
                draft_structure_error,
            )
        elif has_audio:
            omni_addendum = optional_text_file(
                args.omni_draft_addendum,
                "Omni 初稿阶段提示词",
            )
            refine_addendum = optional_text_file(
                args.max_refine_addendum,
                "Max 精修阶段提示词",
            )
            draft_messages = build_messages(
                args,
                f"{system_prompt}\n\n---\n\n{omni_addendum}",
                video_reference,
                build_draft_user_text(
                    args,
                    aspect_ratio,
                    duration_seconds,
                    transcript,
                ),
                character_reference,
                product_references,
                include_fps=False,
            )
            omni_payload = {
                "model": args.omni_model,
                "messages": draft_messages,
                "temperature": min(args.temperature, 0.3),
                "max_tokens": args.max_tokens,
                "modalities": ["text"],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if args.omni_request_body_output:
                omni_request_path = write_json_output(
                    args.omni_request_body_output,
                    omni_payload,
                    args.overwrite,
                    "Omni 请求体文件",
                )
                print(f"Omni 请求体已保存：{omni_request_path}", file=sys.stderr)
            draft, omni_response = call_omni(
                args.base_url,
                api_key,
                omni_payload,
                args.timeout,
                args.retries,
                capture_chunks=bool(args.omni_response_body_output),
            )
            if args.omni_response_body_output:
                omni_response_path = write_json_output(
                    args.omni_response_body_output,
                    omni_response,
                    args.overwrite,
                    "Omni 响应体文件",
                )
                print(f"Omni 响应体已保存：{omni_response_path}", file=sys.stderr)
            if (
                draft_destination is None
                or draft_candidate_destination is None
                or draft_meta_destination is None
            ):
                raise ScriptError("内部错误：含音轨流程缺少初稿输出路径。")
            saved_draft_candidate = write_text_output(
                draft_candidate_destination,
                draft,
                args.overwrite,
                "Omni 初稿候选稿",
            )
            print(
                f"Omni 初稿候选稿已保存：{saved_draft_candidate}",
                file=sys.stderr,
            )
            require_complete_finish(
                str(omni_response.get("finish_reason") or ""),
                "Qwen3.5-Omni-Plus 初稿",
            )
            try:
                draft_plan, draft_structure_error = validate_omni_draft_for_max(
                    draft,
                    duration_seconds,
                    source_duration_seconds,
                    args.segment_max_seconds,
                    expected_image_count,
                    "Omni 初稿",
                )
            except ScriptError as validation_error:
                print(
                    "OMNI_DRAFT contract_repair_start "
                    f"error={validation_error}",
                    file=sys.stderr,
                    flush=True,
                )
                repair_payload = {
                    **omni_payload,
                    "messages": build_contract_repair_messages(
                        draft_messages,
                        draft,
                        str(validation_error),
                        "Omni 视听初稿",
                    ),
                    "temperature": min(args.temperature, 0.1),
                }
                draft, omni_response = call_omni(
                    args.base_url,
                    api_key,
                    repair_payload,
                    args.timeout,
                    args.retries,
                    capture_chunks=bool(args.omni_response_body_output),
                )
                require_complete_finish(
                    str(omni_response.get("finish_reason") or ""),
                    "Qwen3.5-Omni-Plus 初稿修复",
                )
                write_text_output(
                    draft_candidate_destination,
                    draft,
                    True,
                    "Omni 初稿修复候选稿",
                )
                draft_plan, draft_structure_error = validate_omni_draft_for_max(
                    draft,
                    duration_seconds,
                    source_duration_seconds,
                    args.segment_max_seconds,
                    expected_image_count,
                    "Omni 初稿修复稿",
                )
            if draft_structure_error:
                print(
                    "OMNI_DRAFT structure_delegated_to_max "
                    f"error={draft_structure_error}",
                    file=sys.stderr,
                    flush=True,
                )
            saved_draft = promote_candidate(
                draft_candidate_destination,
                draft_destination,
                args.overwrite,
                "视听提示词初稿",
            )
            write_json_output(
                draft_meta_destination,
                expected_draft_metadata,
                args.overwrite,
                "视听初稿元数据",
            )
            print(f"视听提示词初稿已保存：{saved_draft}", file=sys.stderr)
            max_system_prompt = f"{system_prompt}\n\n---\n\n{refine_addendum}"
            max_user_text = build_refine_user_text(
                args,
                aspect_ratio,
                duration_seconds,
                draft,
                draft_plan,
                draft_structure_error,
            )
        else:
            print("参考视频没有音轨，跳过 Omni 初稿阶段。", file=sys.stderr)
            max_system_prompt = system_prompt
            max_user_text = build_direct_user_text(
                args,
                aspect_ratio,
                duration_seconds,
                has_audio=False,
                transcript=transcript,
            )

        messages = build_messages(
            args,
            max_system_prompt,
            video_reference,
            max_user_text,
            character_reference,
            product_references,
        )
        payload = {
            "model": args.model,
            "messages": messages,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "enable_thinking": False,
        }
        if args.request_body_output:
            request_body_path = write_json_output(
                args.request_body_output,
                payload,
                args.overwrite,
                "Max 请求体文件",
            )
            print(f"Max 请求体已保存：{request_body_path}", file=sys.stderr)

        endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
        result, raw_response = call_qwen(
            endpoint, api_key, payload, args.timeout, args.retries
        )
        saved_candidate = write_text_output(
            candidate_destination,
            result,
            args.overwrite,
            "Max 候选稿",
        )
        print(f"Max 候选稿已保存：{saved_candidate}", file=sys.stderr)
        if args.response_body_output:
            response_body_path = write_json_output(
                args.response_body_output,
                raw_response,
                args.overwrite,
                "Max 响应体文件",
            )
            print(f"Max 响应体已保存：{response_body_path}", file=sys.stderr)
        require_complete_finish(
            completion_finish_reason(raw_response), "Qwen3.8-Max 精修"
        )
        try:
            segment_plan = validate_prompt_contract(
                result,
                duration_seconds,
                source_duration_seconds,
                args.segment_max_seconds,
                expected_image_count,
                "Max 终稿",
                draft_plan,
            )
            if draft and not draft_structure_error:
                require_same_shot_timeline(draft, result)
            if draft and not audio_rewrite_allowed:
                require_same_spoken_content(draft, result, spoken_replacements)
        except ScriptError as validation_error:
            print(
                "MAX_FINAL contract_repair_start "
                f"error={validation_error}",
                file=sys.stderr,
                flush=True,
            )
            repair_payload = {
                **payload,
                "messages": build_contract_repair_messages(
                    messages,
                    result,
                    str(validation_error),
                    "Max 最终提示词",
                    (
                        spoken_content_contract(draft, spoken_replacements)
                        if draft and not audio_rewrite_allowed
                        else ""
                    ),
                ),
                "temperature": min(args.temperature, 0.2),
            }
            result, raw_response = call_qwen(
                endpoint,
                api_key,
                repair_payload,
                args.timeout,
                args.retries,
            )
            require_complete_finish(
                completion_finish_reason(raw_response),
                "Qwen3.8-Max 精修修复",
            )
            write_text_output(
                candidate_destination,
                result,
                True,
                "Max 修复候选稿",
            )
            segment_plan = validate_prompt_contract(
                result,
                duration_seconds,
                source_duration_seconds,
                args.segment_max_seconds,
                expected_image_count,
                "Max 终稿修复稿",
                draft_plan,
            )
            if draft and not draft_structure_error:
                require_same_shot_timeline(draft, result)
            if draft and not audio_rewrite_allowed:
                require_same_spoken_content(draft, result, spoken_replacements)
        if args.segment_plan_output:
            segment_plan_path = write_json_output(
                args.segment_plan_output,
                segment_plan,
                args.overwrite,
                "分段计划文件",
            )
            print(f"分段计划已保存：{segment_plan_path}", file=sys.stderr)
        saved_result = promote_candidate(
            candidate_destination,
            destination,
            args.overwrite,
            "最终提示词文件",
        )
        print(f"最终提示词已保存：{saved_result}", file=sys.stderr)

        print(result)
        return 0
    except ScriptError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
