#!/usr/bin/env python3
"""Two-stage video prompting: Omni facts, then Max verification and appearance."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from media_preflight import MediaPreflightError, validate_seedance_image_count
from qwen_video_prompt_reverse import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_INLINE_REQUEST_MB,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_OMNI_MODEL,
    MediaResolver,
    ScriptError,
    add_image_content,
    call_omni,
    call_qwen,
    completion_finish_reason,
    load_api_key,
    parse_spoken_replacements,
    probe_video,
    promote_candidate,
    require_complete_finish,
    require_readable_file,
    validate_image_api_limits,
    validate_prompt_contract,
    validate_video_api_limits,
    write_json_output,
    write_text_output,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_DIR = SCRIPT_DIR.parent / "prompts"
DEFAULT_OMNI_PROMPT = PROMPT_DIR / "video_reverse_omni_facts_system.txt"
DEFAULT_MAX_PROMPT = PROMPT_DIR / "video_reverse_max_verify_appearance_system.txt"
MIN_SEGMENT_SECONDS = 4
VISUAL_FIELDS = (
    "shot_scale",
    "camera",
    "composition",
    "visible_body_range",
    "subject_action",
    "operator_product_action",
    "entry_exit",
    "scene_light",
)

CORRECTION_PATH_CONTRACT = {
    "subjects": {
        "when": "主体清单或任一主体静态描述变化",
        "path": "subjects",
        "value_shape": "完整 subjects 数组",
    },
    "shot_plan": {
        "when": "段内镜头数量、顺序、start_seconds 或 end_seconds 变化",
        "path": "segments[i].shot_plan",
        "value_shape": "完整镜头计划数组，每项只含 index、start_seconds、end_seconds",
        "value_fields": ["index", "start_seconds", "end_seconds"],
        "forbidden_paths": [
            "segments[i].shots[j].start_seconds",
            "segments[i].shots[j].end_seconds",
        ],
    },
    "shot_visuals": {
        "when": "shot_plan 变化且镜头视觉字段或 beats 同时变化",
        "path": "segments[i].shot_visuals",
        "value_shape": "完整镜头视觉数组，不含 index、起止时间或 audio",
        "value_fields": [*VISUAL_FIELDS, "beats"],
    },
    "shot_field": {
        "when": "shot_plan 不变，仅单个镜头视觉字段变化",
        "path": "segments[i].shots[j].<visual_field>",
        "value_shape": "该字段的完整原值与修正值",
    },
    "beat_plan": {
        "when": "shot_plan 不变，但镜头内 beat 数量、顺序或时间区间变化",
        "path": "segments[i].shots[j].beat_plan",
        "value_shape": "完整 beat 计划数组",
    },
    "beat_actions": {
        "when": "beat_plan 变化且动作集合同时变化",
        "path": "segments[i].shots[j].beat_actions",
        "value_shape": "完整 beat action 数组",
    },
}
TIMELINE_CONTRACT = {
    "time_type": "JSON整数",
    "applies_to": [
        "segments[i].shots[j].start_seconds",
        "segments[i].shots[j].end_seconds",
        "segments[i].shots[j].beats[k].start_seconds",
        "segments[i].shots[j].beats[k].end_seconds",
    ],
    "continuity": "镜头连续覆盖完整分段，beats 连续覆盖完整镜头，无缺口或重叠",
    "fractional_seconds_allowed": False,
}
QUALITY_CONSTRAINT = (
    "全片约束：人物、产品与场景外观全程保持一致。"
    "不要字幕、叠加文字、乱码或平台水印；"
    "不新增未提供的品牌、包装文字或标识。"
)
UNAVAILABLE_AUDIO_REFERENCE_PATTERN = re.compile(
    r"原片|原视频|原始音轨|原曲|参考视频"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Omni 提取原片事实，Max 根据原片核验事实并绑定替换外观。"
    )
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--segment-max-seconds", type=int, choices=(15, 30), required=True)
    parser.add_argument("--character-image", type=Path)
    parser.add_argument("--product-image", type=Path, action="append", default=[])
    parser.add_argument("--product-name", default="")
    parser.add_argument("--selling-points", default="")
    parser.add_argument("--user-idea", default="")
    parser.add_argument("--allow-audio-rewrite", action="store_true")
    parser.add_argument("--spoken-replacement", action="append", default=[])
    parser.add_argument("--transcript-file", type=Path)
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--base-url", default=os.environ.get("DASHSCOPE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("QWEN_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--omni-model",
        default=os.environ.get("QWEN_OMNI_MODEL", DEFAULT_OMNI_MODEL),
    )
    parser.add_argument("--fps", type=float, default=4.0)
    parser.add_argument("--omni-system-prompt", type=Path, default=DEFAULT_OMNI_PROMPT)
    parser.add_argument("--max-system-prompt", type=Path, default=DEFAULT_MAX_PROMPT)
    parser.add_argument("--omni-facts-output", type=Path, required=True)
    parser.add_argument("--omni-facts-file", type=Path)
    parser.add_argument("--omni-metadata-output", type=Path, required=True)
    parser.add_argument("--omni-metadata-file", type=Path)
    parser.add_argument("--draft-output", type=Path, required=True)
    parser.add_argument("--verification-output", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--segment-plan-output", type=Path, required=True)
    parser.add_argument("--fact-lock-output", type=Path, required=True)
    parser.add_argument("--omni-request-body-output", type=Path)
    parser.add_argument("--omni-response-body-output", type=Path)
    parser.add_argument("--max-request-body-output", type=Path)
    parser.add_argument("--max-response-body-output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--max-inline-request-mb",
        type=float,
        default=DEFAULT_MAX_INLINE_REQUEST_MB,
    )
    return parser.parse_args()


def read_text(path: Path, label: str) -> str:
    resolved = require_readable_file(path, label)
    value = resolved.read_text(encoding="utf-8").strip()
    if not value:
        raise ScriptError(f"{label}内容为空：{resolved}")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = require_readable_file(path, "输入文件")
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": file_sha256(resolved),
    }


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def parse_json_object(text: str, label: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean, count=1)
        clean = re.sub(r"\s*```$", "", clean, count=1)
    start = clean.find("{")
    end = clean.rfind("}")
    if start < 0 or end < start:
        raise ScriptError(f"{label}没有返回 JSON 对象。")
    def reject_constant(value: str) -> None:
        raise ScriptError(f"{label}包含非有限数：{value}")

    try:
        result = json.loads(
            clean[start : end + 1],
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ScriptError(f"{label} JSON 无效：{exc}") from exc
    if not isinstance(result, dict):
        raise ScriptError(f"{label}根节点必须是对象。")
    return result


def integer(value: Any, label: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScriptError(f"{label}不是数字。") from exc
    rounded = round(number)
    if abs(number - rounded) > 0.02:
        raise ScriptError(f"{label}必须是整数秒：{number}")
    return int(rounded)


def normalize_beats(
    value: Any,
    shot_start: int,
    shot_end: int,
    default_action: str,
    label: str,
) -> list[dict[str, Any]]:
    if value is None:
        return [
            {
                "index": 1,
                "start_seconds": shot_start,
                "end_seconds": shot_end,
                "action": default_action,
            }
        ]
    if not isinstance(value, list) or not value:
        raise ScriptError(f"{label} beats 必须是非空数组。")
    normalized: list[dict[str, Any]] = []
    cursor = shot_start
    for beat_index, beat in enumerate(value, start=1):
        if not isinstance(beat, dict):
            raise ScriptError(f"{label} 第 {beat_index} 个 beat 无效。")
        index = integer(beat.get("index"), f"{label} beat index")
        start = integer(
            beat.get("start_seconds"),
            f"{label} beat start_seconds",
        )
        end = integer(
            beat.get("end_seconds"),
            f"{label} beat end_seconds",
        )
        action = str(beat.get("action") or "").strip()
        if (
            index != beat_index
            or start != cursor
            or end <= start
            or not action
            or "{" in action
            or "}" in action
        ):
            raise ScriptError(f"{label} 第 {beat_index} 个 beat 无效或不连续。")
        normalized.append(
            {
                "index": index,
                "start_seconds": start,
                "end_seconds": end,
                "action": action,
            }
        )
        cursor = end
    if cursor != shot_end:
        raise ScriptError(f"{label} beats 未覆盖完整镜头时长。")
    return normalized


def default_beat_action(shot: dict[str, Any]) -> str:
    value = "；".join(
        str(item).strip()
        for item in (
            shot.get("subject_action"),
            shot.get("operator_product_action"),
        )
        if str(item or "").strip() not in {"", "无", "没有", "无操作"}
    )
    return value or "主体状态保持不变"


def uses_summary_beat(shot: dict[str, Any]) -> bool:
    beats = shot.get("beats")
    return bool(
        isinstance(beats, list)
        and len(beats) == 1
        and int(beats[0]["start_seconds"]) == int(shot["start_seconds"])
        and int(beats[0]["end_seconds"]) == int(shot["end_seconds"])
        and str(beats[0]["action"]) == default_beat_action(shot)
    )


def validate_facts(
    body: dict[str, Any],
    duration_seconds: int,
    segment_max_seconds: int,
) -> dict[str, Any]:
    subjects = body.get("subjects")
    segments = body.get("segments")
    if not isinstance(subjects, list) or not subjects:
        raise ScriptError("结构化事实缺少 subjects。")
    if not isinstance(segments, list) or not segments:
        raise ScriptError("结构化事实缺少 segments。")
    normalized_subjects: list[dict[str, str]] = []
    labels: set[str] = set()
    for index, subject in enumerate(subjects, start=1):
        if not isinstance(subject, dict):
            raise ScriptError(f"第 {index} 个 subject 无效。")
        label = str(subject.get("label") or "").strip()
        kind = str(subject.get("kind") or "").strip()
        original = str(subject.get("original_static_description") or "").strip()
        if not re.fullmatch(r"<[^<>]+>", label) or label in labels:
            raise ScriptError(f"第 {index} 个 subject 标签无效或重复：{label}")
        if kind not in {"character", "operator", "product", "object", "scene"}:
            raise ScriptError(f"第 {index} 个 subject kind 无效：{kind}")
        if not original:
            raise ScriptError(f"第 {index} 个 subject 缺少静态描述。")
        labels.add(label)
        normalized_subjects.append(
            {"label": label, "kind": kind, "original_static_description": original}
        )
    expected_count = math.ceil(duration_seconds / segment_max_seconds)
    if len(segments) != expected_count:
        raise ScriptError(f"分段数量不是最少任务数：{len(segments)} != {expected_count}")
    normalized_segments: list[dict[str, Any]] = []
    source_cursor = 0
    saw_speech = False
    for segment_index, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict):
            raise ScriptError(f"第 {segment_index} 段无效。")
        index = integer(segment.get("index"), "segment index")
        start = integer(segment.get("source_start_seconds"), "source_start_seconds")
        end = integer(segment.get("source_end_seconds"), "source_end_seconds")
        duration = integer(segment.get("duration_seconds"), "duration_seconds")
        if index != segment_index or start != source_cursor or end - start != duration:
            raise ScriptError(f"第 {segment_index} 段编号或源时间轴不连续。")
        if not MIN_SEGMENT_SECONDS <= duration <= segment_max_seconds:
            raise ScriptError(f"第 {segment_index} 段时长不合法：{duration}")
        shots = segment.get("shots")
        if not isinstance(shots, list) or not shots:
            raise ScriptError(f"第 {segment_index} 段缺少 shots。")
        shot_cursor = 0
        normalized_shots: list[dict[str, Any]] = []
        for shot_index, shot in enumerate(shots, start=1):
            if not isinstance(shot, dict):
                raise ScriptError(f"第 {segment_index} 段镜头无效。")
            index_value = integer(shot.get("index"), "shot index")
            shot_start = integer(shot.get("start_seconds"), "shot start_seconds")
            shot_end = integer(shot.get("end_seconds"), "shot end_seconds")
            if index_value != shot_index or shot_start != shot_cursor or shot_end <= shot_start:
                raise ScriptError(f"第 {segment_index} 段镜头时间轴不连续。")
            normalized_shot: dict[str, Any] = {
                "index": index_value,
                "start_seconds": shot_start,
                "end_seconds": shot_end,
            }
            for field in VISUAL_FIELDS:
                value = str(shot.get(field) or "").strip()
                if not value or "{" in value or "}" in value:
                    raise ScriptError(
                        f"第 {segment_index} 段镜头 {shot_index} 的 {field} 无效。"
                    )
                normalized_shot[field] = value
            normalized_shot["beats"] = normalize_beats(
                shot.get("beats"),
                shot_start,
                shot_end,
                default_beat_action(normalized_shot),
                f"第 {segment_index} 段镜头 {shot_index}",
            )
            audio = str(shot.get("audio") or "").strip()
            if audio.count("{") != audio.count("}"):
                raise ScriptError(f"第 {segment_index} 段镜头音频大括号不配对。")
            if "{" in audio:
                saw_speech = True
            normalized_shot["audio"] = audio
            normalized_shots.append(normalized_shot)
            shot_cursor = shot_end
        if shot_cursor != duration:
            raise ScriptError(f"第 {segment_index} 段镜头未覆盖完整时长。")
        normalized_segments.append(
            {
                "index": index,
                "source_start_seconds": start,
                "source_end_seconds": end,
                "duration_seconds": duration,
                "shots": normalized_shots,
            }
        )
        source_cursor = end
    if source_cursor != duration_seconds:
        raise ScriptError("结构化事实没有覆盖完整目标时长。")
    no_speech = bool(body.get("no_speech_confirmed"))
    if saw_speech == no_speech:
        raise ScriptError("人声事实与 no_speech_confirmed 矛盾。")
    return {
        "schema_version": 1,
        "no_speech_confirmed": no_speech,
        "subjects": normalized_subjects,
        "segments": normalized_segments,
    }


def segment_structure(facts: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [
        (
            segment["index"],
            segment["source_start_seconds"],
            segment["source_end_seconds"],
            segment["duration_seconds"],
        )
        for segment in facts["segments"]
    ]


def evidence_times(start: float, end: float, source_duration: float) -> tuple[float, ...]:
    actual_end = min(end, source_duration)
    if actual_end <= start:
        return (round(start, 3),)
    values = (start, (start + actual_end) / 2, actual_end)
    return tuple(dict.fromkeys(round(value, 3) for value in values))


def aggregate_segment_evidence_times(
    segment: dict[str, Any],
    source_duration: float,
) -> tuple[float, ...]:
    segment_start = float(segment["source_start_seconds"])
    segment_end = float(segment["source_end_seconds"])
    values = set(evidence_times(segment_start, segment_end, source_duration))
    for shot in segment["shots"]:
        shot_start = segment_start + float(shot["start_seconds"])
        shot_end = segment_start + float(shot["end_seconds"])
        values.update(evidence_times(shot_start, shot_end, source_duration))
        for beat in shot.get("beats") or []:
            beat_start = segment_start + float(beat["start_seconds"])
            beat_end = segment_start + float(beat["end_seconds"])
            values.update(evidence_times(beat_start, beat_end, source_duration))
    return tuple(sorted(values))


def evidence_time_index(facts: dict[str, Any], source_duration: float) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for segment_pos, segment in enumerate(facts["segments"]):
        segment_start = float(segment["source_start_seconds"])
        segment_end = float(segment["source_end_seconds"])
        segment_path = f"segments[{segment_pos}]"
        segment_allowed = list(
            evidence_times(segment_start, segment_end, source_duration)
        )
        result[segment_path] = segment_allowed
        aggregate_allowed = list(
            aggregate_segment_evidence_times(segment, source_duration)
        )
        result[segment_path + ".shot_plan"] = aggregate_allowed
        result[segment_path + ".shot_visuals"] = aggregate_allowed
        for shot_pos, shot in enumerate(segment["shots"]):
            start = segment_start + float(shot["start_seconds"])
            end = segment_start + float(shot["end_seconds"])
            shot_path = f"segments[{segment_pos}].shots[{shot_pos}]"
            shot_allowed = list(evidence_times(start, end, source_duration))
            result[shot_path] = shot_allowed
            result[shot_path + ".beat_plan"] = shot_allowed
            result[shot_path + ".beat_actions"] = shot_allowed
            for beat_pos, beat in enumerate(shot.get("beats") or []):
                beat_start = segment_start + float(beat["start_seconds"])
                beat_end = segment_start + float(beat["end_seconds"])
                result[
                    shot_path + f".beats[{beat_pos}]"
                ] = list(evidence_times(beat_start, beat_end, source_duration))
    return result


def visual_differences(
    omni: dict[str, Any],
    verified: dict[str, Any],
    source_duration: float,
) -> dict[str, tuple[Any, Any, tuple[float, ...]]]:
    if segment_structure(omni) != segment_structure(verified):
        raise ScriptError("Max 不得修改段级数量、顺序、边界或时长。")
    differences: dict[str, tuple[Any, Any, tuple[float, ...]]] = {}
    if omni["subjects"] != verified["subjects"]:
        differences["subjects"] = (
            omni["subjects"],
            verified["subjects"],
            evidence_times(0, source_duration, source_duration),
        )
    for segment_pos, (omni_segment, verified_segment) in enumerate(
        zip(omni["segments"], verified["segments"])
    ):
        source_offset = float(omni_segment["source_start_seconds"])
        source_end = float(omni_segment["source_end_seconds"])
        omni_audio = "".join(str(shot["audio"]) for shot in omni_segment["shots"])
        verified_audio = "".join(
            str(shot["audio"]) for shot in verified_segment["shots"]
        )
        if omni_audio != verified_audio:
            raise ScriptError("Max 不得修改 Omni 音频事实；音频改写走 audio_overrides。")
        omni_plan = [
            {
                "index": shot["index"],
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
            }
            for shot in omni_segment["shots"]
        ]
        verified_plan = [
            {
                "index": shot["index"],
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
            }
            for shot in verified_segment["shots"]
        ]
        if omni_plan != verified_plan:
            allowed = aggregate_segment_evidence_times(
                omni_segment,
                source_duration,
            )
            differences[f"segments[{segment_pos}].shot_plan"] = (
                omni_plan,
                verified_plan,
                allowed,
            )
            omni_visuals = [
                {
                    **{field: shot[field] for field in VISUAL_FIELDS},
                    "beats": shot["beats"],
                }
                for shot in omni_segment["shots"]
            ]
            verified_visuals = [
                {
                    **{field: shot[field] for field in VISUAL_FIELDS},
                    "beats": shot["beats"],
                }
                for shot in verified_segment["shots"]
            ]
            if omni_visuals != verified_visuals:
                differences[f"segments[{segment_pos}].shot_visuals"] = (
                    omni_visuals,
                    verified_visuals,
                    allowed,
                )
            continue
        for shot_pos, (omni_shot, verified_shot) in enumerate(
            zip(omni_segment["shots"], verified_segment["shots"])
        ):
            if omni_shot["audio"] != verified_shot["audio"]:
                raise ScriptError("Max 不得修改 Omni 音频事实；音频改写走 audio_overrides。")
            shot_start = source_offset + float(omni_shot["start_seconds"])
            shot_end = source_offset + float(omni_shot["end_seconds"])
            compare_beats = not (
                uses_summary_beat(omni_shot) and uses_summary_beat(verified_shot)
            )
            omni_beat_plan = [
                {
                    "index": beat["index"],
                    "start_seconds": beat["start_seconds"],
                    "end_seconds": beat["end_seconds"],
                }
                for beat in omni_shot["beats"]
            ]
            verified_beat_plan = [
                {
                    "index": beat["index"],
                    "start_seconds": beat["start_seconds"],
                    "end_seconds": beat["end_seconds"],
                }
                for beat in verified_shot["beats"]
            ]
            if compare_beats and omni_beat_plan != verified_beat_plan:
                differences[
                    f"segments[{segment_pos}].shots[{shot_pos}].beat_plan"
                ] = (
                    omni_beat_plan,
                    verified_beat_plan,
                    evidence_times(shot_start, shot_end, source_duration),
                )
                omni_actions = [beat["action"] for beat in omni_shot["beats"]]
                verified_actions = [
                    beat["action"] for beat in verified_shot["beats"]
                ]
                if omni_actions != verified_actions:
                    differences[
                        f"segments[{segment_pos}].shots[{shot_pos}].beat_actions"
                    ] = (
                        omni_actions,
                        verified_actions,
                        evidence_times(shot_start, shot_end, source_duration),
                    )
            elif compare_beats:
                for beat_pos, (omni_beat, verified_beat) in enumerate(
                    zip(omni_shot["beats"], verified_shot["beats"])
                ):
                    if omni_beat["action"] == verified_beat["action"]:
                        continue
                    beat_start = source_offset + float(omni_beat["start_seconds"])
                    beat_end = source_offset + float(omni_beat["end_seconds"])
                    differences[
                        f"segments[{segment_pos}].shots[{shot_pos}]."
                        f"beats[{beat_pos}].action"
                    ] = (
                        omni_beat["action"],
                        verified_beat["action"],
                        evidence_times(beat_start, beat_end, source_duration),
                    )
            for field in VISUAL_FIELDS:
                before = omni_shot[field]
                after = verified_shot[field]
                if before == after:
                    continue
                path = f"segments[{segment_pos}].shots[{shot_pos}].{field}"
                start = source_offset + float(omni_shot["start_seconds"])
                end = source_offset + float(omni_shot["end_seconds"])
                differences[path] = (
                    before,
                    after,
                    evidence_times(start, end, source_duration),
                )
    return differences


def validate_corrections(
    review: dict[str, Any],
    differences: dict[str, tuple[Any, Any, tuple[float, ...]]],
) -> list[dict[str, Any]]:
    status = str(review.get("status") or "").strip()
    corrections = review.get("corrections")
    if status not in {"unchanged", "corrected"} or not isinstance(corrections, list):
        raise ScriptError("Max fact_review 缺少有效 status 或 corrections。")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for correction in corrections:
        if not isinstance(correction, dict):
            raise ScriptError("Max correction 必须是对象。")
        path = str(correction.get("path") or "").strip()
        before = correction.get("omni_value")
        after = correction.get("corrected_value")
        evidence = correction.get("evidence_times")
        description = str(correction.get("evidence_description") or "").strip()
        if path not in differences or path in seen:
            raise ScriptError(f"Max correction 路径无对应事实变化或重复：{path}")
        expected_before, expected_after, allowed_times = differences[path]
        if before != expected_before or after != expected_after:
            raise ScriptError(
                "Max correction 的原值或修正值与事实差异不一致："
                f"{path}；必须严格使用 correction_path_contract 的 value_fields，"
                "不能增加其他字段。"
            )
        if not isinstance(evidence, list) or not evidence or not description:
            raise ScriptError(f"Max correction 缺少时间证据或说明：{path}")
        if any(isinstance(value, bool) for value in evidence):
            raise ScriptError(f"Max correction 证据时间必须是数字：{path}")
        try:
            evidence_values = [float(value) for value in evidence]
        except (TypeError, ValueError) as exc:
            raise ScriptError(f"Max correction 证据时间必须是数字：{path}") from exc
        if len(set(evidence_values)) < min(2, len(allowed_times)):
            raise ScriptError(f"Max correction 至少需要两个不同的证据时间点：{path}")
        if any(not math.isfinite(value) for value in evidence_values):
            raise ScriptError(f"Max correction 证据时间必须是有限数：{path}")
        if any(
            not any(abs(value - allowed) <= 0.001 for allowed in allowed_times)
            for value in evidence_values
        ):
            raise ScriptError(
                f"Max correction 证据时间必须来自允许的起点、中点或终点：{path}"
            )
        seen.add(path)
        normalized.append(
            {
                "path": path,
                "omni_value": before,
                "corrected_value": after,
                "evidence_times": evidence_values,
                "evidence_description": description,
            }
        )
    if seen != set(differences):
        raise ScriptError("Max 未逐项解释全部事实变化。")
    if (status == "unchanged") != (not differences):
        raise ScriptError("Max fact_review status 与实际事实变化不一致。")
    return normalized


def validate_appearance_bindings(
    bindings: Any,
    facts: dict[str, Any],
    character_image_present: bool,
    product_image_count: int,
) -> dict[str, list[int]]:
    if not isinstance(bindings, list):
        raise ScriptError("Max appearance_bindings 必须是数组。")
    expected_labels = [subject["label"] for subject in facts["subjects"]]
    subject_by_label = {subject["label"]: subject for subject in facts["subjects"]}
    result: dict[str, list[int]] = {}
    owners: dict[int, str] = {}
    character_index = 1 if character_image_present else None
    product_start = 2 if character_image_present else 1
    product_indices = set(range(product_start, product_start + product_image_count))
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {"label", "image_refs"}:
            raise ScriptError("Max appearance binding 必须是对象。")
        label = str(item.get("label") or "").strip()
        image_refs = item.get("image_refs")
        if label not in expected_labels or label in result:
            raise ScriptError(f"Max appearance label 无效或重复：{label}")
        if not isinstance(image_refs, list):
            raise ScriptError(f"Max appearance image_refs 必须是数组：{label}")
        refs = [integer(value, f"{label} image_ref") for value in image_refs]
        if len(refs) != len(set(refs)):
            raise ScriptError(f"Max appearance image_refs 重复：{label}")
        kind = subject_by_label[label]["kind"]
        for ref in refs:
            if ref in owners:
                raise ScriptError(f"@图片{ref} 被多个主体重复绑定。")
            if ref == character_index:
                if kind != "character":
                    raise ScriptError(f"人物图 @图片{ref} 只能绑定 character 主体。")
            elif ref in product_indices:
                if kind != "product":
                    raise ScriptError(f"产品图 @图片{ref} 只能绑定 product 主体。")
            else:
                raise ScriptError(f"Max appearance 引用了不存在的 @图片{ref}。")
            owners[ref] = label
        result[label] = refs
    if list(result) != expected_labels:
        raise ScriptError("Max appearance bindings 的数量、顺序或标签不一致。")
    expected_refs = ({character_index} if character_index else set()) | product_indices
    if set(owners) != expected_refs:
        raise ScriptError(
            f"Max 图片绑定不一致：expected={sorted(expected_refs)}, "
            f"actual={sorted(owners)}"
        )
    return result


def definitions_from_bindings(
    facts: dict[str, Any],
    bindings: dict[str, list[int]],
) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for subject in facts["subjects"]:
        label = subject["label"]
        refs = bindings[label]
        if not refs:
            definitions[label] = (
                f"将{subject['original_static_description']}定义为{label}。"
            )
            continue
        noun = "人物" if subject["kind"] == "character" else "产品"
        joined = "、".join(f"@图片{ref}" for ref in refs)
        if noun == "人物":
            reference_scope = (
                "只参考人物的面部、发型、体型、服装和配饰等静态外观，"
                "不参考图片中的姿态、动作、景别、机位或拼版布局；"
                "若素材包含同一人物的多视图或拼版，各视图共同定义同一主体，"
                "不生成多个副本"
            )
        else:
            reference_scope = (
                "只参考产品的外形、颜色、材质、包装和已有标识等静态外观，"
                "不参考图片中的手持动作、构图、景别或场景"
            )
        definitions[label] = (
            f"{joined}是{label}的静态外观参考；{reference_scope}；"
            f"全文统一称为{label}。"
        )
    return definitions


def validate_audio_overrides(
    value: Any,
    facts: dict[str, Any],
    allowed: bool,
) -> dict[tuple[int, int], str]:
    if not isinstance(value, list):
        raise ScriptError("Max audio_overrides 必须是数组。")
    if value and not allowed:
        raise ScriptError("未授权整段口播改写时 audio_overrides 必须为空。")
    shots = {
        (int(segment["index"]), int(shot["index"]))
        for segment in facts["segments"]
        for shot in segment["shots"]
    }
    result: dict[tuple[int, int], str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ScriptError("Max audio override 必须是对象。")
        key = (
            integer(item.get("segment_index"), "audio segment_index"),
            integer(item.get("shot_index"), "audio shot_index"),
        )
        audio = str(item.get("audio") or "").strip()
        if (
            key not in shots
            or key in result
            or not audio
            or audio.count("{") != audio.count("}")
        ):
            raise ScriptError(f"Max audio override 无效：{key}")
        unavailable = UNAVAILABLE_AUDIO_REFERENCE_PATTERN.search(audio)
        if unavailable:
            raise ScriptError(
                "Max audio override 引用了不会提交给 Seedance 的原始媒体："
                f"{unavailable.group(0)}"
            )
        result[key] = audio
    return result


def validate_max_result(
    body: dict[str, Any],
    omni: dict[str, Any],
    duration_seconds: int,
    segment_max_seconds: int,
    character_image_present: bool,
    product_image_count: int,
    allow_audio_rewrite: bool,
    source_duration_seconds: float,
) -> tuple[
    dict[str, Any],
    dict[str, list[int]],
    dict[tuple[int, int], str],
    list[dict[str, Any]],
]:
    allowed_keys = {
        "fact_review",
        "verified_source_facts",
        "appearance_bindings",
        "audio_overrides",
    }
    extra = set(body) - allowed_keys
    if extra or set(body) != allowed_keys:
        raise ScriptError("Max 结果字段不完整或越权：" + ", ".join(sorted(extra)))
    verified = validate_facts(
        body["verified_source_facts"],
        duration_seconds,
        segment_max_seconds,
    )
    differences = visual_differences(omni, verified, source_duration_seconds)
    corrections = validate_corrections(body["fact_review"], differences)
    definitions = validate_appearance_bindings(
        body["appearance_bindings"],
        verified,
        character_image_present,
        product_image_count,
    )
    overrides = validate_audio_overrides(
        body["audio_overrides"],
        verified,
        allow_audio_rewrite,
    )
    return verified, definitions, overrides, corrections


def timecode(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_segment_overview(
    facts: dict[str, Any],
    segment: dict[str, Any],
) -> str:
    labels = [
        subject["label"]
        for subject in facts["subjects"]
        if subject["kind"] != "scene"
    ]
    subject_text = "、".join(labels) or "画面主体"
    shots = segment["shots"]
    if len(shots) == 1:
        shot = shots[0]
        return (
            f"生成目标：在{shot['scene_light']}中，以{shot['shot_scale']}、"
            f"{shot['camera']}呈现{subject_text}的连续动作与互动，"
            "保持真实摄影质感。"
        )
    return (
        f"生成目标：严格按下方时间轴呈现{subject_text}的连续动作、"
        "场景和镜头变化，保持真实摄影质感。"
    )


def render_shot_static(shot: dict[str, Any]) -> str:
    parts = [
        f"{shot['shot_scale']}，{shot['camera']}",
        shot["composition"],
        f"画面可见范围：{shot['visible_body_range']}",
        shot["entry_exit"],
        shot["scene_light"],
    ]
    return "。".join(part.strip().rstrip("。") for part in parts if part.strip()) + "。"


def render_prompt(
    facts: dict[str, Any],
    definitions: dict[str, str],
    audio_overrides: dict[tuple[int, int], str],
) -> str:
    definition_block = "\n".join(definitions[subject["label"]] for subject in facts["subjects"])
    multi = len(facts["segments"]) > 1
    sections: list[str] = []
    for segment in facts["segments"]:
        lines: list[str] = []
        if multi:
            lines.append(
                f"【第{segment['index']}段提示词（{segment['duration_seconds']}秒，"
                f"对齐参考视频{segment['source_start_seconds']}-"
                f"{segment['source_end_seconds']}秒）】"
            )
        lines.append(definition_block)
        lines.append(render_segment_overview(facts, segment))
        for shot in segment["shots"]:
            lines.append(
                f"镜头{shot['index']}[{timecode(shot['start_seconds'])}-"
                f"{timecode(shot['end_seconds'])}]"
            )
            lines.append("画面：" + render_shot_static(shot))
            for beat in shot["beats"]:
                lines.append(
                    f"动作阶段{beat['index']}[{timecode(beat['start_seconds'])}-"
                    f"{timecode(beat['end_seconds'])}]："
                    f"{str(beat['action']).rstrip('。')}。"
                )
            audio = audio_overrides.get(
                (int(segment["index"]), int(shot["index"])),
                str(shot["audio"]),
            )
            if audio:
                lines.append("声音：" + audio.rstrip("。") + "。")
        lines.append(QUALITY_CONSTRAINT)
        sections.append("\n".join(lines))
    return "\n\n".join(sections).strip()


def default_definitions(facts: dict[str, Any]) -> dict[str, str]:
    return {
        item["label"]: f"将{item['original_static_description']}定义为{item['label']}。"
        for item in facts["subjects"]
    }


def apply_word_replacements(
    facts: dict[str, Any], replacements: list[tuple[str, str]]
) -> None:
    for source, _ in replacements:
        if not any(
            source in str(shot["audio"])
            for segment in facts["segments"]
            for shot in segment["shots"]
        ):
            raise ScriptError(f"指定口播替换的旧词未出现在音频事实中：{source}")
    for segment in facts["segments"]:
        for shot in segment["shots"]:
            for source, target in replacements:
                shot["audio"] = str(shot["audio"]).replace(source, target)


def build_omni_messages(
    system: str,
    video_reference: str,
    duration_seconds: int,
    maximum: int,
    has_audio: bool,
    transcript: str,
) -> list[dict[str, Any]]:
    context = {
        "duration_seconds": duration_seconds,
        "segment_max_seconds": maximum,
        "required_segment_count": math.ceil(duration_seconds / maximum),
        "has_audio": has_audio,
        "transcript": transcript or None,
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_reference}},
                {
                    "type": "text",
                    "text": "只输出结构化原片事实 JSON：\n"
                    + json.dumps(context, ensure_ascii=False, indent=2),
                },
            ],
        },
    ]


def build_max_messages(
    args: argparse.Namespace,
    system: str,
    video_reference: str,
    omni: dict[str, Any],
    source_duration: float,
    character_reference: str | None,
    product_references: list[str],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "video_url",
            "video_url": {"url": video_reference},
            "fps": args.fps,
        }
    ]
    number = 1
    if character_reference:
        add_image_content(content, character_reference, number, "人物形象图")
        number += 1
    for index, reference in enumerate(product_references, start=1):
        add_image_content(content, reference, number, f"第{index}张产品参考图")
        number += 1
    replacements = parse_spoken_replacements(args.spoken_replacement)
    if args.allow_audio_rewrite:
        audio_permission = (
            "允许通过 audio_overrides 改写音频；视觉事实仍只按原片核验。"
            "每项必须且只能使用 {\"segment_index\": JSON整数, "
            "\"shot_index\": JSON整数, \"audio\": \"完整替换音频描述\"}，"
            "并指向已存在的镜头，不得省略索引。audio 必须是 Seedance 可独立"
            "执行的具体声音描述，不得引用不会提交给 Seedance 的原片、原视频、"
            "原始音轨、原曲或参考视频。"
        )
    elif replacements:
        audio_permission = "audio_overrides 必须为空；程序将执行：" + "；".join(
            f"{source}→{target}" for source, target in replacements
        )
    else:
        audio_permission = "audio_overrides 必须为空。"
    context = {
        "omni_source_facts": omni,
        "allowed_evidence_times": evidence_time_index(omni, source_duration),
        "correction_path_contract": CORRECTION_PATH_CONTRACT,
        "timeline_contract": TIMELINE_CONTRACT,
        "available_image_count": number - 1,
        "product_name": args.product_name.strip(),
        "selling_points": args.selling_points.strip(),
        "user_idea": args.user_idea.strip(),
        "audio_permission": audio_permission,
    }
    content.append(
        {
            "type": "text",
            "text": "核验 Omni 事实并输出指定 JSON：\n"
            + json.dumps(context, ensure_ascii=False, indent=2),
        }
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": content},
    ]


def input_metadata(
    args: argparse.Namespace,
    video: Path,
    transcript: Path | None,
    duration_seconds: int,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "video": file_identity(video),
        "transcript": file_identity(transcript),
        "duration_seconds": duration_seconds,
        "segment_max_seconds": args.segment_max_seconds,
        "omni_model": args.omni_model,
        "fps": args.fps,
        "omni_prompt": file_identity(args.omni_system_prompt),
    }


def fact_lock(
    args: argparse.Namespace,
    video: Path,
    omni_path: Path,
    verification_path: Path,
    prompt: str,
    plan_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "locked",
        "assembly_mode": "deterministic_from_max_verified_facts",
        "prompt_sha256": text_sha256(prompt),
        "segment_plan_sha256": file_sha256(plan_path),
        "analysis_video": file_identity(video),
        "omni_facts": file_identity(omni_path),
        "max_verification": file_identity(verification_path),
        "omni_prompt": file_identity(args.omni_system_prompt),
        "max_prompt": file_identity(args.max_system_prompt),
        "omni_model": args.omni_model,
        "max_model": args.model,
        "fps": args.fps,
    }


def main() -> int:
    args = parse_args()
    try:
        if not 0.1 <= args.fps <= 10:
            raise ScriptError("--fps 必须在 0.1 到 10 之间。")
        if args.allow_audio_rewrite and args.spoken_replacement:
            raise ScriptError("--allow-audio-rewrite 与 --spoken-replacement 不能同时使用。")
        try:
            validate_seedance_image_count(
                args.segment_max_seconds,
                int(args.character_image is not None) + len(args.product_image),
            )
        except MediaPreflightError as exc:
            raise ScriptError(str(exc)) from exc
        api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            if not args.api_key_file:
                raise ScriptError("未设置 DASHSCOPE_API_KEY 或 --api-key-file。")
            api_key = load_api_key(args.api_key_file)
        video = require_readable_file(args.video, "分析视频")
        character = (
            require_readable_file(args.character_image, "人物形象图")
            if args.character_image
            else None
        )
        products = [
            require_readable_file(path, f"第{index}张产品图")
            for index, path in enumerate(args.product_image, start=1)
        ]
        transcript_path = (
            require_readable_file(args.transcript_file, "转写文件")
            if args.transcript_file
            else None
        )
        transcript = (
            transcript_path.read_text(encoding="utf-8").strip()
            if transcript_path
            else ""
        )
        metadata = probe_video(video)
        validate_video_api_limits(video, metadata)
        for index, image in enumerate(([character] if character else []) + products, start=1):
            validate_image_api_limits(image, f"@图片{index}", args.max_inline_request_mb)
        duration_seconds = int(metadata["duration_seconds"])
        source_duration = float(metadata["source_duration"])
        expected_meta = input_metadata(args, video, transcript_path, duration_seconds)
        omni_system = read_text(args.omni_system_prompt, "Omni 事实提示词")
        max_system = read_text(args.max_system_prompt, "Max 核验提示词")
        resolver = MediaResolver(args)
        video_reference = resolver.resolve(video, "video")

        if args.omni_facts_file:
            omni_path = require_readable_file(args.omni_facts_file, "Omni 事实")
            meta_path = require_readable_file(
                args.omni_metadata_file or args.omni_metadata_output,
                "Omni 事实元数据",
            )
            if json.loads(meta_path.read_text(encoding="utf-8")) != expected_meta:
                raise ScriptError("Omni 事实元数据与当前原片输入不一致。")
            omni = validate_facts(
                json.loads(omni_path.read_text(encoding="utf-8")),
                duration_seconds,
                args.segment_max_seconds,
            )
        else:
            omni_messages = build_omni_messages(
                omni_system,
                video_reference,
                duration_seconds,
                args.segment_max_seconds,
                bool(metadata["has_audio"]),
                transcript,
            )
            omni_payload = {
                "model": args.omni_model,
                "messages": omni_messages,
                "temperature": 0.2,
                "max_tokens": args.max_tokens,
                "modalities": ["text"],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if args.omni_request_body_output:
                write_json_output(
                    args.omni_request_body_output,
                    omni_payload,
                    args.overwrite,
                    "Omni 请求体",
                )
            raw_omni, omni_response = call_omni(
                args.base_url,
                api_key,
                omni_payload,
                args.timeout,
                args.retries,
                capture_chunks=bool(args.omni_response_body_output),
            )
            if args.omni_response_body_output:
                write_json_output(
                    args.omni_response_body_output,
                    omni_response,
                    args.overwrite,
                    "Omni 响应体",
                )
            require_complete_finish(
                str(omni_response.get("finish_reason") or ""),
                "Omni 结构化事实",
            )
            try:
                omni = validate_facts(
                    parse_json_object(raw_omni, "Omni 事实"),
                    duration_seconds,
                    args.segment_max_seconds,
                )
            except ScriptError as error:
                repair_payload = {
                    **omni_payload,
                    "messages": [
                        *omni_messages,
                        {"role": "assistant", "content": raw_omni},
                        {
                            "role": "user",
                            "content": f"机器校验失败：{error}\n重新核对原片并完整输出修复 JSON。",
                        },
                    ],
                    "temperature": 0.1,
                }
                raw_omni, omni_response = call_omni(
                    args.base_url,
                    api_key,
                    repair_payload,
                    args.timeout,
                    args.retries,
                    capture_chunks=False,
                )
                omni = validate_facts(
                    parse_json_object(raw_omni, "Omni 修复事实"),
                    duration_seconds,
                    args.segment_max_seconds,
                )

        write_json_output(
            args.omni_facts_output,
            omni,
            args.overwrite,
            "Omni 事实",
        )
        write_json_output(
            args.omni_metadata_output,
            expected_meta,
            args.overwrite,
            "Omni 事实元数据",
        )
        write_text_output(
            args.draft_output,
            render_prompt(omni, default_definitions(omni), {}),
            args.overwrite,
            "Omni 事实初稿",
        )

        character_reference = resolver.resolve(character, "image") if character else None
        product_references = [resolver.resolve(path, "image") for path in products]
        max_messages = build_max_messages(
            args,
            max_system,
            video_reference,
            omni,
            source_duration,
            character_reference,
            product_references,
        )
        max_payload = {
            "model": args.model,
            "messages": max_messages,
            "temperature": args.temperature,
            "max_tokens": min(args.max_tokens, 16384),
            "enable_thinking": False,
        }
        if args.max_request_body_output:
            write_json_output(
                args.max_request_body_output,
                max_payload,
                args.overwrite,
                "Max 请求体",
            )
        endpoint = f"{args.base_url.rstrip('/')}/chat/completions"
        raw_max, max_response = call_qwen(
            endpoint,
            api_key,
            max_payload,
            args.timeout,
            args.retries,
        )
        if args.max_response_body_output:
            write_json_output(
                args.max_response_body_output,
                max_response,
                args.overwrite,
                "Max 响应体",
            )
        require_complete_finish(completion_finish_reason(max_response), "Max 原片核验")
        write_text_output(
            args.candidate_output,
            raw_max,
            args.overwrite,
            "Max 核验候选",
        )
        expected_images = int(character is not None) + len(products)
        try:
            verified, bindings, overrides, corrections = validate_max_result(
                parse_json_object(raw_max, "Max 核验结果"),
                omni,
                duration_seconds,
                args.segment_max_seconds,
                character is not None,
                len(products),
                bool(args.allow_audio_rewrite),
                source_duration,
            )
        except ScriptError as error:
            repair_payload = {
                **max_payload,
                "messages": [
                    *max_messages,
                    {"role": "assistant", "content": raw_max},
                    {
                        "role": "user",
                        "content": (
                            f"机器校验失败：{error}\n重新查看原片，完整输出修复 JSON。"
                            "corrections 必须遵循用户上下文 correction_path_contract："
                            "镜头数量、顺序或起止时间变化统一使用 segments[i].shot_plan，"
                            "相关视觉同步变化使用 segments[i].shot_visuals，"
                            "shot_visuals 每项只含 correction_path_contract.value_fields，"
                            "不得额外放入 index、起止时间或 audio；"
                            "严格执行 timeline_contract，所有镜头与 beat 的起止时间必须是"
                            " JSON 整数并连续完整覆盖，禁止任何小数秒；"
                            "不得使用单个 start_seconds 或 end_seconds 路径，也不得申报无实际差异的字段。"
                            "只允许修正有时间证据的视觉字段、静态外观绑定和授权音频。"
                            "audio_overrides 非空时，每项必须且只能包含整数"
                            " segment_index、整数 shot_index 和字符串 audio；audio"
                            " 必须自包含，不得引用不会提交给 Seedance 的原始媒体。"
                        ),
                    },
                ],
                "temperature": 0.1,
            }
            raw_max, max_response = call_qwen(
                endpoint,
                api_key,
                repair_payload,
                args.timeout,
                args.retries,
            )
            require_complete_finish(
                completion_finish_reason(max_response),
                "Max 原片核验修复",
            )
            write_text_output(
                args.candidate_output,
                raw_max,
                True,
                "Max 核验修复候选",
            )
            verified, bindings, overrides, corrections = validate_max_result(
                parse_json_object(raw_max, "Max 核验修复结果"),
                omni,
                duration_seconds,
                args.segment_max_seconds,
                character is not None,
                len(products),
                bool(args.allow_audio_rewrite),
                source_duration,
            )

        replacements = parse_spoken_replacements(args.spoken_replacement)
        if replacements:
            apply_word_replacements(verified, replacements)
        definitions = definitions_from_bindings(verified, bindings)
        final_prompt = render_prompt(verified, definitions, overrides)
        segment_plan = validate_prompt_contract(
            final_prompt,
            duration_seconds,
            source_duration,
            args.segment_max_seconds,
            expected_images,
            "确定性组装终稿",
        )
        verification = {
            "schema_version": 1,
            "fact_review": {
                "status": "corrected" if corrections else "unchanged",
                "corrections": corrections,
            },
            "verified_source_facts": verified,
            "appearance_bindings": [
                {"label": label, "image_refs": image_refs}
                for label, image_refs in bindings.items()
            ],
            "audio_overrides": [
                {
                    "segment_index": key[0],
                    "shot_index": key[1],
                    "audio": value,
                }
                for key, value in overrides.items()
            ],
        }
        write_json_output(
            args.verification_output,
            verification,
            args.overwrite,
            "Max 核验结果",
        )
        write_text_output(
            args.candidate_output,
            final_prompt,
            True,
            "确定性组装候选",
        )
        write_json_output(
            args.segment_plan_output,
            segment_plan,
            args.overwrite,
            "分段计划",
        )
        write_json_output(
            args.fact_lock_output,
            fact_lock(
                args,
                video,
                args.omni_facts_output.resolve(),
                args.verification_output.resolve(),
                final_prompt,
                args.segment_plan_output.resolve(),
            ),
            args.overwrite,
            "事实锁定记录",
        )
        promote_candidate(
            args.candidate_output,
            args.output,
            args.overwrite,
            "最终提示词",
        )
        print(final_prompt)
        return 0
    except (ScriptError, json.JSONDecodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
