#!/usr/bin/env python3
"""Merge Max-verified audio overrides into previously locked visual facts."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path
from typing import Any

from qwen_video_prompt_reverse import validate_prompt_contract, write_text_output
from seedance_video_pipeline import (
    AUDIO_OVERRIDE_ASSEMBLY_MODE,
    SeedanceError,
    atomic_write_json,
    file_identity,
    file_sha256,
    load_json,
    prompt_text_sha256,
    validate_fact_lock_file,
    validate_identity,
)
from verified_video_prompt_reverse import (
    definitions_from_bindings,
    probe_video,
    render_prompt,
    validate_audio_overrides,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-prompt", type=Path, required=True)
    parser.add_argument("--base-segment-plan", type=Path, required=True)
    parser.add_argument("--base-fact-lock", type=Path, required=True)
    parser.add_argument("--base-max-verification", type=Path, required=True)
    parser.add_argument("--audio-prompt", type=Path, required=True)
    parser.add_argument("--audio-segment-plan", type=Path, required=True)
    parser.add_argument("--audio-fact-lock", type=Path, required=True)
    parser.add_argument("--audio-max-verification", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def binding_map(value: Any) -> dict[str, list[int]]:
    if not isinstance(value, list):
        raise SeedanceError("Max appearance_bindings 必须是数组。")
    result: dict[str, list[int]] = {}
    used_refs: set[int] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"label", "image_refs"}:
            raise SeedanceError("Max appearance binding 无效。")
        label = str(item.get("label") or "").strip()
        refs = item.get("image_refs")
        if not label or not isinstance(refs, list) or label in result:
            raise SeedanceError(f"Max appearance binding 无效或重复：{label}")
        normalized = [int(ref) for ref in refs]
        if any(ref <= 0 or ref in used_refs for ref in normalized):
            raise SeedanceError(f"Max appearance 图片引用无效或重复：{label}")
        used_refs.update(normalized)
        result[label] = normalized
    return result


def require_matching_inputs(
    base_lock: dict[str, Any],
    audio_lock: dict[str, Any],
    base_plan: dict[str, Any],
    audio_plan: dict[str, Any],
) -> None:
    if base_plan != audio_plan:
        raise SeedanceError("音频重建改变了分段计划，拒绝合并。")
    for key in ("analysis_video", "omni_facts"):
        base_identity = base_lock.get(key) or {}
        audio_identity = audio_lock.get(key) or {}
        if base_identity.get("sha256") != audio_identity.get("sha256"):
            raise SeedanceError(f"两次核验使用的 {key} 不一致，拒绝合并。")
    for key in ("omni_model", "max_model", "fps"):
        if base_lock.get(key) != audio_lock.get(key):
            raise SeedanceError(f"两次核验的 {key} 不一致，拒绝合并。")


def main() -> int:
    args = parse_args()
    if not args.overwrite:
        raise SeedanceError("音频覆盖合并要求显式传入 --overwrite。")
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SeedanceError(f"输出目录不是空目录，拒绝覆盖：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    base_prompt = args.base_prompt.expanduser().resolve(strict=True)
    base_plan_path = args.base_segment_plan.expanduser().resolve(strict=True)
    base_lock_path = args.base_fact_lock.expanduser().resolve(strict=True)
    base_verification_path = args.base_max_verification.expanduser().resolve(strict=True)
    audio_prompt = args.audio_prompt.expanduser().resolve(strict=True)
    audio_plan_path = args.audio_segment_plan.expanduser().resolve(strict=True)
    audio_lock_path = args.audio_fact_lock.expanduser().resolve(strict=True)
    audio_verification_path = args.audio_max_verification.expanduser().resolve(strict=True)

    base_lock = validate_fact_lock_file(base_lock_path, base_prompt, base_plan_path)
    audio_lock = validate_fact_lock_file(audio_lock_path, audio_prompt, audio_plan_path)
    if validate_identity(base_lock["max_verification"], "基础 Max 核验结果") != base_verification_path:
        raise SeedanceError("基础 Max 核验结果与事实锁不一致。")
    if validate_identity(audio_lock["max_verification"], "音频 Max 核验结果") != audio_verification_path:
        raise SeedanceError("音频 Max 核验结果与事实锁不一致。")

    base_plan = load_json(base_plan_path, "基础分段计划")
    audio_plan = load_json(audio_plan_path, "音频分段计划")
    require_matching_inputs(base_lock, audio_lock, base_plan, audio_plan)
    base_verification = load_json(base_verification_path, "基础 Max 核验结果")
    audio_verification = load_json(audio_verification_path, "音频 Max 核验结果")
    facts = base_verification.get("verified_source_facts")
    if not isinstance(facts, dict):
        raise SeedanceError("基础 Max 核验结果缺少 verified_source_facts。")
    bindings = binding_map(base_verification.get("appearance_bindings"))
    overrides = validate_audio_overrides(
        audio_verification.get("audio_overrides"),
        facts,
        True,
    )
    if not overrides:
        raise SeedanceError("音频 Max 核验结果没有可合并的 audio_overrides。")

    definitions = definitions_from_bindings(facts, bindings)
    prompt = render_prompt(facts, definitions, overrides)
    analysis_video = validate_identity(base_lock["analysis_video"], "分析视频")
    metadata = probe_video(analysis_video)
    expected_images = len({ref for refs in bindings.values() for ref in refs})
    rebuilt_plan = validate_prompt_contract(
        prompt,
        int(metadata["duration_seconds"]),
        float(metadata["source_duration"]),
        int(base_plan["segment_max_seconds"]),
        expected_images,
        "锁定视觉事实与已核验音频覆盖合并终稿",
    )
    if rebuilt_plan != base_plan:
        raise SeedanceError("合并后的提示词改变了分段计划，拒绝写入。")

    omni_source = validate_identity(base_lock["omni_facts"], "Omni 初步事实")
    omni_output = output_dir / "omni_facts.json"
    shutil.copy2(omni_source, omni_output)
    prompt_output = output_dir / "prompt.txt"
    plan_output = output_dir / "segment_plan.json"
    verification_output = output_dir / "max_verification.json"
    lock_output = output_dir / "fact_lock.json"
    write_text_output(prompt_output, prompt, False, "音频覆盖合并正式提示词")
    atomic_write_json(plan_output, base_plan)
    merged_verification = copy.deepcopy(base_verification)
    merged_verification["audio_overrides"] = [
        {
            "segment_index": key[0],
            "shot_index": key[1],
            "audio": value,
        }
        for key, value in overrides.items()
    ]
    atomic_write_json(verification_output, merged_verification)

    merged_lock = copy.deepcopy(base_lock)
    merged_lock["assembly_mode"] = AUDIO_OVERRIDE_ASSEMBLY_MODE
    merged_lock["prompt_sha256"] = prompt_text_sha256(prompt_output)
    merged_lock["segment_plan_sha256"] = file_sha256(plan_output)
    merged_lock["omni_facts"] = file_identity(omni_output)
    merged_lock["max_verification"] = file_identity(verification_output)
    merged_lock["base_fact_lock"] = file_identity(base_lock_path)
    merged_lock["audio_fact_lock"] = file_identity(audio_lock_path)
    merged_lock["audio_verification"] = file_identity(audio_verification_path)
    atomic_write_json(lock_output, merged_lock)
    validate_fact_lock_file(lock_output, prompt_output, plan_output)
    print(f"AUDIO_OVERRIDE merged prompt={prompt_output} lock={lock_output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", flush=True)
        raise SystemExit(1)
