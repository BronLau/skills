#!/usr/bin/env python3
"""Prepare and submit Seedance 2.0 or 2.5 tasks from verified prompt segments."""

from __future__ import annotations

import argparse
import hashlib
import io
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
from functools import partial
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable
from urllib.parse import quote
from urllib.parse import urlparse

from media_preflight import (
    MediaPreflightError,
    SEEDANCE_MAX_IMAGE_BYTES,
    validate_seedance_image_count,
    validate_seedance_image_input as validate_seedance_image_input_shared,
)
from qwen_video_prompt_reverse import SEGMENT_HEADER_PATTERN


MODEL_BY_SEGMENT_MAX_SECONDS = {
    15: "doubao-seedance-2-0-260128",
    30: "doubao-seedance-2-5-260628",
}
MODEL_DURATION_RANGE_BY_SEGMENT_MAX_SECONDS = {
    15: (4, 15),
    30: (4, 30),
}
DEFAULT_RESOLUTION = "720p"
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_OPENAPI_HOST = "open.volcengineapi.com"
ARK_ASSET_API_VERSION = "2024-01-01"
ARK_ASSET_REGION = "cn-beijing"
MIN_GENERATION_SECONDS = 4
MAX_GENERATION_SECONDS = 30
MAX_SEED = 2**31 - 1
MIN_REFERENCE_VIDEO_SECONDS = 2
MAX_REFERENCE_VIDEO_SECONDS = 30
MIN_REFERENCE_AUDIO_SECONDS = 2
MAX_REFERENCE_AUDIO_SECONDS = 15
MAX_REFERENCE_AUDIO_BYTES = 15 * 1024 * 1024
ASSET_QUERY_MAX_ATTEMPTS = 3
ASSET_QUERY_RETRY_BASE_SECONDS = 1.0
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
BASE_FACT_ASSEMBLY_MODE = "deterministic_from_max_verified_facts"
STATIC_OVERRIDE_ASSEMBLY_MODE = (
    "deterministic_from_max_verified_facts_with_user_static_overrides"
)
AUDIO_OVERRIDE_ASSEMBLY_MODE = (
    "deterministic_from_locked_visual_facts_with_verified_audio_overrides"
)


class SeedanceError(RuntimeError):
    pass


def configure_network_environment() -> None:
    removed: list[str] = []
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        try:
            port = parsed.port
        except ValueError:
            continue
        if parsed.hostname in {"127.0.0.1", "localhost"} and port == 7890:
            os.environ.pop(name, None)
            removed.append(name)
    if removed:
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        print(
            "SEEDANCE proxy_bypass variables=" + ",".join(sorted(removed)),
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按分段上限准备或提交 Doubao Seedance 2.0/2.5 视频任务。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--prompt", type=Path, required=True)
    prepare.add_argument("--segment-plan", type=Path, required=True)
    prepare.add_argument("--fact-lock", type=Path, required=True)
    prepare.add_argument("--source-video", type=Path, required=True)
    prepare.add_argument("--depth-dir", type=Path)
    prepare.add_argument("--character-image", type=Path)
    prepare.add_argument(
        "--character-image-type",
        choices=("virtual", "real"),
    )
    prepare.add_argument("--character-asset-id")
    prepare.add_argument("--confirm-virtual-portrait-rights", action="store_true")
    prepare.add_argument("--product-image", type=Path, action="append", default=[])
    prepare.add_argument("--transcript-file", type=Path)
    prepare.add_argument("--reference-audio", type=Path)
    prepare.add_argument("--confirm-voice-rights", action="store_true")
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
    submit.add_argument(
        "--ark-api-key-file",
        type=Path,
        help="Ark 配置文件：Seedance API Key，以及人物素材库所需 AK/SK。",
    )
    submit.add_argument(
        "--tos-config-file",
        type=Path,
        help="仅供 STS 与 TOS 上传使用的配置文件。",
    )
    submit.add_argument("--poll-interval", type=float, default=30.0)
    submit.add_argument("--poll-timeout", type=float, default=7200.0)
    submit.add_argument("--signed-url-ttl", type=int, default=7 * 24 * 3600)
    submit.add_argument(
        "--asset-project-name",
        default=os.environ.get("ARK_PROJECT_NAME", "default"),
    )
    submit.add_argument("--asset-poll-interval", type=float, default=10.0)
    submit.add_argument("--asset-poll-timeout", type=float, default=1800.0)
    submit.add_argument("--retry-failed", action="store_true")
    submit.add_argument("--allow-recreate-ambiguous", action="store_true")
    submit.add_argument("--retry-failed-character-asset", action="store_true")
    submit.add_argument(
        "--allow-recreate-ambiguous-character-asset",
        action="store_true",
    )
    return parser.parse_args()


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.R_OK):
        raise SeedanceError(f"{label}不存在或不可读：{resolved}")
    return resolved


def normalize_asset_id(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip()
    if normalized.startswith("asset://"):
        normalized = normalized[len("asset://") :]
    if not re.fullmatch(r"asset-[A-Za-z0-9-]+", normalized):
        raise SeedanceError(f"虚拟人像 Asset ID 格式无效：{value}")
    return normalized


def validate_character_reference_plan(
    plan: dict[str, Any],
) -> dict[str, Any] | None:
    images = list(plan.get("images") or [])
    schema_version = int(plan.get("schema_version") or 1)
    if images and schema_version < 2:
        raise SeedanceError(
            "旧版 Seedance 图片计划缺少参考角色信息；请重新运行 prepare。"
        )
    character_images = []
    for image in images:
        role = str(image.get("reference_role") or "")
        if role not in {"character", "product"}:
            raise SeedanceError(f"Seedance 图片参考角色无效：{role or '空'}")
        if role == "character":
            character_images.append(image)
    reference = plan.get("character_reference")
    if not character_images:
        if reference is not None:
            raise SeedanceError("计划包含人物引用配置，但没有人物图片。")
        return None
    if len(character_images) != 1 or not isinstance(reference, dict):
        raise SeedanceError("当前计划必须且只能包含一张已配置的人物图片。")
    if str(reference.get("image_id") or "") != str(character_images[0].get("id") or ""):
        raise SeedanceError("人物引用配置与人物图片 ID 不一致。")
    portrait_type = str(reference.get("portrait_type") or "")
    asset_id = normalize_asset_id(reference.get("asset_id"))
    if portrait_type == "virtual":
        if not bool(reference.get("virtual_rights_confirmed")):
            raise SeedanceError("虚拟人像计划缺少素材权利确认。")
    elif portrait_type == "real":
        if not asset_id:
            raise SeedanceError("真人肖像计划必须包含已授权的 Asset ID。")
    else:
        raise SeedanceError(f"人物类型无效：{portrait_type or '空'}")
    return reference


def plan_requires_storage(
    plan: dict[str, Any],
    character_reference: dict[str, Any] | None,
) -> bool:
    if plan.get("audio_reference"):
        return True
    if any(segment.get("depth_video") for segment in plan.get("segments") or []):
        return True
    for image in plan.get("images") or []:
        role = str(image.get("reference_role") or "")
        if role == "product":
            return True
        if role == "character" and (
            character_reference is None
            or not normalize_asset_id(character_reference.get("asset_id"))
        ):
            return True
    return False


def validate_reference_audio(path: Path) -> dict[str, Any]:
    resolved = require_file(path, "音色参考音频")
    if resolved.suffix.lower() not in {".mp3", ".wav"}:
        raise SeedanceError("音色参考音频只支持 MP3 或 WAV。")
    if resolved.stat().st_size >= MAX_REFERENCE_AUDIO_BYTES:
        raise SeedanceError("音色参考音频必须小于 15 MB。")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise SeedanceError("未找到 ffprobe，无法校验音色参考音频。")
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(resolved),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        metadata = json.loads(completed.stdout)
        duration = float(metadata["format"]["duration"])
        stream_types = [
            str(stream.get("codec_type") or "")
            for stream in metadata.get("streams") or []
        ]
    except (
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise SeedanceError(f"ffprobe 无法读取音色参考音频：{resolved}") from exc
    if "audio" not in stream_types or "video" in stream_types:
        raise SeedanceError("音色参考必须是仅含音频流的 MP3 或 WAV。")
    if not MIN_REFERENCE_AUDIO_SECONDS <= duration <= MAX_REFERENCE_AUDIO_SECONDS + 0.02:
        raise SeedanceError(
            "音色参考音频时长必须在 2 到 15 秒之间："
            f"{duration:.3f} 秒"
        )
    return {"duration": duration, "format": resolved.suffix.lower().lstrip(".")}


def character_image_asset(plan: dict[str, Any]) -> dict[str, Any]:
    matches = [
        image
        for image in plan.get("images") or []
        if image.get("reference_role") == "character"
    ]
    if len(matches) != 1:
        raise SeedanceError("计划缺少唯一的人物图片。")
    return matches[0]


def character_image_digest(plan: dict[str, Any]) -> str:
    identity = character_image_asset(plan).get("identity")
    if not isinstance(identity, dict):
        raise SeedanceError("人物图片缺少文件身份信息；请重新运行 prepare。")
    digest = str(identity.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SeedanceError("人物图片缺少有效 SHA-256；请重新运行 prepare。")
    return digest


def validate_character_asset_identity(
    plan: dict[str, Any],
    asset_result: dict[str, Any],
) -> None:
    remote_url = str(asset_result.get("URL") or "")
    if not remote_url:
        raise SeedanceError("GetAsset 响应缺少人物素材 URL，无法校验一致性。")
    local_path = validate_identity(
        character_image_asset(plan)["identity"],
        "人物形象图",
    )
    request = urllib.request.Request(
        remote_url,
        headers={"User-Agent": "Codex/Seedance-Asset-Check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            remote_bytes = response.read(SEEDANCE_MAX_IMAGE_BYTES + 1)
    except Exception as exc:
        raise SeedanceError(f"人物 Asset 图片下载失败：{exc}") from exc
    if not remote_bytes or len(remote_bytes) >= SEEDANCE_MAX_IMAGE_BYTES:
        raise SeedanceError("人物 Asset 图片为空或达到 30 MB 上限。")
    local_bytes = local_path.read_bytes()
    if hashlib.sha256(local_bytes).digest() == hashlib.sha256(remote_bytes).digest():
        return
    try:
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(local_path) as local_image:
            local_rgb = ImageOps.exif_transpose(local_image).convert("RGB")
            local_ratio = local_rgb.width / local_rgb.height
            local_array = np.asarray(
                local_rgb.resize((256, 256)), dtype=np.float32
            ) / 255.0
        with Image.open(io.BytesIO(remote_bytes)) as remote_image:
            remote_rgb = ImageOps.exif_transpose(remote_image).convert("RGB")
            remote_ratio = remote_rgb.width / remote_rgb.height
            remote_array = np.asarray(
                remote_rgb.resize((256, 256)), dtype=np.float32
            ) / 255.0
    except Exception as exc:
        raise SeedanceError(f"人物 Asset 图片无法解码：{exc}") from exc
    if abs(local_ratio - remote_ratio) > max(0.02, local_ratio * 0.02):
        raise SeedanceError("人物 Asset 与本地人物图宽高比不一致。")
    mean_error = float(np.mean(np.abs(local_array - remote_array)))
    local_gray = local_array.mean(axis=2).reshape(-1)
    remote_gray = remote_array.mean(axis=2).reshape(-1)
    if float(local_gray.std()) < 1e-6 or float(remote_gray.std()) < 1e-6:
        correlation = 1.0 if mean_error <= 0.03 else 0.0
    else:
        correlation = float(np.corrcoef(local_gray, remote_gray)[0, 1])
    if not np.isfinite(correlation) or mean_error > 0.12 or correlation < 0.85:
        raise SeedanceError(
            "人物 Asset 与本地人物图视觉不一致："
            f"mean_error={mean_error:.3f}, correlation={correlation:.3f}"
        )


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


def prompt_text_sha256(path: Path) -> str:
    return hashlib.sha256(
        path.read_text(encoding="utf-8").strip().encode("utf-8")
    ).hexdigest()


def validate_static_visual_overrides(body: dict[str, Any]) -> dict[str, Any]:
    if set(body) != {"schema_version", "subject_definitions", "shot_overrides"}:
        raise SeedanceError("静态视觉覆盖文件字段不完整或越权。")
    if body.get("schema_version") != 1:
        raise SeedanceError("静态视觉覆盖文件 schema_version 必须为 1。")
    definitions = body.get("subject_definitions")
    if not isinstance(definitions, dict):
        raise SeedanceError("静态视觉覆盖 subject_definitions 必须是对象。")
    normalized_definitions: dict[str, str] = {}
    for label, value in definitions.items():
        normalized_label = str(label).strip()
        definition = str(value).strip()
        if not re.fullmatch(r"<[^<>]+>", normalized_label):
            raise SeedanceError(f"静态视觉覆盖主体标签无效：{normalized_label}")
        if (
            not definition
            or "\n" in definition
            or "{" in definition
            or "}" in definition
            or "镜头" in definition
            or not definition.endswith(f"定义为{normalized_label}。")
        ):
            raise SeedanceError(f"静态视觉覆盖主体定义无效：{normalized_label}")
        normalized_definitions[normalized_label] = definition

    shot_overrides = body.get("shot_overrides")
    if not isinstance(shot_overrides, list):
        raise SeedanceError("静态视觉覆盖 shot_overrides 必须是数组。")
    normalized_shots: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    allowed = {"segment_index", "shot_index", "composition", "scene_light"}
    for item in shot_overrides:
        if not isinstance(item, dict) or not set(item).issubset(allowed):
            raise SeedanceError("静态视觉覆盖镜头字段越权。")
        if not {"segment_index", "shot_index"}.issubset(item):
            raise SeedanceError("静态视觉覆盖镜头缺少索引。")
        key = (int(item["segment_index"]), int(item["shot_index"]))
        if key[0] <= 0 or key[1] <= 0 or key in seen:
            raise SeedanceError(f"静态视觉覆盖镜头索引无效或重复：{key}")
        normalized: dict[str, Any] = {
            "segment_index": key[0],
            "shot_index": key[1],
        }
        for field in ("composition", "scene_light"):
            if field not in item:
                continue
            value = str(item[field]).strip()
            if (
                not value
                or "\n" in value
                or "{" in value
                or "}" in value
                or "镜头" in value
                or "@" in value
            ):
                raise SeedanceError(f"静态视觉覆盖 {field} 无效：{key}")
            normalized[field] = value
        if len(normalized) == 2:
            raise SeedanceError(f"静态视觉覆盖镜头没有可修改字段：{key}")
        seen.add(key)
        normalized_shots.append(normalized)
    if not normalized_definitions and not normalized_shots:
        raise SeedanceError("静态视觉覆盖不能为空。")
    return {
        "schema_version": 1,
        "subject_definitions": normalized_definitions,
        "shot_overrides": normalized_shots,
    }


def validate_fact_lock_file(
    lock_path: Path,
    prompt_path: Path,
    segment_plan_path: Path,
) -> dict[str, Any]:
    body = load_json(lock_path, "Max 核验事实锁定记录")
    assembly_mode = body.get("assembly_mode")
    if body.get("status") != "locked" or assembly_mode not in {
        BASE_FACT_ASSEMBLY_MODE,
        STATIC_OVERRIDE_ASSEMBLY_MODE,
        AUDIO_OVERRIDE_ASSEMBLY_MODE,
    }:
        raise SeedanceError("Max 核验事实尚未锁定，拒绝准备或提交 Seedance。")
    if body.get("prompt_sha256") != prompt_text_sha256(prompt_path):
        raise SeedanceError("正式提示词已变化，事实锁定记录失效。")
    if body.get("segment_plan_sha256") != file_sha256(segment_plan_path):
        raise SeedanceError("分段计划已变化，事实锁定记录失效。")
    for key, label in (
        ("analysis_video", "分析视频"),
        ("omni_facts", "Omni 初步事实"),
        ("max_verification", "Max 核验事实"),
    ):
        identity = body.get(key)
        if not isinstance(identity, dict):
            raise SeedanceError(f"事实锁定记录缺少 {key}。")
        validate_identity(identity, label)
    if assembly_mode == STATIC_OVERRIDE_ASSEMBLY_MODE:
        identity = body.get("static_visual_overrides")
        if not isinstance(identity, dict):
            raise SeedanceError("静态视觉覆盖事实锁缺少覆盖文件。")
        override_path = validate_identity(identity, "静态视觉覆盖文件")
        validate_static_visual_overrides(
            load_json(override_path, "静态视觉覆盖文件")
        )
    if assembly_mode == AUDIO_OVERRIDE_ASSEMBLY_MODE:
        for key, label in (
            ("base_fact_lock", "基础视觉事实锁"),
            ("audio_fact_lock", "音频核验事实锁"),
            ("audio_verification", "Max 音频核验结果"),
        ):
            identity = body.get(key)
            if not isinstance(identity, dict):
                raise SeedanceError(f"音频覆盖事实锁缺少 {key}。")
            validate_identity(identity, label)
    return body


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
    if maximum not in MODEL_BY_SEGMENT_MAX_SECONDS:
        raise SeedanceError("segment_plan.json 不符合 Seedance 时长约束。")
    minimum, model_maximum = MODEL_DURATION_RANGE_BY_SEGMENT_MAX_SECONDS[maximum]
    if prompt_duration < minimum:
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
        if not minimum <= duration <= model_maximum:
            raise SeedanceError(
                f"第 {index} 段时长不在 {minimum} 到 {model_maximum} 秒之间。"
            )
        if abs(duration - round(duration)) > 0.02:
            raise SeedanceError(f"第 {index} 段时长不是整数秒：{duration}")
        total += int(round(duration))
    if total != prompt_duration:
        raise SeedanceError(f"分段时长总和不匹配：{total} != {prompt_duration}")
    return segments


def model_for_segment_max_seconds(maximum: int) -> str:
    try:
        return MODEL_BY_SEGMENT_MAX_SECONDS[maximum]
    except KeyError as exc:
        raise SeedanceError(
            f"不支持的最大分段时长：{maximum}；只能为 15 或 30 秒。"
        ) from exc


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


def compile_prompt(
    body: str,
    image_count: int,
    with_depth: bool,
    with_reference_audio: bool = False,
) -> str:
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
    prefixes: list[str] = []
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
        prefixes.append(prefix)
    if with_reference_audio:
        prefixes.append(
            "@音频1只作为全片人物口播的统一音色参考，仅参考人声音色、"
            "发声质感、语速和韵律；不复用音频1中的原台词、背景音乐或环境声。"
            "所有口播台词严格以各镜头大括号中的文字为准，并始终使用同一音色。"
        )
    return "\n".join([*prefixes, compiled.strip()])


def prepare(args: argparse.Namespace) -> Path:
    prompt_path = require_file(args.prompt, "最终提示词")
    plan_path = require_file(args.segment_plan, "分段计划")
    lock_path = require_file(args.fact_lock, "Max 核验事实锁定记录")
    validate_fact_lock_file(lock_path, prompt_path, plan_path)
    source_video = require_file(args.source_video, "原始参考视频")
    character_image = (
        require_file(args.character_image, "人物形象图")
        if args.character_image
        else None
    )
    character_image_type = str(args.character_image_type or "")
    character_asset_id = normalize_asset_id(args.character_asset_id)
    if character_image:
        if not character_image_type:
            raise SeedanceError(
                "提供人物形象图时必须指定 --character-image-type virtual 或 real。"
            )
        if character_image_type == "virtual" and not args.confirm_virtual_portrait_rights:
            raise SeedanceError(
                "创建私域虚拟人像前必须明确确认素材权利与虚拟人像属性。"
            )
        if character_image_type == "real" and not character_asset_id:
            raise SeedanceError(
                "真人肖像不能上传至私域虚拟人像库；请提供已授权的 --character-asset-id。"
            )
    elif character_image_type or character_asset_id or args.confirm_virtual_portrait_rights:
        raise SeedanceError("人物人像参数必须与 --character-image 一起使用。")
    product_images = [
        require_file(path, f"第 {index} 张产品图")
        for index, path in enumerate(args.product_image, start=1)
    ]
    transcript_file = (
        require_file(args.transcript_file, "音轨转写文件")
        if args.transcript_file
        else None
    )
    reference_audio = (
        require_file(getattr(args, "reference_audio"), "音色参考音频")
        if getattr(args, "reference_audio", None)
        else None
    )
    if reference_audio:
        if not bool(getattr(args, "confirm_voice_rights", False)):
            raise SeedanceError("使用音色参考前必须明确确认声音权利与授权。")
        audio_metadata = validate_reference_audio(reference_audio)
    else:
        if bool(getattr(args, "confirm_voice_rights", False)):
            raise SeedanceError("--confirm-voice-rights 必须与 --reference-audio 一起使用。")
        audio_metadata = None
    images = ([character_image] if character_image else []) + product_images

    segment_plan = load_json(plan_path, "分段计划")
    segments = validate_segment_plan(segment_plan)
    segment_max_seconds = int(segment_plan["segment_max_seconds"])
    model_id = model_for_segment_max_seconds(segment_max_seconds)
    try:
        validate_seedance_image_count(segment_max_seconds, len(images))
    except MediaPreflightError as exc:
        raise SeedanceError(str(exc)) from exc
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
    if reference_audio and not generate_audio:
        raise SeedanceError("使用音色参考时必须启用 generate_audio。")
    seed = args.seed if args.seed is not None else secrets.randbelow(MAX_SEED + 1)
    if not -1 <= seed <= MAX_SEED:
        raise SeedanceError(f"--seed 必须在 -1 到 {MAX_SEED} 之间。")

    image_assets: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        image_assets.append(
            {
                "id": f"image-{index:02d}",
                "index": index,
                "kind": "image",
                "reference_role": (
                    "character" if character_image and index == 1 else "product"
                ),
                "identity": file_identity(image),
            }
        )
    prepared_segments: list[dict[str, Any]] = []
    for segment, prompt_body in zip(segments, prompt_bodies):
        index = int(segment["index"])
        depth_identity = None
        if depth_files:
            normalized = normalize_depth_video(
                depth_files[index - 1], assets_dir / f"depth_part_{index:02d}.mp4"
            )
            depth_identity = file_identity(normalized)
        compiled = compile_prompt(
            prompt_body,
            len(images),
            bool(depth_files),
            bool(reference_audio),
        )
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

    plan_body = {
        "schema_version": 4,
        "status": "prepared",
        "run_id": uuid.uuid4().hex,
        "model": model_id,
        "segment_max_seconds": segment_max_seconds,
        "mode": "depth-reference" if depth_files else "text-and-image-reference",
        "source_video": file_identity(source_video),
        "prompt": file_identity(prompt_path),
        "fact_lock": file_identity(lock_path),
        "segment_plan": file_identity(plan_path),
        "images": image_assets,
        "audio_reference": (
            {
                "id": "audio-01",
                "index": 1,
                "kind": "audio",
                "reference_role": "voice_timbre",
                "identity": file_identity(reference_audio),
                "duration_seconds": float(audio_metadata["duration"]),
                "rights_confirmed": bool(
                    getattr(args, "confirm_voice_rights", False)
                ),
            }
            if reference_audio and audio_metadata
            else None
        ),
        "character_reference": (
            {
                "image_id": "image-01",
                "portrait_type": character_image_type,
                "asset_id": character_asset_id or None,
                "virtual_rights_confirmed": bool(
                    args.confirm_virtual_portrait_rights
                ),
            }
            if character_image
            else None
        ),
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
    atomic_write_json(plan_output, plan_body)
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


def load_ark_config(
    path: Path | None,
    require_asset_credentials: bool = False,
) -> dict[str, str]:
    config = {"api_key": resolve_ark_api_key(path)}
    if path is not None:
        resolved = path.expanduser().resolve()
        file_config = read_tos_config_file(resolved, "Ark 配置文件")
        for key in ("access_key", "secret_key", "security_token", "region"):
            if file_config.get(key):
                config[key] = file_config[key]
    environment_mapping = {
        "access_key": "ARK_ACCESS_KEY",
        "secret_key": "ARK_SECRET_KEY",
        "security_token": "ARK_SECURITY_TOKEN",
        "region": "ARK_REGION",
    }
    for key, environment_name in environment_mapping.items():
        value = os.environ.get(environment_name, "").strip()
        if value:
            config[key] = value
    config.setdefault("region", ARK_ASSET_REGION)
    if require_asset_credentials:
        missing = [
            key for key in ("access_key", "secret_key") if not config.get(key)
        ]
        if missing:
            raise SeedanceError(
                "Ark 配置缺少人物素材库凭证字段：" + ", ".join(missing)
            )
    return config


def markdown_tos_value(content: str, name: str) -> str:
    label = (
        rf"(?<![A-Za-z0-9_])(?:\*\*)?{re.escape(name)}(?:\*\*)?"
        r"\s*[:=：]\s*"
    )
    if name == "endpoint":
        match = re.search(label + r"\[([^\]]+)\]", content)
    else:
        match = re.search(label + r"[`\"']?([^\s`\"']+)", content)
    return match.group(1).strip().strip("`\"'*") if match else ""


def read_tos_config_file(path: Path, label: str) -> dict[str, str]:
    content = require_file(path, label).read_text(encoding="utf-8")
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
        raise SeedanceError(f"{label}根节点必须是对象。")
    return normalize_tos_config(parsed)


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


def load_tos_config(
    path: Path | None,
    require_storage: bool = True,
) -> dict[str, str]:
    config: dict[str, str] = {}
    if path:
        resolved = path.expanduser().resolve()
        if resolved.is_dir():
            legacy_file = resolved / "Volc engine_API_KEY.md"
            if legacy_file.is_file():
                resolved = legacy_file
            else:
                resolved = resolved / "TOS_Config.md"
        config.update(read_tos_config_file(resolved, "火山 TOS 配置文件"))
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
    required = (
        ("access_key", "secret_key", "endpoint", "region", "bucket")
        if require_storage
        else ("access_key", "secret_key", "region")
    )
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise SeedanceError("火山 TOS 配置缺少字段：" + ", ".join(missing))
    if require_storage and not config.get("prefix"):
        main_path = config.get("main_path", "").strip("/")
        config["prefix"] = (
            f"{main_path}/video-white-model-prompt"
            if main_path
            else "video-white-model-prompt"
        )
    if require_storage and config.get("public_domain"):
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
        self._client: Any | None = None

    def client(self) -> Any:
        if self._client is not None:
            return self._client
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
            try:
                assumed = sts.assume_role(
                    {
                        "DurationSeconds": "900",
                        "RoleSessionName": "video-white-model-prompt",
                        "RoleTrn": role_trn,
                    }
                )
            except Exception as exc:
                raise SeedanceError(f"STS AssumeRole 失败：{exc}") from exc
            try:
                credentials = assumed["Result"]["Credentials"]
                access_key = credentials["AccessKeyId"]
                secret_key = credentials["SecretAccessKey"]
                security_token = credentials["SessionToken"]
            except (KeyError, TypeError) as exc:
                raise SeedanceError("STS AssumeRole 响应中缺少临时凭证。") from exc
        try:
            self._client = self.tos.TosClientV2(
                ak=access_key,
                sk=secret_key,
                endpoint=self.config["endpoint"],
                region=self.config["region"],
                security_token=security_token,
            )
        except Exception as exc:
            raise SeedanceError(f"火山 TOS 客户端初始化失败：{exc}") from exc
        return self._client

    def upload(self, path: Path, run_id: str, asset_id: str) -> dict[str, Any]:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)
        object_key = (
            f"{self.prefix}/{run_id}/{asset_id}_{file_sha256(path)[:16]}_{safe_name}"
        )
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            self.client().put_object_from_file(
                self.bucket,
                object_key,
                str(path),
                content_type=content_type,
            )
        except Exception as exc:
            raise SeedanceError(f"火山 TOS 素材上传失败：{path.name}：{exc}") from exc
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
        try:
            result = self.client().pre_signed_url(
                self.tos.HttpMethodType.Http_Method_Get,
                self.bucket,
                object_key,
                expires=self.signed_url_ttl,
            )
        except Exception as exc:
            raise SeedanceError(f"火山 TOS 签名 URL 生成失败：{exc}") from exc
        return {
            "object_key": object_key,
            "url": result.signed_url,
            "url_expires_at": int(time.time()) + self.signed_url_ttl,
        }


class ArkAssetLibrary:
    def __init__(self, config: dict[str, str], project_name: str) -> None:
        try:
            from volcengine.ApiInfo import ApiInfo
            from volcengine.Credentials import Credentials
            from volcengine.ServiceInfo import ServiceInfo
            from volcengine.base.Service import Service
        except ImportError as exc:
            raise SeedanceError(
                "私域虚拟人像资产需要 volcengine SDK。"
            ) from exc
        self.project_name = project_name.strip() or "default"
        credentials = Credentials(
            config["access_key"],
            config["secret_key"],
            "ark",
            config["region"],
            config.get("security_token", ""),
        )
        service_info = ServiceInfo(
            ARK_OPENAPI_HOST,
            {},
            credentials,
            10,
            30,
            scheme="https",
        )
        actions = (
            "CreateAssetGroup",
            "CreateAsset",
            "GetAsset",
            "ListAssets",
        )
        api_info = {
            action: ApiInfo(
                "POST",
                "/",
                {"Action": action, "Version": ARK_ASSET_API_VERSION},
                {},
                {},
            )
            for action in actions
        }
        self.service = Service(service_info, api_info)
        self.service.set_ak(config["access_key"])
        self.service.set_sk(config["secret_key"])
        if config.get("security_token"):
            self.service.set_session_token(config["security_token"])

    def call(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self.service.json(
                action,
                {},
                json.dumps(body, ensure_ascii=False),
            )
            response = json.loads(raw)
        except Exception as exc:
            raise SeedanceError(f"私域人像资产 API {action} 调用失败：{exc}") from exc
        if not isinstance(response, dict):
            raise SeedanceError(f"私域人像资产 API {action} 响应无效。")
        result = response.get("Result", response)
        if not isinstance(result, dict):
            raise SeedanceError(f"私域人像资产 API {action} 缺少结果对象。")
        return result

    def create_group(self, name: str, description: str) -> str:
        result = self.call(
            "CreateAssetGroup",
            {
                "Name": name,
                "Description": description,
                "GroupType": "AIGC",
                "ProjectName": self.project_name,
            },
        )
        group_id = str(result.get("Id") or "")
        if not group_id:
            raise SeedanceError("CreateAssetGroup 响应缺少 Group ID。")
        return group_id

    def find_asset(self, name: str) -> dict[str, Any] | None:
        result = self.call(
            "ListAssets",
            {
                "Filter": {
                    "GroupType": "AIGC",
                    "Statuses": ["Active", "Processing"],
                    "Name": name,
                },
                "PageNumber": 1,
                "PageSize": 100,
                "SortBy": "CreateTime",
                "SortOrder": "Desc",
            },
        )
        items = result.get("Items") or []
        if not isinstance(items, list):
            raise SeedanceError("ListAssets 响应中的 Items 无效。")
        exact = [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("Name") or "") == name
            and str(item.get("ProjectName") or self.project_name)
            == self.project_name
        ]
        exact.sort(
            key=lambda item: (
                str(item.get("Status") or "") == "Active",
                str(item.get("CreateTime") or ""),
            ),
            reverse=True,
        )
        return exact[0] if exact else None

    def create_asset(self, group_id: str, url: str, name: str) -> str:
        result = self.call(
            "CreateAsset",
            {
                "GroupId": group_id,
                "URL": url,
                "AssetType": "Image",
                "Name": name,
                "ProjectName": self.project_name,
            },
        )
        asset_id = str(result.get("Id") or "")
        if not asset_id:
            raise SeedanceError("CreateAsset 响应缺少 Asset ID。")
        return asset_id

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self.call(
            "GetAsset",
            {"Id": asset_id, "ProjectName": self.project_name},
        )


def get_character_asset_with_retry(
    library: Any,
    asset_id: str,
    retry_base_delay: float,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, ASSET_QUERY_MAX_ATTEMPTS + 1):
        try:
            result = library.get_asset(asset_id)
            if not isinstance(result, dict):
                raise SeedanceError("GetAsset 响应无效。")
            return result
        except Exception as exc:
            last_error = exc
            if attempt >= ASSET_QUERY_MAX_ATTEMPTS:
                break
            delay = retry_base_delay * (2 ** (attempt - 1))
            print(
                "SEEDANCE character_asset_query_retry "
                f"asset={asset_id} attempt={attempt + 1}/"
                f"{ASSET_QUERY_MAX_ATTEMPTS} error={exc}",
                flush=True,
            )
            if delay > 0:
                time.sleep(delay)
    raise SeedanceError(
        f"GetAsset 连续查询 {ASSET_QUERY_MAX_ATTEMPTS} 次失败：{last_error}"
    ) from last_error


def ensure_character_asset_uri(
    plan: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    library: Any,
    character_url: str | Callable[[], str] | None,
    project_name: str,
    poll_interval: float,
    poll_timeout: float,
    retry_failed: bool,
    allow_recreate_ambiguous: bool,
) -> str | None:
    reference = plan.get("character_reference")
    if not isinstance(reference, dict):
        return None
    portrait_type = str(reference.get("portrait_type") or "")
    planned_asset_id = normalize_asset_id(reference.get("asset_id"))
    character_state = state.setdefault("character_asset", {})
    recorded_project = str(character_state.get("project_name") or "")
    if recorded_project and recorded_project != project_name:
        raise SeedanceError(
            "恢复时私域人像 ProjectName 与原状态不一致："
            f"{recorded_project} != {project_name}"
        )
    character_state["project_name"] = project_name
    character_state["portrait_type"] = portrait_type

    if planned_asset_id:
        recorded_asset_id = normalize_asset_id(character_state.get("asset_id"))
        if recorded_asset_id and recorded_asset_id != planned_asset_id:
            raise SeedanceError("恢复时人物 Asset ID 与计划不一致。")
        character_state.update(
            {"asset_id": planned_asset_id, "source": "provided"}
        )
        atomic_write_json(state_path, state)
    elif portrait_type == "real":
        raise SeedanceError("真人肖像必须提供已授权的 Asset ID。")
    else:
        if str(character_state.get("status") or "") == "Failed":
            if not retry_failed:
                raise SeedanceError(
                    "私域人像素材上次处理失败，不会自动创建新素材。"
                )
            character_state.pop("asset_id", None)
            character_state.pop("status", None)
            character_state.pop("asset_create_status", None)
            character_state.pop("source", None)
            atomic_write_json(state_path, state)

        asset_name = f"vwm-{character_image_digest(plan)[:32]}"
        asset_id = normalize_asset_id(character_state.get("asset_id"))
        if not asset_id:
            finder = getattr(library, "find_asset", None)
            discovered = finder(asset_name) if callable(finder) else None
            if isinstance(discovered, dict):
                discovered_asset_id = normalize_asset_id(discovered.get("Id"))
                if discovered_asset_id:
                    character_state.update(
                        {
                            "asset_id": discovered_asset_id,
                            "group_id": str(discovered.get("GroupId") or ""),
                            "asset_create_status": "discovered",
                            "source": "discovered",
                        }
                    )
                    character_state.pop("error", None)
                    atomic_write_json(state_path, state)
                    asset_id = discovered_asset_id

        if not asset_id:
            asset_status = str(character_state.get("asset_create_status") or "")
            if asset_status == "create_ambiguous":
                if not allow_recreate_ambiguous:
                    raise SeedanceError(
                        "私域人像素材创建结果未知，拒绝自动重复创建。"
                    )
                character_state.pop("asset_create_status", None)

            group_status = str(character_state.get("group_create_status") or "")
            if group_status == "create_ambiguous":
                if not allow_recreate_ambiguous:
                    raise SeedanceError(
                        "私域人像素材组创建结果未知，拒绝自动重复创建。"
                    )
                character_state.pop("group_id", None)
                character_state.pop("group_create_status", None)
            group_id = str(character_state.get("group_id") or "")
            if not group_id:
                try:
                    group_id = library.create_group(
                        f"vwm-{plan['run_id'][:20]}",
                        "video-white-model-prompt virtual portrait",
                    )
                except Exception as exc:
                    character_state.update(
                        {
                            "group_create_status": "create_ambiguous",
                            "error": str(exc),
                        }
                    )
                    atomic_write_json(state_path, state)
                    raise SeedanceError(
                        "私域人像素材组创建结果未知，不会自动重复创建。"
                    ) from exc
                character_state.update(
                    {"group_id": group_id, "group_create_status": "created"}
                )
                character_state.pop("error", None)
                atomic_write_json(state_path, state)

            if not character_url:
                raise SeedanceError("创建私域虚拟人像缺少可访问的图片 URL。")
            resolved_character_url = (
                character_url() if callable(character_url) else character_url
            )
            if not resolved_character_url:
                raise SeedanceError("创建私域虚拟人像缺少可访问的图片 URL。")
            try:
                asset_id = library.create_asset(
                    group_id,
                    resolved_character_url,
                    asset_name,
                )
            except Exception as exc:
                character_state.update(
                    {"asset_create_status": "create_ambiguous", "error": str(exc)}
                )
                atomic_write_json(state_path, state)
                raise SeedanceError(
                    "私域人像素材创建结果未知，不会自动重复创建。"
                ) from exc
            character_state.update(
                {
                    "asset_id": asset_id,
                    "asset_create_status": "created",
                    "source": "created",
                }
            )
            character_state.pop("error", None)
            atomic_write_json(state_path, state)

    asset_id = normalize_asset_id(character_state.get("asset_id"))
    deadline = time.monotonic() + poll_timeout
    query_retry_delay = min(max(poll_interval, 0.0), ASSET_QUERY_RETRY_BASE_SECONDS)
    while True:
        try:
            result = get_character_asset_with_retry(
                library,
                asset_id,
                query_retry_delay,
            )
        except SeedanceError as exc:
            character_state["error"] = str(exc)
            atomic_write_json(state_path, state)
            raise
        status = str(result.get("Status") or "")
        if not status:
            raise SeedanceError("GetAsset 响应缺少素材状态。")
        response_asset_id = normalize_asset_id(result.get("Id"))
        if response_asset_id and response_asset_id != asset_id:
            raise SeedanceError("GetAsset 返回的 Asset ID 与请求不一致。")
        response_project = str(result.get("ProjectName") or "")
        if response_project and response_project != project_name:
            raise SeedanceError(
                "人物素材所属 ProjectName 与提交配置不一致："
                f"{response_project} != {project_name}"
            )
        character_state["status"] = status
        character_state["last_checked_at"] = int(time.time())
        character_state.pop("error", None)
        atomic_write_json(state_path, state)
        print(f"SEEDANCE character_asset={asset_id} status={status}", flush=True)
        if status == "Active":
            if str(character_state.get("source") or "") in {
                "provided",
                "discovered",
            }:
                validate_character_asset_identity(plan, result)
            return f"asset://{asset_id}"
        if status == "Failed":
            raise SeedanceError("私域人像素材处理失败，无法用于视频生成。")
        if time.monotonic() >= deadline:
            raise SeedanceError("私域人像素材处理超时，可稍后恢复查询。")
        time.sleep(poll_interval)


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
    audio_url: str | None = None,
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
    if audio_url:
        content.append(
            {
                "type": "audio_url",
                "audio_url": {"url": audio_url},
                "role": "reference_audio",
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
    if image_urls or depth_url or audio_url:
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
    asset_library_factory: Callable[[dict[str, str], str], Any] | None = None,
) -> Path:
    plan_path = require_file(args.plan, "Seedance 计划")
    plan = load_json(plan_path, "Seedance 计划")
    if plan.get("status") != "prepared":
        raise SeedanceError("Seedance 计划状态无效。")
    character_reference = validate_character_reference_plan(plan)
    validate_identity(plan["source_video"], "原始参考视频")
    prompt_path = validate_identity(plan["prompt"], "最终提示词")
    segment_plan_path = validate_identity(plan["segment_plan"], "分段计划")
    state_path = plan_path.with_name("tasks.json")
    preexisting_state: dict[str, Any] | None = None
    legacy_query_only = False
    if isinstance(plan.get("fact_lock"), dict):
        lock_path = validate_identity(plan["fact_lock"], "Max 核验事实锁定记录")
        validate_fact_lock_file(lock_path, prompt_path, segment_plan_path)
    else:
        if not state_path.is_file():
            raise SeedanceError("Seedance 计划缺少 Max 核验事实锁定记录。")
        preexisting_state = load_json(state_path, "Seedance 任务状态")
        if preexisting_state.get("run_id") != plan.get("run_id"):
            raise SeedanceError("Seedance 任务状态不属于当前计划。")
        missing_task_ids = [
            int(segment["index"])
            for segment in plan.get("segments") or []
            if not str(
                (preexisting_state.get("segments") or {})
                .get(str(segment["index"]), {})
                .get("task_id")
                or ""
            )
        ]
        if missing_task_ids:
            raise SeedanceError(
                "旧计划没有 Max 核验事实锁，只允许查询已有任务；"
                f"以下分段缺少 task_id：{missing_task_ids}"
            )
        legacy_query_only = True
        print("SEEDANCE legacy_plan_query_only", flush=True)
    segment_plan = load_json(segment_plan_path, "分段计划")
    validate_segment_plan(segment_plan)
    segment_max_seconds = int(segment_plan["segment_max_seconds"])
    expected_model = model_for_segment_max_seconds(segment_max_seconds)
    if plan.get("model") != expected_model:
        raise SeedanceError(
            "Seedance 计划模型与最大分段时长不匹配："
            f"{segment_max_seconds}s 必须使用 {expected_model}。"
        )
    recorded_maximum = plan.get("segment_max_seconds")
    if recorded_maximum is not None and int(recorded_maximum) != segment_max_seconds:
        raise SeedanceError("Seedance 计划记录的最大分段时长与分段计划不一致。")
    for asset in plan.get("images") or []:
        validate_identity(asset["identity"], f"{asset['id']} 图片")
    audio_reference = plan.get("audio_reference")
    if audio_reference is not None:
        if not isinstance(audio_reference, dict):
            raise SeedanceError("Seedance 音色参考计划无效。")
        if (
            audio_reference.get("id") != "audio-01"
            or audio_reference.get("reference_role") != "voice_timbre"
            or not bool(audio_reference.get("rights_confirmed"))
        ):
            raise SeedanceError("Seedance 音色参考计划缺少权利确认或角色配置。")
        audio_path = validate_identity(
            audio_reference["identity"], "音色参考音频"
        )
        validate_reference_audio(audio_path)
    for segment in plan["segments"]:
        validate_identity(segment["prompt"], f"第 {segment['index']} 段提示词")
        if segment.get("depth_video"):
            validate_identity(segment["depth_video"], f"第 {segment['index']} 段白模")

    ark_config = load_ark_config(
        args.ark_api_key_file,
        require_asset_credentials=(
            isinstance(character_reference, dict) and not legacy_query_only
        ),
    )
    api_key = ark_config["api_key"]
    requires_storage = (
        False if legacy_query_only else plan_requires_storage(plan, character_reference)
    )
    tos_config = (
        load_tos_config(args.tos_config_file, require_storage=requires_storage)
        if requires_storage
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
    if tos_config and requires_storage:
        publisher_builder = publisher_factory or TosPublisher
        publisher = publisher_builder(tos_config, args.signed_url_ttl)

    if preexisting_state is not None:
        state = preexisting_state
    elif state_path.exists():
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

    asset_library = None
    project_name = (
        str(getattr(args, "asset_project_name", "default") or "default").strip()
        or "default"
    )
    if isinstance(character_reference, dict) and not legacy_query_only:
        asset_library_builder = asset_library_factory or ArkAssetLibrary
        asset_library = asset_library_builder(
            ark_config,
            project_name,
        )

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

    image_urls: list[str] = []
    for asset in [] if legacy_query_only else (plan.get("images") or []):
        if (
            asset.get("reference_role") == "character"
            and isinstance(character_reference, dict)
        ):
            if asset_library is None:
                raise SeedanceError("内部错误：缺少私域人像素材客户端。")
            planned_asset_id = normalize_asset_id(character_reference.get("asset_id"))
            character_url = None
            if not planned_asset_id:
                character_url = partial(
                    asset_url,
                    str(asset["id"]),
                    asset["identity"],
                )
            character_asset_uri = ensure_character_asset_uri(
                plan,
                state,
                state_path,
                asset_library,
                character_url,
                project_name,
                float(getattr(args, "asset_poll_interval", 10.0)),
                float(getattr(args, "asset_poll_timeout", 1800.0)),
                bool(getattr(args, "retry_failed_character_asset", False)),
                bool(
                    getattr(
                        args,
                        "allow_recreate_ambiguous_character_asset",
                        False,
                    )
                ),
            )
            if not character_asset_uri:
                raise SeedanceError("内部错误：人物 Asset URI 为空。")
            image_urls.append(character_asset_uri)
        else:
            image_urls.append(asset_url(str(asset["id"]), asset["identity"]))
    audio_url = None
    if not legacy_query_only and isinstance(audio_reference, dict):
        audio_url = asset_url("audio-01", audio_reference["identity"])
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
                    f"第 {index} 段上次状态为 {previous_status}，不会自动创建新任务。"
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
        request_body = build_request(
            plan, segment, image_urls, depth_url, audio_url
        )
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
            configure_network_environment()
            if args.poll_interval < 0 or args.poll_timeout <= 0:
                raise SeedanceError("轮询间隔不能为负数，超时时间必须为正数。")
            if args.asset_poll_interval < 0 or args.asset_poll_timeout <= 0:
                raise SeedanceError(
                    "素材轮询间隔不能为负数，超时时间必须为正数。"
                )
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
