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


from video_prompt_format import FORMAT_VERSION, render_prompt as render_public_prompt


SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_DIR = SCRIPT_DIR.parent / "prompts"
DEFAULT_OMNI_PROMPT = PROMPT_DIR / "video_reverse_omni_facts_system.txt"
DEFAULT_MAX_PROMPT = PROMPT_DIR / "video_reverse_max_verify_appearance_system.txt"
MIN_SEGMENT_SECONDS = 4
MAX_CONFLICT_CONFIRMATION_REQUIRED = 3
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
    "overall_av": {
        "when": "全局视觉风格或声音基底变化",
        "path": "overall_av",
        "value_shape": "完整 overall_av 对象，包含 visual 与 audio",
    },
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
        "when": "shot_plan 不变，但镜头内 beat 动作数组变化；与 beat_plan 是否变化无关",
        "path": "segments[i].shots[j].beat_actions",
        "value_shape": "完整 beat action 数组",
    },
    "no_speech_confirmed": {
        "when": "Omni 对是否存在可辨识人声的判断与原片或逐字台词不一致",
        "path": "no_speech_confirmed",
        "value_shape": "JSON 布尔值",
    },
    "audio_plan": {
        "when": "shot_plan 变化且镜头音频归属或内容同时变化",
        "path": "segments[i].audio_plan",
        "value_shape": "完整音频计划数组，每项只含 index、start_seconds、end_seconds、audio",
        "value_fields": ["index", "start_seconds", "end_seconds", "audio"],
    },
    "shot_audio": {
        "when": "shot_plan 不变，仅单个镜头的原片音频事实变化",
        "path": "segments[i].shots[j].audio",
        "value_shape": "该镜头完整 audio 字符串",
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
PUBLIC_TIMELINE_CONTRACT = {
    **TIMELINE_CONTRACT,
    "time_type": "有限 JSON 数值，可含小数秒",
    "fractional_seconds_allowed": True,
    "display_only": "正文使用 MM:SS - MM:SS 近似显示，不据显示值改变真实镜头边界。",
}
UNAVAILABLE_AUDIO_REFERENCE_PATTERN = re.compile(
    r"原片|原视频|原始音轨|原曲|参考视频"
)
SUPPORTED_FACT_SCHEMA_VERSIONS = {1, 2, 3}
AUDIO_ATTRIBUTION_SCHEMA_VERSION = 2
DIALOGUE_PATTERN = re.compile(r"\{(?P<text>[^{}\n]+)\}")
ATTRIBUTED_DIALOGUE_PATTERN = re.compile(
    r"(?P<speaker><[^<>\n]+>)"
    r"（(?P<location>画内|画外)、(?P<sync>口型同步|无需口型)）"
    r"(?P<delivery>[^：；。{}<>\n]{0,12})"
    r"(?P<verb>说道|说|回应道|回应|回答道|回答|补充道|补充|"
    r"讲解道|讲解|解释道|解释|旁白)："
    r"\{(?P<text>[^{}\n]+)\}"
)
ATTRIBUTED_SPEECH_PREFIX_PATTERN = re.compile(
    r"<[^<>\n]+>"
    r"（(?:画内|画外)、(?:口型同步|无需口型)）"
    r"[^：；。{}<>\n]{0,12}"
    r"(?:说道|说|回应道|回应|回答道|回答|补充道|补充|"
    r"讲解道|讲解|解释道|解释|旁白)："
)
QUOTED_SPEECH_PATTERN = re.compile(
    r"(?:说|回应|回答|补充|讲解|解释|旁白)"
    r"[^。；\n]{0,12}[“\"][^”\"\n]+[”\"]"
)
EXPLICIT_NO_DIALOGUE_PATTERN = re.compile(
    r"无对白|移除(?:全部|所有)?(?:人物)?(?:台词|对白)"
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
    parser.add_argument("--omni-conflicts-output", type=Path, required=True)
    parser.add_argument("--omni-conflicts-file", type=Path)
    parser.add_argument("--omni-metadata-output", type=Path, required=True)
    parser.add_argument("--omni-metadata-file", type=Path)
    parser.add_argument("--draft-output", type=Path, required=True)
    parser.add_argument("--verification-output", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--segment-plan-output", type=Path, required=True)
    parser.add_argument("--fact-lock-output", type=Path, required=True)
    parser.add_argument("--max-conflict-report-output", type=Path, required=True)
    parser.add_argument("--confirm-max-conflicts", action="store_true")
    parser.add_argument("--omni-request-body-output", type=Path)
    parser.add_argument("--omni-response-body-output", type=Path)
    parser.add_argument("--max-request-body-output", type=Path)
    parser.add_argument("--max-response-body-output", type=Path)
    parser.add_argument(
        "--reuse-candidate",
        action="store_true",
        help="恢复时优先本地校验已有 Max 候选，通过后不重复调用 Max。",
    )
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


def reuse_captured_omni_response(
    request_path: Path | None,
    response_path: Path | None,
    expected_payload: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    if request_path is None or response_path is None:
        return None
    request_resolved = request_path.expanduser().resolve()
    response_resolved = response_path.expanduser().resolve()
    if not request_resolved.is_file() or not response_resolved.is_file():
        return None
    try:
        saved_request = json.loads(request_resolved.read_text(encoding="utf-8"))
        saved_response = json.loads(response_resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptError("已有 Omni 调试请求或响应不是有效 JSON。") from exc
    if saved_request != expected_payload:
        return None
    chunks = saved_response.get("chunks") if isinstance(saved_response, dict) else None
    if not isinstance(chunks, list):
        raise ScriptError("已有 Omni 调试响应缺少 chunks。")
    parts: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices") or []
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    text = "".join(parts).strip()
    if not text:
        raise ScriptError("已有 Omni 调试响应没有可复用文本。")
    print("OMNI_CAPTURE_REUSE accepted", flush=True)
    return text, saved_response


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


def precise_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScriptError(f"{label}必须是 JSON 数值。")
    if not math.isfinite(value) or value < 0:
        raise ScriptError(f"{label}必须是非负有限秒数。")
    return value


def same_time(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0, abs_tol=1e-6)


def dialogue_texts(audio: str) -> list[str]:
    return [match.group("text").strip() for match in DIALOGUE_PATTERN.finditer(audio)]


def validate_audio_description(
    value: Any,
    labels: set[str],
    require_attribution: bool,
    label: str,
) -> str:
    audio = str(value or "").strip()
    if audio.count("{") != audio.count("}"):
        raise ScriptError(f"{label}大括号不配对。")
    if not require_attribution or not audio:
        return audio
    if QUOTED_SPEECH_PATTERN.search(audio):
        raise ScriptError(f"{label}台词必须使用 {{}}，不得使用引号。")

    dialogue_matches = list(DIALOGUE_PATTERN.finditer(audio))
    attributed_matches = list(ATTRIBUTED_DIALOGUE_PATTERN.finditer(audio))
    if not dialogue_matches and ATTRIBUTED_SPEECH_PREFIX_PATTERN.search(audio):
        raise ScriptError(
            f"{label}检测到说话描述，但逐字台词没有使用 {{}}；"
            "必须按 <说话人>（画内、口型同步）说：{逐字台词} "
            "或画外对应形式输出。"
        )
    attributed_dialogue_spans = {
        (match.start("text") - 1, match.end("text") + 1)
        for match in attributed_matches
    }
    dialogue_spans = {match.span() for match in dialogue_matches}
    if dialogue_spans != attributed_dialogue_spans:
        raise ScriptError(
            f"{label}每句 {{}}台词都必须紧邻明确的说话人、"
            "画内/画外位置与口型状态。"
        )

    speakers: set[str] = set()
    for match in attributed_matches:
        speaker = match.group("speaker")
        location = match.group("location")
        sync = match.group("sync")
        if speaker not in labels and speaker != "<旁白>":
            raise ScriptError(f"{label}引用未定义说话人：{speaker}")
        if speaker == "<旁白>" and location != "画外":
            raise ScriptError(f"{label}<旁白>必须标记为画外。")
        if location == "画内" and sync != "口型同步":
            raise ScriptError(f"{label}画内说话必须标记口型同步。")
        if location == "画外" and sync != "无需口型":
            raise ScriptError(f"{label}画外说话必须标记无需口型。")
        speakers.add(speaker)
    if len(speakers - {"<旁白>"}) > 1 and not (
        "自然闭口" in audio or "重叠发声" in audio
    ):
        raise ScriptError(
            f"{label}多人对话必须说明未发言者自然闭口聆听，"
            "或明确标记重叠发声。"
        )
    return audio


def normalize_beats(
    value: Any,
    shot_start: float,
    shot_end: float,
    default_action: str,
    label: str,
    fractional: bool = False,
) -> list[dict[str, Any]]:
    parse_seconds = precise_seconds if fractional else integer
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
        start = parse_seconds(
            beat.get("start_seconds"),
            f"{label} beat start_seconds",
        )
        end = parse_seconds(
            beat.get("end_seconds"),
            f"{label} beat end_seconds",
        )
        action = str(beat.get("action") or "").strip()
        if (
            index != beat_index
            or not same_time(start, cursor)
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
    if not same_time(cursor, shot_end):
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
        and same_time(beats[0]["start_seconds"], shot["start_seconds"])
        and same_time(beats[0]["end_seconds"], shot["end_seconds"])
        and str(beats[0]["action"]) == default_beat_action(shot)
    )


def validate_facts(
    body: dict[str, Any],
    duration_seconds: int,
    segment_max_seconds: int,
    *,
    allow_audio_conflicts: bool = False,
    conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    conflict_items = conflicts if conflicts is not None else []
    schema_version = integer(body.get("schema_version", 1), "facts schema_version")
    if schema_version not in SUPPORTED_FACT_SCHEMA_VERSIONS:
        raise ScriptError(f"不支持的结构化事实 Schema：{schema_version}")
    overall_av = None
    if schema_version >= 3:
        overall_av = body.get("overall_av")
        if not isinstance(overall_av, dict) or set(overall_av) != {"visual", "audio"}:
            raise ScriptError("Schema v3 缺少 overall_av.visual/audio。")
        if any(not isinstance(value, str) or "{" in value or "}" in value for value in overall_av.values()):
            raise ScriptError("overall_av 必须是全局视听描述，不能包含台词全文。")
        if not overall_av["visual"].strip():
            raise ScriptError("overall_av.visual 不能为空。")
        overall_av = {key: value.strip() for key, value in overall_av.items()}
    parse_seconds = precise_seconds if schema_version >= 3 else integer
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
            shot_start = parse_seconds(shot.get("start_seconds"), "shot start_seconds")
            shot_end = parse_seconds(shot.get("end_seconds"), "shot end_seconds")
            if index_value != shot_index or not same_time(shot_start, shot_cursor) or shot_end <= shot_start:
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
                fractional=schema_version >= 3,
            )
            raw_audio = str(shot.get("audio") or "").strip()
            try:
                audio = validate_audio_description(
                    raw_audio,
                    labels,
                    schema_version >= AUDIO_ATTRIBUTION_SCHEMA_VERSION,
                    f"第 {segment_index} 段镜头 {shot_index} 音频：",
                )
            except ScriptError as error:
                if not allow_audio_conflicts:
                    raise
                audio = raw_audio
                conflict_items.append(
                    {
                        "path": (
                            f"segments[{segment_index - 1}].shots["
                            f"{shot_index - 1}].audio"
                        ),
                        "omni_value": raw_audio,
                        "issue": str(error),
                    }
                )
            if "{" in audio:
                saw_speech = True
            normalized_shot["audio"] = audio
            normalized_shots.append(normalized_shot)
            shot_cursor = shot_end
        if not same_time(shot_cursor, duration):
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
        issue = (
            "no_speech_confirmed=false，但 audio 中没有可机器识别的 "
            "{逐字台词}。"
            if not no_speech
            else "no_speech_confirmed=true，但 audio 中存在 {逐字台词}。"
        )
        if not allow_audio_conflicts:
            raise ScriptError(issue)
        conflict_items.append(
            {
                "path": "no_speech_confirmed",
                "omni_value": no_speech,
                "issue": issue,
            }
        )
    return {
        "schema_version": schema_version,
        **({"overall_av": overall_av} if overall_av is not None else {}),
        "no_speech_confirmed": no_speech,
        "subjects": normalized_subjects,
        "segments": normalized_segments,
    }


def validate_facts_for_max_arbitration(
    body: dict[str, Any],
    duration_seconds: int,
    segment_max_seconds: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    facts = validate_facts(
        body,
        duration_seconds,
        segment_max_seconds,
        allow_audio_conflicts=True,
        conflicts=conflicts,
    )
    if not conflicts:
        raise ScriptError("Max 仲裁模式没有检测到可恢复的 Omni 音频矛盾。")
    return facts, conflicts


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


def aggregate_shot_evidence_times(
    segment_start: float,
    shot: dict[str, Any],
    source_duration: float,
) -> tuple[float, ...]:
    shot_start = segment_start + float(shot["start_seconds"])
    shot_end = segment_start + float(shot["end_seconds"])
    values = set(evidence_times(shot_start, shot_end, source_duration))
    for beat in shot.get("beats") or []:
        beat_start = segment_start + float(beat["start_seconds"])
        beat_end = segment_start + float(beat["end_seconds"])
        values.update(evidence_times(beat_start, beat_end, source_duration))
    return tuple(sorted(values))


def aggregate_segment_evidence_times(
    segment: dict[str, Any],
    source_duration: float,
) -> tuple[float, ...]:
    segment_start = float(segment["source_start_seconds"])
    segment_end = float(segment["source_end_seconds"])
    values = set(evidence_times(segment_start, segment_end, source_duration))
    for shot in segment["shots"]:
        values.update(
            aggregate_shot_evidence_times(segment_start, shot, source_duration)
        )
    return tuple(sorted(values))


def evidence_time_index(facts: dict[str, Any], source_duration: float) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {
        "no_speech_confirmed": list(
            evidence_times(0, source_duration, source_duration)
        )
    }
    if "overall_av" in facts:
        result["overall_av"] = list(evidence_times(0, source_duration, source_duration))
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
        result[segment_path + ".audio_plan"] = aggregate_allowed
        for shot_pos, shot in enumerate(segment["shots"]):
            shot_path = f"segments[{segment_pos}].shots[{shot_pos}]"
            shot_allowed = list(
                aggregate_shot_evidence_times(
                    segment_start,
                    shot,
                    source_duration,
                )
            )
            result[shot_path] = shot_allowed
            result[shot_path + ".beat_plan"] = shot_allowed
            result[shot_path + ".beat_actions"] = shot_allowed
            result[shot_path + ".audio"] = shot_allowed
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
    if omni["schema_version"] != verified["schema_version"]:
        raise ScriptError("Max 不得改变事实 Schema 版本。")
    if omni.get("overall_av") != verified.get("overall_av"):
        differences["overall_av"] = (
            omni.get("overall_av"), verified.get("overall_av"),
            evidence_times(0, source_duration, source_duration),
        )
    if omni["subjects"] != verified["subjects"]:
        differences["subjects"] = (
            omni["subjects"],
            verified["subjects"],
            evidence_times(0, source_duration, source_duration),
        )
    if omni["no_speech_confirmed"] != verified["no_speech_confirmed"]:
        differences["no_speech_confirmed"] = (
            omni["no_speech_confirmed"],
            verified["no_speech_confirmed"],
            evidence_times(0, source_duration, source_duration),
        )
    for segment_pos, (omni_segment, verified_segment) in enumerate(
        zip(omni["segments"], verified["segments"])
    ):
        source_offset = float(omni_segment["source_start_seconds"])
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
            omni_audio_plan = [
                {
                    "index": shot["index"],
                    "start_seconds": shot["start_seconds"],
                    "end_seconds": shot["end_seconds"],
                    "audio": shot["audio"],
                }
                for shot in omni_segment["shots"]
            ]
            verified_audio_plan = [
                {
                    "index": shot["index"],
                    "start_seconds": shot["start_seconds"],
                    "end_seconds": shot["end_seconds"],
                    "audio": shot["audio"],
                }
                for shot in verified_segment["shots"]
            ]
            if omni_audio_plan != verified_audio_plan:
                differences[f"segments[{segment_pos}].audio_plan"] = (
                    omni_audio_plan,
                    verified_audio_plan,
                    allowed,
                )
            continue
        for shot_pos, (omni_shot, verified_shot) in enumerate(
            zip(omni_segment["shots"], verified_segment["shots"])
        ):
            if omni_shot["audio"] != verified_shot["audio"]:
                differences[
                    f"segments[{segment_pos}].shots[{shot_pos}].audio"
                ] = (
                    omni_shot["audio"],
                    verified_shot["audio"],
                    aggregate_shot_evidence_times(
                        source_offset,
                        omni_shot,
                        source_duration,
                    ),
                )
            shot_allowed = aggregate_shot_evidence_times(
                source_offset,
                omni_shot,
                source_duration,
            )
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
                    shot_allowed,
                )
            if compare_beats:
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
                        shot_allowed,
                    )
            for field in VISUAL_FIELDS:
                before = omni_shot[field]
                after = verified_shot[field]
                if before == after:
                    continue
                path = f"segments[{segment_pos}].shots[{shot_pos}].{field}"
                differences[path] = (
                    before,
                    after,
                    shot_allowed,
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


def select_derived_evidence_times(
    path: str,
    before: Any,
    after: Any,
    allowed_times: tuple[float, ...],
) -> list[float]:
    if path.endswith(".beat_actions") and isinstance(before, list) and isinstance(after, list):
        item_count = max(len(before), len(after))
        changed_indices = [
            index
            for index in range(item_count)
            if (before[index] if index < len(before) else None)
            != (after[index] if index < len(after) else None)
        ]
        if changed_indices and allowed_times:
            start = int(min(changed_indices) * len(allowed_times) / item_count)
            end = int((max(changed_indices) + 1) * len(allowed_times) / item_count)
            selected = [float(value) for value in allowed_times[start:max(end, start + 1)]]
            if len(set(selected)) >= min(2, len(allowed_times)):
                return selected

    if path.endswith((".shot_plan", ".beat_plan", ".audio_plan")):
        changed_times: set[float] = set()
        before_items = before if isinstance(before, list) else []
        after_items = after if isinstance(after, list) else []
        before_by_index = {
            item.get("index"): item
            for item in before_items
            if isinstance(item, dict)
        }
        after_by_index = {
            item.get("index"): item
            for item in after_items
            if isinstance(item, dict)
        }
        for index in set(before_by_index) | set(after_by_index):
            before_item = before_by_index.get(index) or {}
            after_item = after_by_index.get(index) or {}
            if before_item == after_item:
                continue
            for key in ("start_seconds", "end_seconds"):
                before_value = before_item.get(key)
                after_value = after_item.get(key)
                if before_value == after_value:
                    continue
                for value in (before_value, after_value):
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        changed_times.add(float(value))
        matched = [
            float(allowed)
            for allowed in allowed_times
            if any(abs(float(allowed) - value) <= 0.001 for value in changed_times)
        ]
        if changed_times:
            for changed in sorted(changed_times):
                nearest = min(
                    allowed_times,
                    key=lambda allowed: abs(float(allowed) - changed),
                )
                matched.append(float(nearest))
            matched = list(dict.fromkeys(matched))
        if len(set(matched)) >= min(2, len(allowed_times)):
            return matched

    if len(allowed_times) <= 3:
        return [float(value) for value in allowed_times]
    middle = allowed_times[len(allowed_times) // 2]
    return list(
        dict.fromkeys(
            [float(allowed_times[0]), float(middle), float(allowed_times[-1])]
        )
    )


def resolve_corrections(
    review: Any,
    differences: dict[str, tuple[Any, Any, tuple[float, ...]]],
) -> list[dict[str, Any]]:
    try:
        if not isinstance(review, dict):
            raise ScriptError("Max fact_review 不是对象。")
        return validate_corrections(review, differences)
    except ScriptError as error:
        if not differences:
            raise
        print(
            f"MAX_CORRECTIONS_DERIVED reason={error}",
            file=sys.stderr,
            flush=True,
        )
        derived: list[dict[str, Any]] = []
        for path, (before, after, allowed_times) in differences.items():
            derived.append(
                {
                    "path": path,
                    "omni_value": before,
                    "corrected_value": after,
                    "evidence_times": select_derived_evidence_times(
                        path,
                        before,
                        after,
                        allowed_times,
                    ),
                    "evidence_description": (
                        "Qwen 3.8 Max verified_source_facts 与 Omni 在该字段"
                        "不一致；程序按对应原片允许采样点生成待用户确认项。"
                    ),
                }
            )
        return derived


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
        (int(segment["index"]), int(shot["index"])): shot
        for segment in facts["segments"]
        for shot in segment["shots"]
    }
    labels = {str(subject["label"]) for subject in facts["subjects"]}
    result: dict[tuple[int, int], str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ScriptError("Max audio override 必须是对象。")
        key = (
            integer(item.get("segment_index"), "audio segment_index"),
            integer(item.get("shot_index"), "audio shot_index"),
        )
        if key not in shots or key in result:
            raise ScriptError(f"Max audio override 无效：{key}")
        audio = validate_audio_description(
            item.get("audio"),
            labels,
            True,
            f"Max audio override {key}：",
        )
        if not audio:
            raise ScriptError(f"Max audio override 无效：{key}")
        source_dialogues = dialogue_texts(str(shots[key].get("audio") or ""))
        override_dialogues = dialogue_texts(audio)
        if (
            source_dialogues
            and not override_dialogues
            and not EXPLICIT_NO_DIALOGUE_PATTERN.search(audio)
        ):
            raise ScriptError(
                f"Max audio override {key} 丢失了原镜头台词；"
                "如果用户明确要求移除对白，必须在 audio 中写明无对白。"
            )
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
    corrections = resolve_corrections(body["fact_review"], differences)
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


def validate_max_candidate_text(
    text: str,
    label: str,
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
    return validate_max_result(
        parse_json_object(text, label),
        omni,
        duration_seconds,
        segment_max_seconds,
        character_image_present,
        product_image_count,
        allow_audio_rewrite,
        source_duration_seconds,
    )


def render_prompt(
    facts: dict[str, Any],
    definitions: dict[str, str],
    audio_overrides: dict[tuple[int, int], str],
) -> str:
    return render_public_prompt(facts, definitions, audio_overrides)


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
    fps: float = 4.0,
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
                {"type": "video_url", "video_url": {"url": video_reference}, "fps": fps},
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
    omni_conflicts: list[dict[str, Any]] | None = None,
    transcript: str = "",
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
            "原始音轨、原曲或参考视频。每句台词必须使用"
            " <说话人>（画内、口型同步）说：{逐字台词} 或"
            " <说话人>（画外、无需口型）说：{逐字台词} 的形式；"
            "多人对话还要明确未发言者自然闭口聆听或重叠发声。"
            "不得用引号代替 {}，不得输出无说话人归属的台词。"
        )
    elif replacements:
        audio_permission = "audio_overrides 必须为空；程序将执行：" + "；".join(
            f"{source}→{target}" for source, target in replacements
        )
    else:
        audio_permission = "audio_overrides 必须为空。"
    context = {
        "omni_source_facts": omni,
        "omni_validation_conflicts": omni_conflicts or [],
        "transcript": transcript or None,
        "source_fact_authority": (
            "Qwen 3.8 Max 是矛盾仲裁结果。可依据原片和 transcript 修正 "
            "verified_source_facts 中的音频事实与 no_speech_confirmed；"
            "所有变化必须进入 fact_review.corrections。"
        ),
        "allowed_evidence_times": evidence_time_index(omni, source_duration),
        "correction_path_contract": CORRECTION_PATH_CONTRACT,
        "timeline_contract": (PUBLIC_TIMELINE_CONTRACT if omni["schema_version"] >= 3 else TIMELINE_CONTRACT),
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


def read_omni_conflicts(path: Path) -> list[dict[str, Any]]:
    resolved = require_readable_file(path, "Omni 矛盾记录")
    try:
        body = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptError("Omni 矛盾记录不是有效 JSON。") from exc
    conflicts = body.get("conflicts") if isinstance(body, dict) else None
    if not isinstance(conflicts, list) or any(
        not isinstance(item, dict) for item in conflicts
    ):
        raise ScriptError("Omni 矛盾记录缺少 conflicts 数组。")
    return conflicts


def build_max_conflict_report(
    args: argparse.Namespace,
    video: Path,
    transcript: Path | None,
    max_candidate_text: str,
    verification_path: Path,
    omni_conflicts: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    if status not in {"pending_user_confirmation", "confirmed"}:
        raise ScriptError(f"Max 矛盾报告状态无效：{status}")
    return {
        "schema_version": 1,
        "status": status,
        "authoritative_model": args.model,
        "resolution_policy": "max_authoritative_after_conflict",
        "analysis_video": file_identity(video),
        "transcript": file_identity(transcript),
        "max_candidate_sha256": text_sha256(max_candidate_text),
        "max_verification": {
            "path": str(verification_path.resolve()),
            "sha256": file_sha256(verification_path.resolve()),
        },
        "omni_validation_conflicts": omni_conflicts,
        "max_corrections": corrections,
    }


def validate_existing_conflict_report(
    path: Path,
    expected: dict[str, Any],
) -> None:
    resolved = require_readable_file(path, "待确认 Max 矛盾报告")
    try:
        actual = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptError("待确认 Max 矛盾报告不是有效 JSON。") from exc
    if not isinstance(actual, dict):
        raise ScriptError("待确认 Max 矛盾报告根节点必须是对象。")
    actual = {**actual, "status": "pending_user_confirmation"}
    if actual != expected:
        raise ScriptError("Max 候选、输入或矛盾内容已变化，必须重新展示并确认。")


def fact_lock(
    args: argparse.Namespace,
    video: Path,
    omni_path: Path,
    verification_path: Path,
    prompt: str,
    plan_path: Path,
    conflict_report_path: Path | None = None,
) -> dict[str, Any]:
    body = {
        "schema_version": 2,
        "prompt_format": FORMAT_VERSION,
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
    if conflict_report_path is not None:
        body["max_conflict_report"] = file_identity(conflict_report_path)
        body["max_conflicts_confirmed"] = True
    return body


def main() -> int:
    args = parse_args()
    try:
        if not 0.1 <= args.fps <= 10:
            raise ScriptError("--fps 必须在 0.1 到 10 之间。")
        if args.allow_audio_rewrite and args.spoken_replacement:
            raise ScriptError("--allow-audio-rewrite 与 --spoken-replacement 不能同时使用。")
        if args.confirm_max_conflicts and not args.overwrite:
            raise ScriptError("--confirm-max-conflicts 只能在恢复运行中使用。")
        try:
            validate_seedance_image_count(
                args.segment_max_seconds,
                int(args.character_image is not None) + len(args.product_image),
            )
        except MediaPreflightError as exc:
            raise ScriptError(str(exc)) from exc
        api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not api_key and args.api_key_file:
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

        omni_conflicts: list[dict[str, Any]] = []
        if args.omni_facts_file:
            omni_path = require_readable_file(args.omni_facts_file, "Omni 事实")
            meta_path = require_readable_file(
                args.omni_metadata_file or args.omni_metadata_output,
                "Omni 事实元数据",
            )
            if json.loads(meta_path.read_text(encoding="utf-8")) != expected_meta:
                raise ScriptError("Omni 事实元数据与当前原片输入不一致。")
            omni_body = json.loads(omni_path.read_text(encoding="utf-8"))
            if args.omni_conflicts_file:
                saved_conflicts = read_omni_conflicts(args.omni_conflicts_file)
                if saved_conflicts:
                    omni, detected_conflicts = validate_facts_for_max_arbitration(
                        omni_body,
                        duration_seconds,
                        args.segment_max_seconds,
                    )
                    if detected_conflicts != saved_conflicts:
                        raise ScriptError("Omni 事实与已保存的矛盾记录不一致。")
                    omni_conflicts = saved_conflicts
                else:
                    omni = validate_facts(
                        omni_body,
                        duration_seconds,
                        args.segment_max_seconds,
                    )
            else:
                omni = validate_facts(
                    omni_body,
                    duration_seconds,
                    args.segment_max_seconds,
                )
        else:
            if not api_key:
                raise ScriptError("未设置 DASHSCOPE_API_KEY 或 --api-key-file。")
            omni_messages = build_omni_messages(
                omni_system,
                video_reference,
                duration_seconds,
                args.segment_max_seconds,
                bool(metadata["has_audio"]),
                transcript,
                fps=args.fps,
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
            captured = (
                reuse_captured_omni_response(
                    args.omni_request_body_output,
                    args.omni_response_body_output,
                    omni_payload,
                )
                if args.overwrite
                else None
            )
            captured_reused = captured is not None
            if captured is None:
                raw_omni, omni_response = call_omni(
                    args.base_url,
                    api_key,
                    omni_payload,
                    args.timeout,
                    args.retries,
                    capture_chunks=bool(args.omni_response_body_output),
                )
            else:
                raw_omni, omni_response = captured
            if args.omni_response_body_output and not captured_reused:
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
            omni_body = parse_json_object(raw_omni, "Omni 事实")
            try:
                omni = validate_facts(
                    omni_body,
                    duration_seconds,
                    args.segment_max_seconds,
                )
            except ScriptError as error:
                if captured_reused:
                    omni, omni_conflicts = validate_facts_for_max_arbitration(
                        omni_body,
                        duration_seconds,
                        args.segment_max_seconds,
                    )
                else:
                    repair_payload = {
                        **omni_payload,
                        "messages": [
                            *omni_messages,
                            {"role": "assistant", "content": raw_omni},
                            {
                                "role": "user",
                                "content": (
                                    f"机器校验失败：{error}\n"
                                    "重新核对原片并完整输出修复 JSON。"
                                ),
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
                    repaired_body = parse_json_object(raw_omni, "Omni 修复事实")
                    try:
                        omni = validate_facts(
                            repaired_body,
                            duration_seconds,
                            args.segment_max_seconds,
                        )
                    except ScriptError:
                        omni, omni_conflicts = validate_facts_for_max_arbitration(
                            repaired_body,
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
            args.omni_conflicts_output,
            {
                "schema_version": 1,
                "status": (
                    "requires_max_arbitration" if omni_conflicts else "none"
                ),
                "conflicts": omni_conflicts,
            },
            args.overwrite,
            "Omni 矛盾记录",
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

        expected_images = int(character is not None) + len(products)
        max_result: tuple[
            dict[str, Any],
            dict[str, list[int]],
            dict[tuple[int, int], str],
            list[dict[str, Any]],
        ] | None = None
        max_candidate_text: str | None = None
        candidate_path = args.candidate_output.expanduser().resolve()
        if args.reuse_candidate and candidate_path.is_file():
            try:
                raw_candidate = read_text(candidate_path, "已有 Max 核验候选")
                max_result = validate_max_candidate_text(
                    raw_candidate,
                    "已有 Max 核验候选",
                    omni,
                    duration_seconds,
                    args.segment_max_seconds,
                    character is not None,
                    len(products),
                    bool(args.allow_audio_rewrite),
                    source_duration,
                )
            except ScriptError as error:
                print(
                    f"MAX_CANDIDATE_REUSE rejected reason={error}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                max_candidate_text = raw_candidate
                print("MAX_CANDIDATE_REUSE accepted", flush=True)

        if max_result is None:
            if not api_key:
                raise ScriptError("未设置 DASHSCOPE_API_KEY 或 --api-key-file。")
            character_reference = (
                resolver.resolve(character, "image") if character else None
            )
            product_references = [resolver.resolve(path, "image") for path in products]
            max_messages = build_max_messages(
                args,
                max_system,
                video_reference,
                omni,
                source_duration,
                character_reference,
                product_references,
                omni_conflicts,
                transcript,
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
            max_candidate_text = raw_max
            write_text_output(
                args.candidate_output,
                raw_max,
                args.overwrite,
                "Max 核验候选",
            )
            try:
                max_result = validate_max_candidate_text(
                    raw_max,
                    "Max 核验结果",
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
                                "严格执行 timeline_contract 指定的时间精度，镜头与 beat "
                                "必须连续完整覆盖；Schema v3 保留真实小数秒边界；"
                                "不得使用单个 start_seconds 或 end_seconds 路径，也不得申报无实际差异的字段。"
                                "只允许修正有时间证据的原片事实与静态外观绑定；"
                                "原片音频事实或 no_speech_confirmed 变化必须作为"
                                " correction 明确记录；镜头计划不变时音频使用"
                                " segments[i].shots[j].audio，计划变化时使用"
                                " segments[i].audio_plan，人声存在性使用"
                                " no_speech_confirmed。创意改写仍只能走 audio_overrides。"
                                "audio_overrides 非空时，每项必须且只能包含整数"
                                " segment_index、整数 shot_index 和字符串 audio；audio"
                                " 必须自包含，不得引用不会提交给 Seedance 的原始媒体。"
                                "每句台词必须用 <说话人>（画内、口型同步）"
                                "说：{逐字台词} 或画外对应形式明确归属，"
                                "多人对话明确闭口聆听或重叠发声；"
                                "不得用引号替代 {}，不得遗漏说话人。"
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
                max_candidate_text = raw_max
                write_text_output(
                    args.candidate_output,
                    raw_max,
                    True,
                    "Max 核验修复候选",
                )
                max_result = validate_max_candidate_text(
                    raw_max,
                    "Max 核验修复结果",
                    omni,
                    duration_seconds,
                    args.segment_max_seconds,
                    character is not None,
                    len(products),
                    bool(args.allow_audio_rewrite),
                    source_duration,
                )

        verified, bindings, overrides, corrections = max_result
        if max_candidate_text is None:
            raise ScriptError("内部错误：缺少 Max 核验候选文本。")

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

        conflict_report_path: Path | None = None
        has_conflicts = bool(omni_conflicts or corrections)
        if has_conflicts:
            pending_report = build_max_conflict_report(
                args,
                video,
                transcript_path,
                max_candidate_text,
                args.verification_output.resolve(),
                omni_conflicts,
                corrections,
                "pending_user_confirmation",
            )
            report_path = args.max_conflict_report_output.expanduser().resolve()
            if args.confirm_max_conflicts:
                if not report_path.is_file():
                    raise ScriptError("缺少已向用户展示的待确认 Max 矛盾报告。")
                validate_existing_conflict_report(report_path, pending_report)
                write_json_output(
                    report_path,
                    {**pending_report, "status": "confirmed"},
                    True,
                    "已确认 Max 矛盾报告",
                )
                conflict_report_path = report_path
            else:
                write_json_output(
                    report_path,
                    pending_report,
                    args.overwrite,
                    "待确认 Max 矛盾报告",
                )
                print(
                    "MAX_CONFLICT_CONFIRMATION_REQUIRED "
                    + json.dumps(
                        {
                            "report": str(report_path),
                            "omni_validation_conflicts": len(omni_conflicts),
                            "max_corrections": len(corrections),
                            "authoritative_model": args.model,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return MAX_CONFLICT_CONFIRMATION_REQUIRED
        elif args.confirm_max_conflicts:
            raise ScriptError("当前没有需要确认的 Max 矛盾。")

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
            facts=verified,
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
                conflict_report_path,
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
