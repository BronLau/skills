#!/usr/bin/env python3
"""Apply explicit user-approved static visual overrides to a locked prompt."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from qwen_video_prompt_reverse import write_text_output
from seedance_video_pipeline import (
    STATIC_OVERRIDE_ASSEMBLY_MODE,
    SeedanceError,
    atomic_write_json,
    file_identity,
    load_json,
    prompt_text_sha256,
    validate_fact_lock_file,
    validate_identity,
    validate_static_visual_overrides,
)
from verified_video_prompt_reverse import definitions_from_bindings, render_prompt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--segment-plan", type=Path, required=True)
    parser.add_argument("--fact-lock", type=Path, required=True)
    parser.add_argument("--max-verification", type=Path, required=True)
    parser.add_argument("--overrides", type=Path, required=True)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def ensure_not_submitted(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    body = load_json(path, "Seedance 任务状态")
    if body.get("uploads") or body.get("segments"):
        raise SeedanceError("已有上传记录或任务状态，拒绝覆盖正式提示词。")


def binding_map(value: Any) -> dict[str, list[int]]:
    if not isinstance(value, list):
        raise SeedanceError("Max appearance_bindings 必须是数组。")
    result: dict[str, list[int]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise SeedanceError("Max appearance binding 无效。")
        label = str(item.get("label") or "").strip()
        refs = item.get("image_refs")
        if not label or not isinstance(refs, list) or label in result:
            raise SeedanceError(f"Max appearance binding 无效或重复：{label}")
        result[label] = [int(ref) for ref in refs]
    return result


def audio_override_map(value: Any) -> dict[tuple[int, int], str]:
    if not isinstance(value, list):
        raise SeedanceError("Max audio_overrides 必须是数组。")
    result: dict[tuple[int, int], str] = {}
    for item in value:
        if not isinstance(item, dict):
            raise SeedanceError("Max audio override 无效。")
        key = (int(item["segment_index"]), int(item["shot_index"]))
        if key in result:
            raise SeedanceError(f"Max audio override 重复：{key}")
        result[key] = str(item.get("audio") or "")
    return result


def apply_overrides(
    facts: dict[str, Any],
    definitions: dict[str, str],
    overrides: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    updated_facts = copy.deepcopy(facts)
    labels = {str(subject["label"]) for subject in updated_facts["subjects"]}
    updated_definitions = dict(definitions)
    for label, definition in overrides["subject_definitions"].items():
        if label not in labels:
            raise SeedanceError(f"静态视觉覆盖引用了不存在的主体：{label}")
        updated_definitions[label] = definition

    shots = {
        (int(segment["index"]), int(shot["index"])): shot
        for segment in updated_facts["segments"]
        for shot in segment["shots"]
    }
    for item in overrides["shot_overrides"]:
        key = (int(item["segment_index"]), int(item["shot_index"]))
        target = shots.get(key)
        if target is None:
            raise SeedanceError(f"静态视觉覆盖引用了不存在的镜头：{key}")
        for field in ("composition", "scene_light"):
            if field in item:
                target[field] = item[field]
    return updated_facts, updated_definitions


def main() -> int:
    args = parse_args()
    if not args.overwrite:
        raise SeedanceError("静态视觉覆盖要求显式传入 --overwrite。")
    prompt_path = args.prompt.expanduser().resolve(strict=True)
    segment_plan_path = args.segment_plan.expanduser().resolve(strict=True)
    lock_path = args.fact_lock.expanduser().resolve(strict=True)
    verification_path = args.max_verification.expanduser().resolve(strict=True)
    overrides_path = args.overrides.expanduser().resolve(strict=True)
    ensure_not_submitted(args.tasks.expanduser().resolve() if args.tasks else None)
    lock = validate_fact_lock_file(lock_path, prompt_path, segment_plan_path)
    if validate_identity(lock["max_verification"], "Max 核验事实") != verification_path:
        raise SeedanceError("指定的 Max 核验事实与事实锁不一致。")
    overrides = validate_static_visual_overrides(
        load_json(overrides_path, "静态视觉覆盖文件")
    )
    verification = load_json(verification_path, "Max 核验事实")
    facts = verification.get("verified_source_facts")
    if not isinstance(facts, dict):
        raise SeedanceError("Max 核验事实缺少 verified_source_facts。")
    bindings = binding_map(verification.get("appearance_bindings"))
    definitions = definitions_from_bindings(facts, bindings)
    updated_facts, updated_definitions = apply_overrides(
        facts, definitions, overrides
    )
    prompt = render_prompt(
        updated_facts,
        updated_definitions,
        audio_override_map(verification.get("audio_overrides")),
    )
    write_text_output(prompt_path, prompt, True, "静态视觉覆盖正式提示词")
    lock["assembly_mode"] = STATIC_OVERRIDE_ASSEMBLY_MODE
    lock["prompt_sha256"] = prompt_text_sha256(prompt_path)
    lock["static_visual_overrides"] = file_identity(overrides_path)
    atomic_write_json(lock_path, lock)
    validate_fact_lock_file(lock_path, prompt_path, segment_plan_path)
    print(f"STATIC_OVERRIDE applied prompt={prompt_path} lock={lock_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        raise SystemExit(1)
