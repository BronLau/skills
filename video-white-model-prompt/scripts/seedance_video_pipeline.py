#!/usr/bin/env python3
"""Prepare and submit Seedance 2.5 generation tasks from verified prompt segments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable
from urllib.parse import quote

from media_preflight import (
    MediaPreflightError,
    validate_seedance_image_input as validate_seedance_image_input_shared,
)
from qwen_video_prompt_reverse import SEGMENT_HEADER_PATTERN


MODEL_ID = "doubao-seedance-2-5-260628"
DEFAULT_RESOLUTION = "720p"
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MIN_GENERATION_SECONDS = 4
MAX_GENERATION_SECONDS = 30
MIN_REFERENCE_VIDEO_SECONDS = 2
MAX_REFERENCE_VIDEO_SECONDS = 30
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
SUPPORTED_RATIOS = {
    "21:9": (1470, 630),
    "16:9": (1280, 720),
    "4:3": (1112, 834),
    "1:1": (960, 960),
    "3:4": (834, 1112),
    "9:16": (720, 1280),
}
TASK_INTENT_CONFLICTS = (
    re.compile(r"编辑\s*(?:@视频\d+|视频)"),
    re.compile(r"(?:向前|向后)延长"),
    re.compile(r"续写\s*@视频\d+"),
    re.compile(r"(?:删除|去掉|修改)\s*@视频\d+"),
)


class SeedanceError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="准备或提交 Doubao Seedance 2.5 分段视频生成任务。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--prompt", type=Path, required=True)
    prepare.add_argument("--segment-plan", type=Path, required=True)
    prepare.add_argument("--source-video", type=Path, required=True)
    prepare.add_argument("--depth-dir", type=Path)
    prepare.add_argument("--character-image", type=Path)
    prepare.add_argument("--product-image", type=Path, action="append", default=[])
    prepare.add_argument("--transcript-file", type=Path)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument(
        "--resolution",
        choices=("480p", "720p", "1080p"),
        default=DEFAULT_RESOLUTION,
    )
    prepare.add_argument(
        "--ratio",
        choices=("source", "adaptive", *SUPPORTED_RATIOS.keys()),
        default="source",
    )
    prepare.add_argument("--output-format", choices=("mp4", "mov"), default="mp4")
    prepare.add_argument(
        "--generate-audio", choices=("auto", "true", "false"), default="auto"
    )
    prepare.add_argument("--watermark", action="store_true")
    prepare.add_argument("--seed", type=int)
    prepare.add_argument("--overwrite", action="store_true")

    submit = subparsers.add_parser("submit")
    submit.add_argument("--plan", type=Path, required=True)
    submit.add_argument("--ark-api-key-file", type=Path)
    submit.add_argument("--tos-config-file", type=Path)
    submit.add_argument("--poll-interval", type=float, default=30.0)
    submit.add_argument("--poll-timeout", type=float, default=7200.0)
    submit.add_argument("--signed-url-ttl", type=int, default=7 * 24 * 3600)
    submit.add_argument("--retry-failed", action="store_true")
    submit.add_argument("--allow-recreate-ambiguous", action="store_true")
    return parser.parse_args()


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.R_OK):
        raise SeedanceError(f"{label}不存在或不可读：{resolved}")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> dict[str, Any]:
    resolved = require_file(path, "输入文件")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": file_sha256(resolved),
    }


def validate_identity(identity: dict[str, Any], label: str) -> Path:
    path = require_file(Path(str(identity["path"])), label)
    actual = file_identity(path)
    if actual != identity:
        raise SeedanceError(f"{label}在计划生成后发生变化，拒绝继续：{path}")
    return path


def atomic_write_json(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_json(path: Path, label: str) -> dict[str, Any]:
    resolved = require_file(path, label)
    try:
        body = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SeedanceError(f"{label}不是有效 JSON：{resolved}") from exc
    if not isinstance(body, dict):
        raise SeedanceError(f"{label}根节点必须是对象：{resolved}")
    return body


def parse_frame_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SeedanceError("未找到 ffprobe，无法检查 Seedance 视频素材。")
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate",
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
        streams = metadata.get("streams") or []
        video_stream = next(
            stream for stream in streams if stream.get("codec_type") == "video"
        )
        duration = float(metadata["format"]["duration"])
        width = int(video_stream["width"])
        height = int(video_stream["height"])
        fps = parse_frame_rate(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        )
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
    ) as exc:
        raise SeedanceError(f"ffprobe 无法读取视频：{path}") from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise SeedanceError(f"视频元数据无效：{path}")
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "codec": str(video_stream.get("codec_name") or ""),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def closest_supported_ratio(width: int, height: int) -> str:
    ratio = width / height
    return min(
        SUPPORTED_RATIOS,
        key=lambda name: abs(
            ratio - SUPPORTED_RATIOS[name][0] / SUPPORTED_RATIOS[name][1]
        ),
    )


def output_ratio(requested: str, source: dict[str, Any]) -> str:
    if requested != "source":
        return requested
    ratio_name = closest_supported_ratio(int(source["width"]), int(source["height"]))
    expected = SUPPORTED_RATIOS[ratio_name][0] / SUPPORTED_RATIOS[ratio_name][1]
    actual = float(source["width"]) / float(source["height"])
    return ratio_name if abs(actual - expected) <= expected * 0.015 else "adaptive"


def validate_seedance_image(path: Path, label: str) -> None:
    try:
        validate_seedance_image_input_shared(path, label)
    except MediaPreflightError as exc:
        raise SeedanceError(str(exc)) from exc


def depth_video_compatible(metadata: dict[str, Any]) -> bool:
    width = int(metadata["width"])
    height = int(metadata["height"])
    ratio = width / height
    pixels = width * height
    return (
        metadata["codec"] in {"h264", "hevc"}
        and 24 <= float(metadata["fps"]) <= 60
        and MIN_REFERENCE_VIDEO_SECONDS
        <= float(metadata["duration"])
        <= MAX_REFERENCE_VIDEO_SECONDS
        and 300 <= width <= 6000
        and 300 <= height <= 6000
        and 0.4 <= ratio <= 2.5
        and 407_696 <= pixels <= 8_295_044
        and not metadata["has_audio"]
    )


def normalize_depth_video(source: Path, destination: Path) -> Path:
    metadata = probe_video(source)
    if depth_video_compatible(metadata):
        return source
    if not (
        MIN_REFERENCE_VIDEO_SECONDS
        <= float(metadata["duration"])
        <= MAX_REFERENCE_VIDEO_SECONDS
    ):
        raise SeedanceError(
            "白模参考视频时长必须在 2 到 30 秒之间："
            f"{metadata['duration']:.3f} 秒，{source}"
        )
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SeedanceError("未找到 ffmpeg，无法生成 Seedance 兼容白模。")
    width = int(metadata["width"])
    height = int(metadata["height"])
    ratio = width / height
    pixels = width * height
    target_fps = min(60.0, max(24.0, float(metadata["fps"] or 24.0)))
    filters: list[str] = []
    dimensions_valid = (
        300 <= width <= 6000
        and 300 <= height <= 6000
        and 0.4 <= ratio <= 2.5
        and 407_696 <= pixels <= 8_295_044
    )
    if not dimensions_valid:
        ratio_name = closest_supported_ratio(width, height)
        out_width, out_height = SUPPORTED_RATIOS[ratio_name]
        filters.append(
            f"scale={out_width}:{out_height}:force_original_aspect_ratio=decrease"
        )
        filters.append(f"pad={out_width}:{out_height}:(ow-iw)/2:(oh-ih)/2:black")
    filters.append(f"fps={target_fps:.6g}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        ",".join(filters),
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise SeedanceError(f"Seedance 白模兼容转码失败：{source}") from exc
    converted = probe_video(destination)
    if not depth_video_compatible(converted):
        raise SeedanceError(f"转码后的白模仍不符合 Seedance 要求：{destination}")
    if abs(float(converted["duration"]) - float(metadata["duration"])) > 0.15:
        raise SeedanceError(f"白模兼容转码改变了视频时长：{destination}")
    return destination


def validate_segment_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        maximum = int(plan["segment_max_seconds"])
        segments = list(plan["segments"])
        prompt_duration = int(plan["prompt_duration_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedanceError("segment_plan.json 缺少有效的分段信息。") from exc
    if maximum not in (15, 30) or prompt_duration < MIN_GENERATION_SECONDS:
        raise SeedanceError("segment_plan.json 不符合 Seedance 时长约束。")
    expected_count = math.ceil(prompt_duration / maximum)
    if len(segments) != expected_count:
        raise SeedanceError(
            f"Seedance 分段必须使用最少任务数：{len(segments)} != {expected_count}"
        )
    total = 0
    for expected_index, segment in enumerate(segments, start=1):
        try:
            index = int(segment["index"])
            duration = float(segment["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SeedanceError(f"第 {expected_index} 段信息无效。") from exc
        if index != expected_index:
            raise SeedanceError("segment_plan.json 的分段编号不连续。")
        if not MIN_GENERATION_SECONDS <= duration <= maximum:
            raise SeedanceError(f"第 {index} 段时长不在 4 到 {maximum} 秒之间。")
        if abs(duration - round(duration)) > 0.02:
            raise SeedanceError(f"第 {index} 段时长不是整数秒：{duration}")
        total += int(round(duration))
    if total != prompt_duration:
        raise SeedanceError(f"分段时长总和不匹配：{total} != {prompt_duration}")
    return segments


def split_prompt(prompt: str, segments: list[dict[str, Any]]) -> list[str]:
    matches = list(SEGMENT_HEADER_PATTERN.finditer(prompt))
    if len(segments) == 1 and not matches:
        body = prompt.strip()
        if not body:
            raise SeedanceError("最终提示词为空。")
        return [body]
    if len(matches) != len(segments):
        raise SeedanceError(
            f"提示词分段标题数量与计划不一致：{len(matches)} != {len(segments)}"
        )
    if prompt[: matches[0].start()].strip():
        raise SeedanceError("第一段标题之前存在额外内容。")
    bodies: list[str] = []
    for index, match in enumerate(matches):
        expected_duration = float(segments[index]["duration_seconds"])
        actual_duration = float(match.group("duration"))
        if abs(actual_duration - expected_duration) > 0.02:
            raise SeedanceError(f"第 {index + 1} 段标题时长与计划不一致。")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        body = prompt[match.end() : end].strip()
        if not body:
            raise SeedanceError(f"第 {index + 1} 段提示词为空。")
        bodies.append(body)
    return bodies


def compile_prompt(body: str, image_count: int, with_depth: bool) -> str:
    if re.search(r"@(?:视频|音频)\d+", body):
        raise SeedanceError("Max 正式稿不得预先包含 @视频N 或 @音频N 引用。")
    compiled = re.sub(r"(?<!@)图片(?P<number>\d+)", r"@图片\g<number>", body)
    compiled = compiled.replace("4K高清", "高清").replace("4K 高清", "高清")
    for pattern in TASK_INTENT_CONFLICTS:
        if pattern.search(compiled):
            raise SeedanceError(
                "最终提示词包含视频编辑或延长意图，拒绝按参考生视频提交。"
            )
    indices = [int(value) for value in re.findall(r"@图片(\d+)", compiled)]
    if indices and max(indices) > image_count:
        raise SeedanceError(
            f"提示词引用了不存在的 @图片{max(indices)}，实际只有 {image_count} 张。"
        )
    if with_depth:
        appearance_source = (
            "人物、产品和场景外观以文字及@图片N为准。"
            if image_count
            else "人物、产品和场景外观以文字提示为准。"
        )
        prefix = (
            "@视频1是本段细粒度深度白模参考，只提供主体结构、动作、"
            "空间布局、机位、运镜、切镜和时间节奏；不采用其中近白远黑的"
            f"深度可视化材质与灰阶颜色。{appearance_source}"
        )
        return f"{prefix}\n{compiled.strip()}"
    return compiled.strip()


def prepare(args: argparse.Namespace) -> Path:
    prompt_path = require_file(args.prompt, "最终提示词")
    plan_path = require_file(args.segment_plan, "分段计划")
    source_video = require_file(args.source_video, "原始参考视频")
    character_image = (
        require_file(args.character_image, "人物形象图")
        if args.character_image
        else None
    )
    product_images = [
        require_file(path, f"第 {index} 张产品图")
        for index, path in enumerate(args.product_image, start=1)
    ]
    transcript_file = (
        require_file(args.transcript_file, "音轨转写文件")
        if args.transcript_file
        else None
    )
    images = ([character_image] if character_image else []) + product_images
    for index, image in enumerate(images, start=1):
        validate_seedance_image(image, f"@图片{index}")

    output_dir = args.output_dir.expanduser().resolve()
    plan_output = output_dir / "seedance_plan.json"
    if plan_output.exists() and not args.overwrite:
        raise SeedanceError(f"Seedance 计划已存在，拒绝覆盖：{plan_output}")
    output_dir.mkdir(parents=True, exist_ok=True)
    prompts_dir = output_dir / "prompts"
    assets_dir = output_dir / "assets"
    generated_dir = output_dir / "generated"
    responses_dir = output_dir / "responses"
    for directory in (prompts_dir, assets_dir, generated_dir, responses_dir):
        directory.mkdir(exist_ok=True)

    segment_plan = load_json(plan_path, "分段计划")
    segments = validate_segment_plan(segment_plan)
    prompt_bodies = split_prompt(prompt_path.read_text(encoding="utf-8"), segments)
    depth_files: list[Path] = []
    if args.depth_dir:
        depth_dir = args.depth_dir.expanduser().resolve()
        if not depth_dir.is_dir():
            raise SeedanceError(f"白模目录不存在：{depth_dir}")
        depth_files = sorted(depth_dir.glob("*_depth_720p_part_*.mp4"))
        if len(depth_files) != len(segments):
            raise SeedanceError(
                f"白模分段数量与提示词不一致：{len(depth_files)} != {len(segments)}"
            )

    source_metadata = probe_video(source_video)
    if float(source_metadata["duration"]) < MIN_GENERATION_SECONDS:
        raise SeedanceError("Seedance 成片模式要求原始参考视频至少 4 秒。")
    ratio = output_ratio(args.ratio, source_metadata)
    if args.generate_audio == "auto":
        generate_audio = bool(source_metadata["has_audio"] or transcript_file)
    else:
        generate_audio = args.generate_audio == "true"
    seed = args.seed if args.seed is not None else secrets.randbelow(2**32)
    if not -1 <= seed <= 2**32 - 1:
        raise SeedanceError("--seed 必须在 -1 到 2^32-1 之间。")

    image_assets = [
        {
            "id": f"image-{index:02d}",
            "index": index,
            "kind": "image",
            "identity": file_identity(image),
        }
        for index, image in enumerate(images, start=1)
    ]
    prepared_segments: list[dict[str, Any]] = []
    for segment, body in zip(segments, prompt_bodies):
        index = int(segment["index"])
        depth_identity = None
        if depth_files:
            normalized = normalize_depth_video(
                depth_files[index - 1], assets_dir / f"depth_part_{index:02d}.mp4"
            )
            depth_identity = file_identity(normalized)
        compiled = compile_prompt(body, len(images), bool(depth_files))
        prompt_file = prompts_dir / f"part_{index:02d}.txt"
        prompt_file.write_text(compiled.rstrip() + "\n", encoding="utf-8")
        prepared_segments.append(
            {
                "index": index,
                "duration_seconds": int(round(float(segment["duration_seconds"]))),
                "prompt": file_identity(prompt_file),
                "depth_video": depth_identity,
                "output_file": str(
                    generated_dir / f"part_{index:02d}.{args.output_format}"
                ),
            }
        )

    body = {
        "schema_version": 1,
        "status": "prepared",
        "run_id": uuid.uuid4().hex,
        "model": MODEL_ID,
        "mode": "depth-reference" if depth_files else "text-and-image-reference",
        "source_video": file_identity(source_video),
        "prompt": file_identity(prompt_path),
        "segment_plan": file_identity(plan_path),
        "images": image_assets,
        "parameters": {
            "resolution": args.resolution,
            "ratio": ratio,
            "output_format": args.output_format,
            "watermark": bool(args.watermark),
            "generate_audio": generate_audio,
            "seed": seed,
        },
        "full_output_file": str(generated_dir / f"full.{args.output_format}"),
        "segments": prepared_segments,
    }
    atomic_write_json(plan_output, body)
    print(f"SEEDANCE prepared plan={plan_output} tasks={len(prepared_segments)}")
    return plan_output


def load_key_file(path: Path, label: str, variable_name: str) -> str:
    content = require_file(path, label).read_text(encoding="utf-8")
    accepted_labels = [variable_name]
    if variable_name == "ARK_API_KEY":
        accepted_labels.append("Volcengine_API_KEY")
    for accepted_label in accepted_labels:
        assignment = re.search(
            rf"(?m)^\s*{re.escape(accepted_label)}\s*[:=]\s*"
            r"[`\"']?([^\s`\"']+)",
            content,
        )
        if assignment:
            return assignment.group(1).strip()
    for line in content.splitlines():
        candidate = line.strip().strip("`\"'")
        if ":" in candidate or "=" in candidate:
            candidate = re.split(r"[:=]", candidate, maxsplit=1)[1].strip()
        if candidate and not candidate.startswith("#") and len(candidate) >= 16:
            return candidate
    raise SeedanceError(f"{label}中没有可用的 {variable_name}。")


def resolve_ark_api_key(path: Path | None) -> str:
    value = os.environ.get("ARK_API_KEY", "").strip()
    if value:
        return value
    if path is None:
        raise SeedanceError("未设置 ARK_API_KEY，也未提供 --ark-api-key-file。")
    return load_key_file(path, "Ark API Key 文件", "ARK_API_KEY")


def markdown_tos_value(content: str, name: str) -> str:
    if name == "endpoint":
        match = re.search(r"\bendpoint:\s*\[([^\]]+)\]", content)
    else:
        match = re.search(rf"\b{re.escape(name)}:\s*([^\s]+)", content)
    return match.group(1).strip().strip("`\"'") if match else ""


def normalize_tos_config(body: dict[str, Any]) -> dict[str, str]:
    aliases = {
        "access_key": ("access_key", "accessKey"),
        "secret_key": ("secret_key", "secretKey"),
        "endpoint": ("endpoint",),
        "region": ("region",),
        "bucket": ("bucket",),
        "security_token": ("security_token", "securityToken"),
        "role_trn": ("role_trn", "roleTrn"),
        "public_domain": ("public_domain", "publicDomain"),
        "prefix": ("prefix",),
        "main_path": ("main_path", "mainPath"),
    }
    config: dict[str, str] = {}
    for normalized, candidates in aliases.items():
        for candidate in candidates:
            value = body.get(candidate)
            if value:
                config[normalized] = str(value).strip()
                break
    return config


def load_tos_config(path: Path | None) -> dict[str, str]:
    config: dict[str, str] = {}
    if path:
        resolved = path.expanduser().resolve()
        if resolved.is_dir():
            resolved = resolved / "Volc engine_API_KEY.md"
        content = require_file(resolved, "火山 TOS 配置文件").read_text(
            encoding="utf-8"
        )
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {
                name: markdown_tos_value(content, name)
                for name in (
                    "endpoint",
                    "region",
                    "accessKey",
                    "secretKey",
                    "bucket",
                    "roleTrn",
                    "publicDomain",
                    "mainPath",
                )
            }
        if not isinstance(parsed, dict):
            raise SeedanceError("火山 TOS 配置根节点必须是对象。")
        config.update(normalize_tos_config(parsed))
    environment_mapping = {
        "access_key": "TOS_ACCESS_KEY",
        "secret_key": "TOS_SECRET_KEY",
        "endpoint": "TOS_ENDPOINT",
        "region": "TOS_REGION",
        "bucket": "TOS_BUCKET",
        "security_token": "TOS_SECURITY_TOKEN",
        "prefix": "TOS_PREFIX",
        "role_trn": "TOS_ROLE_TRN",
        "public_domain": "TOS_PUBLIC_DOMAIN",
        "main_path": "TOS_MAIN_PATH",
    }
    for key, environment_name in environment_mapping.items():
        value = os.environ.get(environment_name, "").strip()
        if value:
            config[key] = value
    required = ("access_key", "secret_key", "endpoint", "region", "bucket")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SeedanceError("火山 TOS 配置缺少字段：" + ", ".join(missing))
    if not config.get("prefix"):
        main_path = config.get("main_path", "").strip("/")
        config["prefix"] = (
            f"{main_path}/video-white-model-prompt"
            if main_path
            else "video-white-model-prompt"
        )
    if config.get("public_domain"):
        config["public_domain"] = config["public_domain"].rstrip("/")
    return config


class TosPublisher:
    def __init__(self, config: dict[str, str], signed_url_ttl: int) -> None:
        try:
            import tos
        except ImportError as exc:
            raise SeedanceError("缺少火山 TOS SDK：python3 -m pip install tos") from exc
        self.tos = tos
        self.bucket = config["bucket"]
        self.prefix = config["prefix"].strip("/")
        self.config = config
        self.public_domain = config.get("public_domain", "").rstrip("/")
        self.signed_url_ttl = signed_url_ttl

    def client(self) -> Any:
        access_key = self.config["access_key"]
        secret_key = self.config["secret_key"]
        security_token = self.config.get("security_token") or None
        role_trn = self.config.get("role_trn")
        if role_trn:
            try:
                from volcengine.sts.StsService import StsService
            except ImportError as exc:
                raise SeedanceError(
                    "STS AssumeRole 需要 volcengine SDK："
                    "python3 -m pip install volcengine"
                ) from exc
            sts = StsService()
            sts.set_ak(access_key)
            sts.set_sk(secret_key)
            assumed = sts.assume_role(
                {
                    "DurationSeconds": "900",
                    "RoleSessionName": "video-white-model-prompt",
                    "RoleTrn": role_trn,
                }
            )
            try:
                credentials = assumed["Result"]["Credentials"]
                access_key = credentials["AccessKeyId"]
                secret_key = credentials["SecretAccessKey"]
                security_token = credentials["SessionToken"]
            except (KeyError, TypeError) as exc:
                raise SeedanceError("STS AssumeRole 响应中缺少临时凭证。") from exc
        return self.tos.TosClientV2(
            ak=access_key,
            sk=secret_key,
            endpoint=self.config["endpoint"],
            region=self.config["region"],
            security_token=security_token,
        )

    def upload(self, path: Path, run_id: str, asset_id: str) -> dict[str, Any]:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)
        object_key = (
            f"{self.prefix}/{run_id}/{asset_id}_{file_sha256(path)[:16]}_{safe_name}"
        )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.client().put_object_from_file(
            self.bucket,
            object_key,
            str(path),
            content_type=content_type,
        )
        return self.sign(object_key)

    def sign(self, object_key: str) -> dict[str, Any]:
        if self.public_domain:
            return {
                "object_key": object_key,
                "url": f"{self.public_domain}/{quote(object_key, safe='/')}",
                "url_expires_at": 2**31 - 1,
            }
        if self.config.get("role_trn"):
            raise SeedanceError(
                "STS 临时凭证模式需要配置 publicDomain，"
                "避免签名 URL 超过临时凭证有效期。"
            )
        result = self.client().pre_signed_url(
            self.tos.HttpMethodType.Http_Method_Get,
            self.bucket,
            object_key,
            expires=self.signed_url_ttl,
        )
        return {
            "object_key": object_key,
            "url": result.signed_url,
            "url_expires_at": int(time.time()) + self.signed_url_ttl,
        }


def sdk_json(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    result = {
        key: getattr(value, key)
        for key in dir(value)
        if not key.startswith("_")
        and not callable(getattr(value, key))
        and isinstance(getattr(value, key), (str, int, float, bool, type(None)))
    }
    return result


def build_request(
    plan: dict[str, Any],
    segment: dict[str, Any],
    image_urls: list[str],
    depth_url: str | None,
) -> dict[str, Any]:
    prompt = (
        validate_identity(segment["prompt"], "Seedance 分段提示词")
        .read_text(encoding="utf-8")
        .strip()
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_url in image_urls:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url},
                "role": "reference_image",
            }
        )
    if depth_url:
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": depth_url},
                "role": "reference_video",
            }
        )
    parameters = plan["parameters"]
    body = {
        "model": plan["model"],
        "content": content,
        "generate_audio": bool(parameters["generate_audio"]),
        "ratio": parameters["ratio"],
        "duration": int(segment["duration_seconds"]),
        "resolution": parameters["resolution"],
        "watermark": bool(parameters["watermark"]),
        "seed": int(parameters["seed"]),
        "output_format": parameters["output_format"],
    }
    if image_urls or depth_url:
        body["omni_reference_task_type"] = "reference"
    return body


def create_task(client: Any, request_body: dict[str, Any]) -> str:
    standard = {
        key: request_body[key]
        for key in (
            "model",
            "content",
            "generate_audio",
            "ratio",
            "duration",
            "resolution",
            "watermark",
            "seed",
        )
    }
    extra_body = {
        key: request_body[key]
        for key in ("omni_reference_task_type", "output_format")
        if key in request_body
    }
    result = client.content_generation.tasks.create(
        **standard,
        extra_body=extra_body,
    )
    task_id = str(getattr(result, "id", "") or "")
    if not task_id:
        raise SeedanceError("Seedance 创建任务响应中没有 task_id。")
    return task_id


def poll_task(
    client: Any,
    task_id: str,
    interval: float,
    timeout: float,
    on_update: Callable[[dict[str, Any]], None],
    stop_event: Event | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        if stop_event and stop_event.is_set():
            raise SeedanceError(f"Seedance 任务轮询已取消：{task_id}")
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = client.content_generation.tasks.get(task_id=task_id)
                body = sdk_json(result)
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt == 3:
                    raise SeedanceError(
                        f"查询 Seedance 任务失败：{task_id}：{last_error}"
                    ) from exc
                delay = min(2 ** (attempt - 1), 4)
                if stop_event:
                    if stop_event.wait(delay):
                        raise SeedanceError(f"Seedance 任务轮询已取消：{task_id}")
                else:
                    time.sleep(delay)
        status = str(body.get("status") or "")
        if not status:
            raise SeedanceError(f"Seedance 查询响应没有状态：{task_id}")
        on_update(body)
        print(f"SEEDANCE task={task_id} status={status}", flush=True)
        if status in TERMINAL_STATUSES:
            return body
        if time.monotonic() >= deadline:
            raise SeedanceError(f"Seedance 任务轮询超时，可稍后恢复：{task_id}")
        if stop_event:
            if stop_event.wait(interval):
                raise SeedanceError(f"Seedance 任务轮询已取消：{task_id}")
        else:
            time.sleep(interval)


def result_video_url(body: dict[str, Any]) -> str:
    content = body.get("content") or {}
    if isinstance(content, dict):
        return str(content.get("video_url") or content.get("file_url") or "")
    return str(getattr(content, "video_url", "") or getattr(content, "file_url", ""))


def download_video(url: str, destination: Path, attempts: int = 3) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Codex/Seedance"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
            if temporary.stat().st_size == 0:
                raise SeedanceError("下载结果为空文件。")
            os.replace(temporary, destination)
            return
        except Exception as exc:
            last_error = str(exc)
            if temporary.exists():
                temporary.unlink()
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise SeedanceError(f"Seedance 成片下载失败：{last_error}")


def validate_generated_video(
    path: Path,
    expected_duration: int,
    expect_audio: bool,
    duration_tolerance: float = 1.0,
) -> None:
    metadata = probe_video(path)
    if abs(float(metadata["duration"]) - expected_duration) > duration_tolerance:
        raise SeedanceError(
            "Seedance 成片时长偏差超过允许范围："
            f"{metadata['duration']:.3f} != {expected_duration}，"
            f"tolerance={duration_tolerance:.3f}，{path}"
        )
    if expect_audio and not metadata["has_audio"]:
        raise SeedanceError(f"Seedance 请求了音频，但成片没有音轨：{path}")


def archive_invalid_output(path: Path) -> Path:
    index = 1
    while True:
        archived = path.with_name(f"{path.stem}.invalid_{index}{path.suffix}")
        if not archived.exists():
            path.rename(archived)
            return archived
        index += 1


def download_and_validate_video(
    url: str,
    destination: Path,
    expected_duration: int,
    expect_audio: bool,
    validation_attempts: int = 2,
) -> None:
    last_error = ""
    for attempt in range(1, validation_attempts + 1):
        download_video(url, destination)
        try:
            validate_generated_video(destination, expected_duration, expect_audio)
            return
        except SeedanceError as exc:
            last_error = str(exc)
            archived = archive_invalid_output(destination)
            print(
                "SEEDANCE invalid_download "
                f"attempt={attempt}/{validation_attempts} archived={archived}",
                flush=True,
            )
    raise SeedanceError("Seedance 成片重复下载后仍未通过校验：" + last_error)


def ffconcat_path(path: Path) -> str:
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"file '{escaped}'"


def concat_generated_videos(
    parts: list[Path],
    destination: Path,
    expected_duration: int,
    expect_audio: bool,
) -> Path:
    if not parts:
        raise SeedanceError("没有可供拼接的 Seedance 分段成片。")
    resolved_parts = [require_file(path, "Seedance 分段成片") for path in parts]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SeedanceError("未找到 ffmpeg，无法拼接 Seedance 分段成片。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = destination.with_name(
        f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}"
    )
    concat_list: Path | None = None
    try:
        descriptor, list_name = tempfile.mkstemp(
            prefix=".seedance-concat-",
            suffix=".txt",
            dir=destination.parent,
            text=True,
        )
        concat_list = Path(list_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for part in resolved_parts:
                stream.write(ffconcat_path(part) + "\n")
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temporary_output),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise SeedanceError(
                "FFmpeg 顺序拼接失败；未进行降质重编码："
                + completed.stderr.strip()[-2000:]
            )
        os.replace(temporary_output, destination)
        validate_generated_video(
            destination,
            expected_duration,
            expect_audio,
            duration_tolerance=max(1.0, len(parts) * 0.25),
        )
        return destination
    except SeedanceError:
        if destination.exists():
            archive_invalid_output(destination)
        raise
    finally:
        if concat_list and concat_list.exists():
            concat_list.unlink()
        if temporary_output.exists():
            temporary_output.unlink()


def submit(
    args: argparse.Namespace,
    client_factory: Callable[[str], Any] | None = None,
    publisher_factory: Callable[[dict[str, str], int], Any] | None = None,
) -> Path:
    plan_path = require_file(args.plan, "Seedance 计划")
    plan = load_json(plan_path, "Seedance 计划")
    if plan.get("status") != "prepared" or plan.get("model") != MODEL_ID:
        raise SeedanceError("Seedance 计划状态或模型无效。")
    validate_identity(plan["source_video"], "原始参考视频")
    validate_identity(plan["prompt"], "最终提示词")
    validate_identity(plan["segment_plan"], "分段计划")
    for asset in plan.get("images") or []:
        validate_identity(asset["identity"], f"{asset['id']} 图片")
    for segment in plan["segments"]:
        validate_identity(segment["prompt"], f"第 {segment['index']} 段提示词")
        if segment.get("depth_video"):
            validate_identity(segment["depth_video"], f"第 {segment['index']} 段白模")

    api_key = resolve_ark_api_key(args.ark_api_key_file)
    tos_config = (
        load_tos_config(args.tos_config_file)
        if (
            plan.get("images")
            or any(segment.get("depth_video") for segment in plan["segments"])
        )
        else None
    )
    if client_factory is None:
        try:
            from volcenginesdkarkruntime import Ark
        except ImportError as exc:
            raise SeedanceError(
                "缺少 Ark SDK：python3 -m pip install 'volcengine-python-sdk[ark]'"
            ) from exc

        def default_client_factory(key: str) -> Any:
            return Ark(
                base_url=ARK_BASE_URL,
                api_key=key,
                max_retries=0,
            )

        client_factory = default_client_factory
    client = client_factory(api_key)
    publisher = None
    if tos_config:
        factory = publisher_factory or TosPublisher
        publisher = factory(tos_config, args.signed_url_ttl)

    state_path = plan_path.with_name("tasks.json")
    if state_path.exists():
        state = load_json(state_path, "Seedance 任务状态")
        if state.get("run_id") != plan["run_id"]:
            raise SeedanceError("Seedance 任务状态不属于当前计划。")
    else:
        state = {
            "schema_version": 1,
            "run_id": plan["run_id"],
            "uploads": {},
            "segments": {},
        }
        atomic_write_json(state_path, state)

    def asset_url(asset_id: str, identity: dict[str, Any]) -> str:
        if publisher is None:
            raise SeedanceError("内部错误：缺少 TOS 发布器。")
        upload = state["uploads"].get(asset_id)
        now = int(time.time())
        if upload and upload.get("object_key"):
            if int(upload.get("url_expires_at") or 0) <= now + 3600:
                upload = publisher.sign(str(upload["object_key"]))
                state["uploads"][asset_id] = upload
                atomic_write_json(state_path, state)
            return str(upload["url"])
        local = validate_identity(identity, asset_id)
        upload = publisher.upload(local, plan["run_id"], asset_id)
        state["uploads"][asset_id] = upload
        atomic_write_json(state_path, state)
        return str(upload["url"])

    image_urls = [
        asset_url(str(asset["id"]), asset["identity"])
        for asset in plan.get("images") or []
    ]
    responses_dir = plan_path.parent / "responses"
    responses_dir.mkdir(exist_ok=True)

    active_tasks: list[tuple[dict[str, Any], str]] = []
    pending_creations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for segment in plan["segments"]:
        index = int(segment["index"])
        key = str(index)
        segment_state = state["segments"].setdefault(key, {})
        destination = Path(segment["output_file"])
        if destination.is_file() and destination.stat().st_size > 0:
            try:
                validate_generated_video(
                    destination,
                    int(segment["duration_seconds"]),
                    bool(plan["parameters"]["generate_audio"]),
                )
            except SeedanceError as exc:
                archived = archive_invalid_output(destination)
                segment_state.update(
                    {
                        "status": "download_validation_failed",
                        "error": str(exc),
                        "invalid_output": str(archived),
                    }
                )
                atomic_write_json(state_path, state)
            else:
                segment_state["status"] = "downloaded"
                segment_state.pop("error", None)
                atomic_write_json(state_path, state)
                continue
        previous_status = str(segment_state.get("status") or "")
        if previous_status in {"create_failed", "failed", "cancelled", "expired"}:
            if not args.retry_failed:
                raise SeedanceError(
                    f"第 {index} 段上次状态为 {previous_status}，不会自动重新计费。"
                )
            segment_state.clear()
        if previous_status == "create_ambiguous":
            if not args.allow_recreate_ambiguous:
                raise SeedanceError(f"第 {index} 段创建结果未知，拒绝自动重复提交。")
            segment_state.clear()

        task_id = str(segment_state.get("task_id") or "")
        if task_id:
            active_tasks.append((segment, task_id))
            continue
        depth_url = None
        if segment.get("depth_video"):
            depth_url = asset_url(f"video-{index:02d}", segment["depth_video"])
        request_body = build_request(plan, segment, image_urls, depth_url)
        atomic_write_json(
            responses_dir / f"request_part_{index:02d}.json", request_body
        )
        pending_creations.append((segment, request_body))

    # Create every missing task before polling any task, so generation overlaps.
    for segment, request_body in pending_creations:
        index = int(segment["index"])
        segment_state = state["segments"][str(index)]
        try:
            task_id = create_task(client, request_body)
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            known_rejection = (
                isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in {408, 409, 425, 429}
            )
            create_status = "create_failed" if known_rejection else "create_ambiguous"
            segment_state.update({"status": create_status, "error": str(exc)})
            atomic_write_json(state_path, state)
            if known_rejection:
                raise SeedanceError(
                    f"第 {index} 段创建请求被 Ark 明确拒绝，不会自动重试：{exc}"
                ) from exc
            raise SeedanceError(
                f"第 {index} 段创建请求结果未知，不会自动重试：{exc}"
            ) from exc
        segment_state.update(
            {
                "task_id": task_id,
                "status": "created",
                "created_at": int(time.time()),
            }
        )
        atomic_write_json(state_path, state)
        active_tasks.append((segment, task_id))

    state_lock = Lock()
    stop_event = Event()

    def process_segment(segment: dict[str, Any], task_id: str) -> Path:
        index = int(segment["index"])
        key = str(index)
        destination = Path(segment["output_file"])
        worker_client = client_factory(api_key)

        def on_update(body: dict[str, Any]) -> None:
            atomic_write_json(responses_dir / f"response_part_{index:02d}.json", body)
            with state_lock:
                segment_state = state["segments"][key]
                segment_state["status"] = str(body.get("status") or "")
                segment_state["last_response_at"] = int(time.time())
                if body.get("error"):
                    segment_state["error"] = body["error"]
                atomic_write_json(state_path, state)

        result = poll_task(
            worker_client,
            task_id,
            args.poll_interval,
            args.poll_timeout,
            on_update,
            stop_event,
        )
        status = str(result.get("status") or "")
        if status != "succeeded":
            raise SeedanceError(
                f"第 {index} 段 Seedance 任务未成功：{status}，"
                f"{result.get('error') or ''}"
            )
        url = result_video_url(result)
        if not url:
            raise SeedanceError(f"第 {index} 段成功响应中没有 video_url。")
        try:
            download_and_validate_video(
                url,
                destination,
                int(segment["duration_seconds"]),
                bool(plan["parameters"]["generate_audio"]),
            )
        except SeedanceError as exc:
            with state_lock:
                state["segments"][key].update(
                    {"status": "download_validation_failed", "error": str(exc)}
                )
                atomic_write_json(state_path, state)
            raise
        with state_lock:
            segment_state = state["segments"][key]
            segment_state.update(
                {
                    "status": "downloaded",
                    "output_file": str(destination),
                    "downloaded_at": int(time.time()),
                }
            )
            segment_state.pop("error", None)
            atomic_write_json(state_path, state)
        return destination

    worker_errors: list[tuple[int, str]] = []
    if active_tasks:
        with ThreadPoolExecutor(
            max_workers=len(active_tasks),
            thread_name_prefix="seedance-task",
        ) as executor:
            futures = {
                executor.submit(process_segment, segment, task_id): int(
                    segment["index"]
                )
                for segment, task_id in active_tasks
            }
            try:
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        worker_errors.append((index, str(exc)))
            except KeyboardInterrupt:
                stop_event.set()
                for future in futures:
                    future.cancel()
                raise
    if worker_errors:
        detail = "; ".join(
            f"第 {index} 段：{error}" for index, error in sorted(worker_errors)
        )
        raise SeedanceError(f"部分 Seedance 并行任务未完成：{detail}")

    part_files = [Path(segment["output_file"]) for segment in plan["segments"]]
    full_output = Path(
        plan.get("full_output_file")
        or plan_path.parent
        / "generated"
        / f"full.{plan['parameters']['output_format']}"
    )
    expected_full_duration = sum(
        int(segment["duration_seconds"]) for segment in plan["segments"]
    )
    full_tolerance = max(1.0, len(part_files) * 0.25)
    if full_output.is_file() and full_output.stat().st_size > 0:
        try:
            validate_generated_video(
                full_output,
                expected_full_duration,
                bool(plan["parameters"]["generate_audio"]),
                duration_tolerance=full_tolerance,
            )
        except SeedanceError as exc:
            archived = archive_invalid_output(full_output)
            state["full_output"] = {
                "status": "validation_failed",
                "error": str(exc),
                "invalid_output": str(archived),
            }
            atomic_write_json(state_path, state)
    if not full_output.exists():
        try:
            concat_generated_videos(
                part_files,
                full_output,
                expected_full_duration,
                bool(plan["parameters"]["generate_audio"]),
            )
        except SeedanceError as exc:
            state["full_output"] = {"status": "concat_failed", "error": str(exc)}
            atomic_write_json(state_path, state)
            raise
    state["full_output"] = {
        "status": "complete",
        "identity": file_identity(full_output),
    }
    atomic_write_json(state_path, state)

    completion = plan_path.with_name("completed.json")
    atomic_write_json(
        completion,
        {
            "schema_version": 1,
            "status": "complete",
            "run_id": plan["run_id"],
            "segments": len(plan["segments"]),
            "full_output_file": str(full_output),
        },
    )
    print(f"SEEDANCE complete file={completion}")
    return completion


def main() -> int:
    args = parse_args()
    try:
        if args.command == "prepare":
            prepare(args)
        else:
            if args.poll_interval < 0 or args.poll_timeout <= 0:
                raise SeedanceError("轮询间隔不能为负数，超时时间必须为正数。")
            if not 3600 <= args.signed_url_ttl <= 7 * 24 * 3600:
                raise SeedanceError("TOS 签名 URL 有效期必须在 1 小时到 7 天之间。")
            submit(args)
        return 0
    except SeedanceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
