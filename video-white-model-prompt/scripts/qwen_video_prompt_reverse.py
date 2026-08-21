#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用阿里云百炼 Qwen3.8-Max 从参考视频反推视频生成提示词。

参考视频必须通过 --video 显式传入，原视频始终只读。
系统提示词从独立文本文件读取，并作为 system message 原样提交。

环境变量：
  DASHSCOPE_API_KEY   可选，阿里云百炼 API Key；也可传 --api-key-file
  DASHSCOPE_BASE_URL  可选，OpenAI 兼容接口根地址
  QWEN_MODEL          可选，默认 qwen3.8-max

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


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SYSTEM_PROMPT = SCRIPT_DIR.parent / "prompts" / "video_reverse_system_prompt.txt"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-max"
RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
DEFAULT_MAX_INLINE_REQUEST_MB = 100
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="调用 Qwen3.8-Max，从参考视频反推视频生成提示词。"
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
        "--request-body-output",
        type=Path,
        help="可选：保存实际发送的完整请求体 JSON，不包含请求头和 API Key。",
    )
    parser.add_argument(
        "--response-body-output",
        type=Path,
        help="可选：保存 API 返回的完整响应体 JSON。",
    )
    parser.add_argument(
        "--segment-plan-output",
        type=Path,
        help="可选：保存从提示词解析出的分段时长与累计切分时间点 JSON。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖显式指定的 --output 文件。",
    )
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=300, help="单次请求超时秒数。")
    parser.add_argument("--retries", type=int, default=2, help="最大请求次数，默认 2。")
    parser.add_argument(
        "--max-inline-request-mb",
        type=float,
        default=DEFAULT_MAX_INLINE_REQUEST_MB,
        help="Base64 内联媒体的估算上限 MiB，默认 100。",
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


def validate_args(args: argparse.Namespace) -> None:
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
    if args.max_inline_request_mb <= 0:
        raise ScriptError("--max-inline-request-mb 必须为正数。")


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


def file_to_data_url(path: Path, media_prefix: str) -> str:
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
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def estimated_inline_bytes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        size = path.stat().st_size
        total += 4 * ((size + 2) // 3) + 128
    return total


def optional_text_file(path: Path | None, label: str) -> str:
    if path is None:
        return ""
    resolved = require_readable_file(path, label)
    content = resolved.read_text(encoding="utf-8").strip()
    if not content:
        raise ScriptError(f"{label}内容为空：{resolved}")
    return content


def add_image_content(
    content: list[dict[str, Any]], path: Path, number: int, meaning: str
) -> None:
    resolved = require_readable_file(path, meaning)
    content.append({"type": "text", "text": f"图片{number}：{meaning}"})
    content.append(
        {
            "type": "image_url",
            "image_url": {"url": file_to_data_url(resolved, "image")},
        }
    )


def build_user_text(
    args: argparse.Namespace,
    aspect_ratio: str,
    duration_seconds: int,
    has_audio: bool | None,
    transcript: str,
) -> str:
    if transcript:
        audio_context = f"以下为参考视频音轨转写，请按时间和画面匹配使用：\n{transcript}"
    elif has_audio:
        audio_context = (
            "参考视频存在原始音轨。请直接理解音轨，准确提取口播、对白、旁白、"
            "BGM、环境声、音效及其时间位置；听不清的内容不要猜测或编造。"
        )
    elif has_audio is False:
        audio_context = "参考视频没有音轨，不要编造口播、对白或旁白。"
    else:
        audio_context = (
            "请直接检查并理解参考视频的原始音轨，准确提取其中的口播、对白、"
            "旁白、BGM、环境声和音效；听不清的内容不要猜测或编造。"
        )

    return "\n".join(
        [
            "请严格依据系统提示词分析本消息中的参考视频，只输出最终视频生成提示词正文。",
            "reference_video：本消息中的视频，仅用于提示词推理。",
            f"character_image：{'已按图片编号提供' if args.character_image else '未提供'}。",
            f"product_images：{'已按图片编号提供' if args.product_image else '未提供'}。",
            f"product_name：{args.product_name.strip() or '未提供'}。",
            f"selling_points：{args.selling_points.strip() or '未提供'}。",
            f"user_idea：{args.user_idea.strip() or '未提供'}。",
            f"aspect_ratio：{aspect_ratio}",
            f"duration_seconds：{duration_seconds}",
            f"segment_max_seconds：{args.segment_max_seconds}",
            f"音频补充信息：{audio_context}",
        ]
    )


def build_messages(
    args: argparse.Namespace,
    system_prompt: str,
    video_reference: str,
    aspect_ratio: str,
    duration_seconds: int,
    has_audio: bool | None,
    transcript: str,
) -> list[dict[str, Any]]:
    user_content: list[dict[str, Any]] = [
        {
            "type": "video_url",
            "video_url": {"url": video_reference},
            "fps": args.fps,
        }
    ]

    image_number = 1
    if args.character_image:
        add_image_content(user_content, args.character_image, image_number, "人物形象图")
        image_number += 1
    for index, product_image in enumerate(args.product_image, start=1):
        add_image_content(
            user_content,
            product_image,
            image_number,
            f"第{index}张产品参考图",
        )
        image_number += 1

    user_content.append(
        {
            "type": "text",
            "text": build_user_text(
                args,
                aspect_ratio,
                duration_seconds,
                has_audio,
                transcript,
            ),
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
            "API 返回结构中没有模型正文："
            + json.dumps(body, ensure_ascii=False)[:1000]
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


def build_segment_plan(
    result: str,
    prompt_duration_seconds: int,
    source_duration_seconds: float,
    segment_max_seconds: int,
) -> dict[str, Any]:
    header_pattern = re.compile(
        r"【[^】]*?段提示词[（(]\s*(\d+(?:\.\d+)?)\s*秒"
    )
    durations = [float(value) for value in header_pattern.findall(result)]
    if not durations:
        if prompt_duration_seconds > segment_max_seconds:
            raise ScriptError(
                "Qwen 返回内容未包含可解析的分段标题，无法驱动白模切分。"
            )
        durations = [float(prompt_duration_seconds)]

    if any(value <= 0 or value > segment_max_seconds for value in durations):
        raise ScriptError(
            f"Qwen 返回的分段时长必须在 0 到 {segment_max_seconds} 秒之间："
            f"{durations}"
        )
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
        "segment_max_seconds": segment_max_seconds,
        "prompt_duration_seconds": prompt_duration_seconds,
        "source_duration_seconds": source_duration_seconds,
        "segments": segments,
        "split_times_seconds": split_times,
    }


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


def output_path(args: argparse.Namespace, video_path: Path) -> Path:
    if args.output:
        path = args.output.expanduser().resolve()
        if path.exists() and not args.overwrite:
            raise ScriptError(f"输出文件已存在；如需覆盖请增加 --overwrite：{path}")
        return path

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(__file__).resolve().parent / "outputs"
    return output_dir / f"{video_path.stem}_qwen3.8_prompt_{timestamp}.txt"


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

        system_prompt_path = require_readable_file(args.system_prompt, "系统提示词文件")
        system_prompt = system_prompt_path.read_text(encoding="utf-8").strip()
        if not system_prompt:
            raise ScriptError(f"系统提示词文件内容为空：{system_prompt_path}")

        transcript = optional_text_file(args.transcript_file, "音轨转写文件")
        local_video = require_readable_file(args.video, "参考视频")
        metadata = probe_video(local_video)
        inline_media = [local_video]
        if args.character_image:
            inline_media.append(require_readable_file(args.character_image, "人物形象图"))
        inline_media.extend(
            require_readable_file(path, f"第{index}张产品参考图")
            for index, path in enumerate(args.product_image, start=1)
        )
        estimated_bytes = estimated_inline_bytes(inline_media)
        max_inline_bytes = int(args.max_inline_request_mb * 1024 * 1024)
        if estimated_bytes > max_inline_bytes:
            raise ScriptError(
                "Base64 内联媒体预计过大："
                f"{estimated_bytes / 1024**2:.2f} MiB > "
                f"{args.max_inline_request_mb:.2f} MiB。"
                "请先压缩参考视频或产品图片。"
            )
        print(
            f"预计 Base64 内联媒体大小：{estimated_bytes / 1024**2:.2f} MiB",
            file=sys.stderr,
        )
        video_reference = file_to_data_url(local_video, "video")
        aspect_ratio = args.aspect_ratio or metadata["aspect_ratio"]
        duration_seconds = args.duration_seconds or metadata["duration_seconds"]
        source_duration_seconds = float(metadata["source_duration"])
        has_audio = metadata["has_audio"]
        print(
            "已读取视频元数据："
            f"画幅 {aspect_ratio}，源时长 {source_duration_seconds:.3f} 秒，"
            f"提示词目标时长 {duration_seconds} 秒，"
            f"音轨{'存在' if has_audio else '不存在'}。",
            file=sys.stderr,
        )

        if has_audio and not transcript:
            print(
                "参考视频存在音轨，将由 Qwen3.8-Max 直接分析原始音频内容。",
                file=sys.stderr,
            )

        destination = output_path(args, local_video)
        if args.response_body_output:
            response_target = args.response_body_output.expanduser().resolve()
            if response_target.exists() and not args.overwrite:
                raise ScriptError(
                    "响应体文件已存在；如需覆盖请增加 --overwrite："
                    f"{response_target}"
                )
        if args.segment_plan_output:
            segment_plan_target = args.segment_plan_output.expanduser().resolve()
            if segment_plan_target.exists() and not args.overwrite:
                raise ScriptError(
                    "分段计划文件已存在；如需覆盖请增加 --overwrite："
                    f"{segment_plan_target}"
                )

        messages = build_messages(
            args,
            system_prompt,
            video_reference,
            aspect_ratio,
            duration_seconds,
            has_audio,
            transcript,
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
                "请求体文件",
            )
            print(f"请求体已保存：{request_body_path}", file=sys.stderr)

        endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
        result, raw_response = call_qwen(
            endpoint, api_key, payload, args.timeout, args.retries
        )
        if args.response_body_output:
            response_body_path = write_json_output(
                args.response_body_output,
                raw_response,
                args.overwrite,
                "响应体文件",
            )
            print(f"响应体已保存：{response_body_path}", file=sys.stderr)
        segment_plan = build_segment_plan(
            result,
            duration_seconds,
            source_duration_seconds,
            args.segment_max_seconds,
        )
        if args.segment_plan_output:
            segment_plan_path = write_json_output(
                args.segment_plan_output,
                segment_plan,
                args.overwrite,
                "分段计划文件",
            )
            print(f"分段计划已保存：{segment_plan_path}", file=sys.stderr)

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(result + "\n", encoding="utf-8")
        print(result)
        print(f"\n结果已保存：{destination}", file=sys.stderr)
        return 0
    except ScriptError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
